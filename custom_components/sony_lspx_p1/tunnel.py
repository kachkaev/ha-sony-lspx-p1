"""The projector's HTTP API through its Bluetooth tunnel (service EE001950): framing and sequence.

The official app sends some HTTP requests over Bluetooth instead of Wi-Fi (BleWebApiPassThroughIfs); see "Bluetooth
tunnel" in docs/protocol.md. Seen on one projector (firmware 3.1.15, 2026-11-24): GET and POST /v1/settings/time
answered as written up there, every notification echoing the counter (``seq``) of the frame it answers. Nothing else
was tried: no other path, no errors, no re-polls, no body longer than one read, and the projector was on and awake.

No Bluetooth or Home Assistant dependencies: the client (client.py) supplies the link.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

SERVICE = "ee001950-5ab8-446b-2a0f-e1c55a6d871f"  # WebApiPassThroughService
CHAR_CONTROL = "ee00270d-5ab8-446b-2a0f-e1c55a6d871f"  # WebAPIpassthroughControlPoint: frames out, notifications back
CHAR_PARAM = "ee002c46-5ab8-446b-2a0f-e1c55a6d871f"  # WebAPIpassthroughParam: path and body out, body chunks back
CHARACTERISTICS = (CHAR_CONTROL, CHAR_PARAM)

# Each step answered within 0.06 s, the POST's response within 1 s (the HTTP request runs then); the app has no
# timeout per step, only 60 s for the whole request.
STEP_TIMEOUT = 8.0
# The app asks for the response again every second while the projector answers it with an error (presumably while
# the HTTP request still runs), for 30 s; never seen. Fewer here, so that a projector in an unknown state does not hold
# the link for long.
RETRY_DELAY = 1.0
RETRY_LIMIT = 5
# A reply to the clock is 64 bytes; anything far longer is not one.
MAX_BODY = 4096

POST_BODY_OP = 0x41  # HTTPPOSTWriteRequestBody

_LOGGER = logging.getLogger(__name__)


class Method(enum.Enum):
    """The control point ops of each HTTP method: (write request, read response, read response body).

    DELETE (0x58, 0x5A, 0x5B) is left out: never sent through the tunnel.
    """

    GET = (0x50, 0x52, 0x53)
    POST = (0x40, 0x42, 0x43)

    @property
    def request_op(self) -> int:
        return self.value[0]

    @property
    def response_op(self) -> int:
        return self.value[1]

    @property
    def body_op(self) -> int:
        return self.value[2]


class TunnelError(Exception):
    """The tunnel is missing, refused a step (``code``, as on the command characteristic) or answered otherwise than
    written up."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class TunnelMissingError(TunnelError):
    """The projector does not offer the tunnel's characteristics."""


@dataclass(frozen=True)
class Notification:
    """A control point notification: ``[seq:u16][op:u16][error:u8][extra...]``."""

    seq: int
    op: int
    error: int
    http_status: int | None = None  # response ops, when the frame has bytes 5-10
    total_length: int | None = None


@dataclass(frozen=True)
class Response:
    http_status: int
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass
class Counter:
    """The app's step counter (``seq``), which runs on across requests; one per link to the projector."""

    value: int = 0


class Link(Protocol):
    """The GATT operations a request needs, on a link with notifications on the control point enabled."""

    async def write(self, char: str, data: bytes) -> None: ...
    async def read(self, char: str) -> bytes: ...
    async def notification(self) -> bytes:
        """The next control point notification (request() bounds the wait)."""
        ...


def frame_request(seq: int, method: Method, path: bytes, body: bytes = b"") -> bytes:
    """The 0x40/0x50 frame, written to the control point after the path was written to the param."""
    frame = struct.pack(">HHH", seq & 0xFFFF, method.request_op, len(path))
    if method is Method.POST:
        frame += struct.pack(">I", len(body))
    return frame


def frame_post_body(seq: int, body: bytes) -> bytes:
    """The 0x41 frame, written to the control point after the body was written to the param."""
    return struct.pack(">HHH", seq & 0xFFFF, POST_BODY_OP, len(body))


def frame_call(seq: int, op: int) -> bytes:
    """A frame without arguments: read the response (0x42/0x52) or the next body chunk (0x43/0x53)."""
    return struct.pack(">HH", seq & 0xFFFF, op)


def parse_notification(data: bytes) -> Notification:
    """As the app's BleWebApiJob.parseCPNotify reads it. Raises TunnelError for a frame shorter than 5 bytes."""
    if len(data) < 5:
        raise TunnelError(f"notification shorter than 5 bytes: {data.hex()}")
    seq, op, error = struct.unpack_from(">HHB", data)
    if not error and op in {m.response_op for m in Method} and len(data) >= 11:
        status, total = struct.unpack_from(">HI", data, 5)
        return Notification(seq, op, error, http_status=status, total_length=total)
    return Notification(seq, op, error)


async def request(
    link: Link,
    method: Method,
    path: str,
    body: str | None = None,
    *,
    counter: Counter,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Response:
    """One request, step by step as the app's BleWebApiExecutor does. Raises TunnelError.

    ``counter`` is advanced by every step, also when the request fails part way.
    """
    path_bytes = path.encode()
    body_bytes = (body or "").encode()
    await link.write(CHAR_PARAM, path_bytes)
    await _call(link, counter, frame_request(counter.value, method, path_bytes, body_bytes), method.request_op)
    counter.value += 2
    if method is Method.POST:
        await link.write(CHAR_PARAM, body_bytes)
        await _call(link, counter, frame_post_body(counter.value, body_bytes), POST_BODY_OP)
        counter.value += 2

    retries = 0
    while True:
        note = await _call(link, counter, frame_call(counter.value, method.response_op), method.response_op, ok=False)
        if not note.error:
            break
        if retries >= RETRY_LIMIT:
            raise TunnelError(f"error 0x{note.error:02X} asking for the response, {retries} times again", note.error)
        retries += 1
        await sleep(RETRY_DELAY)  # the same frame again, as the app does
    counter.value += 1
    if note.http_status is None or note.total_length is None:
        raise TunnelError("no HTTP status in the response")
    if note.total_length > MAX_BODY:
        raise TunnelError(f"a body of {note.total_length} bytes")

    received = bytearray()
    while len(received) < note.total_length:
        await _call(link, counter, frame_call(counter.value, method.body_op), method.body_op)
        counter.value += 2
        chunk = await link.read(CHAR_PARAM)
        if not chunk:
            raise TunnelError(f"empty body chunk, {note.total_length - len(received)} bytes missing")
        received += chunk
    return Response(note.http_status, bytes(received))


async def _call(link: Link, counter: Counter, frame: bytes, op: int, *, ok: bool = True) -> Notification:
    """Write a control point frame and wait for the notification with its ``seq`` and ``op``.

    Others are dropped: a frame sent again on subscribing, or a copy of an earlier one, as the command characteristic
    sometimes sends. With ``ok``, an error in it raises TunnelError.
    """
    seq = counter.value & 0xFFFF
    await link.write(CHAR_CONTROL, frame)
    try:
        async with asyncio.timeout(STEP_TIMEOUT):
            note = await _answer(link, seq, op)
    except TimeoutError as err:
        raise TunnelError(f"no answer to op 0x{op:02X} within {STEP_TIMEOUT:.0f} s") from err
    if ok and note.error:
        raise TunnelError(f"error 0x{note.error:02X} to op 0x{op:02X}", note.error)
    return note


async def _answer(link: Link, seq: int, op: int) -> Notification:
    while True:
        data = await link.notification()
        try:
            note = parse_notification(data)
        except TunnelError as err:
            _LOGGER.debug("tunnel: ignoring %s", err)
            continue
        if (note.seq, note.op) == (seq, op):
            return note
        _LOGGER.debug("tunnel: ignoring notification %s while waiting for seq %d op 0x%02X", data.hex(), seq, op)
