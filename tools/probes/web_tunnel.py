# Sends one HTTP request to the projector through its Bluetooth tunnel (service EE001950) and prints every frame.
#
# UNTESTED ON A REAL PROJECTOR. Written from docs/protocol.md ("Bluetooth tunnel"), which was reconstructed from the
# official app only. Every frame sent and received is printed in hex with a timestamp, so that one run either works
# or shows exactly which step answers differently from the write-up.
#
# Usage (it does nothing without a command):
#   .venv/bin/python tools/probes/web_tunnel.py get /v1/settings/time
#   .venv/bin/python tools/probes/web_tunnel.py post /v1/settings/posterResume '{"posterResume":true}'
#
# - The projector serves one Bluetooth client: turn off the Home Assistant integration (or stop Home Assistant) and
#   close the phone app first. The script disconnects when it is done.
# - This computer must already be registered with the projector (`tools/lspx_cli.py pair`): the tunnel presumably
#   needs the same bond and encrypted link as the commands. `--no-secure` skips the explicit encryption step, to see
#   how the projector refuses (the OS may still encrypt a bonded link). On Linux, a pairing agent must be running
#   (a desktop session has one; otherwise `bluetoothctl` with `agent on`), or encrypting can hang; it gives up
#   after 15 s.
# - POSTs to paths that start firmware updates, change Wi-Fi or the network, or reset anything are refused.
# - The control point is written without response when it declares write-without-response, as Android does by
#   default; the param always with response, so that long values go out as long writes.
from __future__ import annotations

import argparse
import asyncio
import enum
import json
import struct
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

SERVICE = "ee001950-5ab8-446b-2a0f-e1c55a6d871f"  # WebApiPassThroughService
CP = "ee00270d-5ab8-446b-2a0f-e1c55a6d871f"  # WebAPIpassthroughControlPoint: frames out, notifications back
PARAM = "ee002c46-5ab8-446b-2a0f-e1c55a6d871f"  # WebAPIpassthroughParam: path and body out, body chunks back
SECURITY_PROBE = "ee002193-5ab8-446b-2a0f-e1c55a6d871f"  # needs an encrypted link; reading it encrypts a bonded one
LOCAL_NAME_PREFIX = "LSPXP1"

REQUEST_TIMEOUT = 60.0  # BleControl.webApi*Sync; like the app, no step has a timeout of its own
SECURE_TIMEOUT = 15.0
RETRY_DELAY = 1.0  # BleWebApiExecutor re-sends the response frame after an error, every second...
RETRY_LIMIT = 30  # ...up to 30 times
MAX_BODY_READS = 1000

# Refused for POST only (GET is read-only):
REFUSED_PATH_PARTS = (
    "firmware",
    "wifi",
    "wps",
    "ssid",
    "/settings/ap",
    "connection",
    "application",
    "reset",
    "initiali",
    "factory",
    "demo",
)

ERRORS = {
    0x01: "checksum",
    0x02: "invalid argument",
    0x03: "unknown command",
    0xA0: "access denied",
    0xC0: "timeout",
    0xD0: "module error",
    0xFF: "runtime error",
}


class Method(enum.Enum):
    """The control point ops of each HTTP method: (write request, read response, read response body)."""

    GET = (0x50, 0x52, 0x53)
    POST = (0x40, 0x42, 0x43)
    DELETE = (0x58, 0x5A, 0x5B)

    @property
    def request_op(self) -> int:
        return self.value[0]

    @property
    def response_op(self) -> int:
        return self.value[1]

    @property
    def body_op(self) -> int:
        return self.value[2]


POST_BODY_OP = 0x41  # HTTPPOSTWriteRequestBody
RESPONSE_OPS = {m.response_op for m in Method}
BODY_OPS = {m.body_op for m in Method}
PLAIN_OPS = {0x40, 0x41, 0x44, 0x50, 0x54, 0x58}  # nothing after the error byte


class TunnelError(Exception):
    """The projector answered differently from the write-up, or not at all."""


# --- Framing ---------------------------------------------------------------------------------------------------


def frame_request(seq: int, method: Method, path: bytes, body: bytes | None = None) -> bytes:
    """The 0x40/0x50/0x58 frame, written to the control point after the path was written to the param."""
    frame = struct.pack(">HHH", seq & 0xFFFF, method.request_op, len(path))
    if method is Method.POST:
        frame += struct.pack(">I", len(body or b""))
    return frame


def frame_post_body(seq: int, body: bytes) -> bytes:
    """The 0x41 frame, written to the control point after the body was written to the param."""
    return struct.pack(">HHH", seq & 0xFFFF, POST_BODY_OP, len(body))


def frame_call(seq: int, op: int) -> bytes:
    """A frame without arguments: read the response (0x42/0x52/0x5A) or the next body chunk (0x43/0x53/0x5B)."""
    return struct.pack(">HH", seq & 0xFFFF, op)


@dataclass(frozen=True)
class Notification:
    seq: int
    op: int
    error: int
    http_status: int | None = None  # response ops, when the frame has bytes 5-10
    total_length: int | None = None
    chunk_length: int | None = None  # body ops, when the frame has bytes 5-6; the app ignores it

    def describe(self) -> str:
        text = f"seq={self.seq} op=0x{self.op:02X} error=0x{self.error:02X}"
        if self.error:
            text += f" ({ERRORS.get(self.error, 'unknown')})"
        if self.http_status is not None:
            text += f" http_status={self.http_status} total_length={self.total_length}"
        if self.chunk_length is not None:
            text += f" chunk_length={self.chunk_length}"
        return text


def parse_notification(data: bytes) -> Notification:
    """Parse a control point notification as BleWebApiJob.parseCPNotify does (its raw instructions)."""
    if len(data) < 5:
        raise TunnelError(f"notification shorter than 5 bytes: {data.hex()}")
    seq, op, error = struct.unpack_from(">HHB", data)
    if error:
        return Notification(seq, op, error)
    if op in RESPONSE_OPS:
        if len(data) > 10:
            status, total = struct.unpack_from(">HI", data, 5)
            return Notification(seq, op, error, http_status=status, total_length=total)
        return Notification(seq, op, error)  # the app then takes status 0 and an empty body
    if op in BODY_OPS:
        if len(data) > 6:
            return Notification(seq, op, error, chunk_length=struct.unpack_from(">H", data, 5)[0])
        return Notification(seq, op, error)
    if op in PLAIN_OPS:
        return Notification(seq, op, error)
    raise TunnelError(f"notification with an op the app does not know (0x{op:02X}): {data.hex()}")


class BodyAssembler:
    """Joins body chunks until the total length from the response notification is reached."""

    def __init__(self, total_length: int) -> None:
        self.total_length = total_length
        self._chunks: list[bytes] = []
        self._received = 0

    @property
    def remaining(self) -> int:
        return max(self.total_length - self._received, 0)

    @property
    def complete(self) -> bool:
        return self._received >= self.total_length

    def add(self, chunk: bytes) -> None:
        if not chunk and not self.complete:
            # The app would ask again until its 60 s run out; stop here instead.
            raise TunnelError(f"empty body chunk with {self.remaining} of {self.total_length} bytes missing")
        self._chunks.append(chunk)
        self._received += len(chunk)

    def body(self) -> bytes:
        # Joined as bytes before decoding, unlike the app, so a UTF-8 character split between chunks survives.
        return b"".join(self._chunks)


# --- Sequence --------------------------------------------------------------------------------------------------


class Link(Protocol):
    """The GATT operations the tunnel needs; BleakLink below, or a fake in the tests."""

    async def enable_notifications(self) -> None: ...
    async def write(self, char: str, data: bytes) -> None: ...
    async def read(self, char: str) -> bytes: ...
    async def notification(self) -> bytes: ...
    def drain(self) -> list[bytes]: ...


@dataclass(frozen=True)
class Response:
    http_status: int
    body: bytes
    next_seq: int
    retries: int

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


Log = Callable[[str], None]


async def _call(link: Link, frame: bytes, expected_op: int, log: Log) -> Notification:
    """Write a control point frame and wait for its notification, as long as the request's 60 s allow."""
    for stale in link.drain():
        log(f"   (dropping a notification that arrived between steps: {stale.hex()})")
    await link.write(CP, frame)
    started = time.monotonic()
    while True:
        note = parse_notification(await link.notification())
        log(f"   = {note.describe()} (after {time.monotonic() - started:.3f} s)")
        if note.op == expected_op:
            break
        # The app does not check the op; keep waiting so that one run shows what comes next.
        log(f"   (op 0x{note.op:02X} is not the 0x{expected_op:02X} just sent; waiting for another notification)")
    if note.seq != struct.unpack_from(">H", frame)[0]:
        log("   (seq differs from the frame's; the app ignores it)")
    return note


async def request(
    link: Link,
    method: Method,
    path: str,
    body: str | None = None,
    *,
    seq: int = 0,
    retry_delay: float = RETRY_DELAY,
    log: Log = print,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Response:
    """Run one request the way BleWebApiExecutor does; ``seq`` is the app's step counter at the start."""
    path_bytes = path.encode()
    body_bytes = (body or "").encode()
    log(f"-- {method.name} {path}" + (f" body {body_bytes!r}" if method is Method.POST else ""))
    log("1. enable notifications on the control point")
    await link.enable_notifications()

    log("2. path -> param, then the request frame")
    await link.write(PARAM, path_bytes)
    await _call(link, frame_request(seq, method, path_bytes, body_bytes), method.request_op, log)
    seq += 2
    if method is Method.POST:
        log("3. body -> param, then the body frame")
        await link.write(PARAM, body_bytes)
        await _call(link, frame_post_body(seq, body_bytes), POST_BODY_OP, log)
        seq += 2

    log("4. ask for the response")
    retries = 0
    while True:
        note = await _call(link, frame_call(seq, method.response_op), method.response_op, log)
        if not note.error:
            break
        if retries >= RETRY_LIMIT:
            raise TunnelError(f"still error 0x{note.error:02X} after {retries} re-polls")
        retries += 1
        log(f"   re-poll {retries}/{RETRY_LIMIT} in {retry_delay:g} s (the app re-sends the same frame)")
        await sleep(retry_delay)
    seq += 1
    status = note.http_status or 0
    if note.http_status is None:
        log("   (no status or length in the notification: the app takes status 0 and an empty body)")
    assembler = BodyAssembler(note.total_length or 0)

    log(f"5. read the body ({assembler.total_length} bytes)")
    reads = 0
    while not assembler.complete:
        reads += 1
        if reads > MAX_BODY_READS:
            raise TunnelError(f"more than {MAX_BODY_READS} body reads")
        chunk_note = await _call(link, frame_call(seq, method.body_op), method.body_op, log)
        if chunk_note.error:
            raise TunnelError(f"error 0x{chunk_note.error:02X} asking for a body chunk")
        seq += 2
        chunk = await link.read(PARAM)
        if chunk_note.chunk_length is not None and chunk_note.chunk_length != len(chunk):
            log(f"   (read {len(chunk)} bytes, the notification said {chunk_note.chunk_length})")
        assembler.add(chunk)
        log(f"   {len(chunk)} bytes, {assembler.remaining} to go")
    return Response(status, assembler.body(), seq, retries)


# --- Bluetooth -------------------------------------------------------------------------------------------------


class BleakLink:
    """Bleak, printing every frame with the time since the start."""

    def __init__(self, client: Any, log: Log, *, cp_response: bool = True) -> None:
        self._client = client
        self._log = log
        self._cp_response = cp_response
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._subscribed = False

    def _on_notify(self, _char: object, data: bytearray) -> None:
        self._log(f"<- notify {CP[:8]} {bytes(data).hex()}")
        self._queue.put_nowait(bytes(data))

    async def enable_notifications(self) -> None:
        # Bleak writes the CCCD once; the app writes it at the start of every request.
        if not self._subscribed:
            await self._client.start_notify(CP, self._on_notify)
            self._subscribed = True
            self._log(f"-> CCCD {CP[:8]} 0100 (subscribed)")

    async def write(self, char: str, data: bytes) -> None:
        self._log(f"-> write  {char[:8]} {data.hex()}" + (f"  {data!r}" if char == PARAM else ""))
        response = char == PARAM or self._cp_response
        await self._client.write_gatt_char(char, data, response=response)
        self._log(f"   write response {char[:8]}" if response else f"   written without response {char[:8]}")

    async def read(self, char: str) -> bytes:
        data = bytes(await self._client.read_gatt_char(char))
        self._log(f"<- read   {char[:8]} {data.hex()}  {data!r}")
        return data

    async def notification(self) -> bytes:
        return await self._queue.get()

    def drain(self) -> list[bytes]:
        stale = []
        while not self._queue.empty():
            stale.append(self._queue.get_nowait())
        return stale


async def secure(client: Any, log: Log) -> None:
    """Encrypt a bonded link as the integration's client does: read a protected value, else pair() and read again."""
    try:
        async with asyncio.timeout(SECURE_TIMEOUT):
            await client.read_gatt_char(SECURITY_PROBE)
        log("link encrypted (security probe read)")
        return
    except Exception as err:
        log(f"security probe read failed: {type(err).__name__}: {err}")
    try:
        async with asyncio.timeout(SECURE_TIMEOUT):
            await client.pair()
    except Exception as err:
        log(f"pair() failed: {type(err).__name__}: {err}")
    try:
        async with asyncio.timeout(SECURE_TIMEOUT):
            await client.read_gatt_char(SECURITY_PROBE)
        log("link encrypted (security probe read after pair())")
    except Exception as err:
        log(f"security probe read after pair() failed: {type(err).__name__}: {err}; going on anyway")


def refused(path: str) -> str | None:
    lowered = path.lower()
    return next((part for part in REFUSED_PATH_PARTS if part in lowered), None)


async def main() -> int:
    parser = argparse.ArgumentParser(description="One HTTP request through the projector's Bluetooth tunnel.")
    parser.add_argument("--address", help="the projector's Bluetooth address or (macOS) UUID; default: scan")
    parser.add_argument("--seq", type=int, default=0, help="the counter's start value (the app's starts at 0)")
    parser.add_argument("--no-secure", action="store_true", help="skip the explicit encryption step")
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("get", help="GET a path, for example /v1/settings/time")
    get.add_argument("path")
    post = sub.add_parser("post", help="POST a JSON body to a path")
    post.add_argument("path")
    post.add_argument("body", help="JSON, sent as given")
    args = parser.parse_args()

    if not args.path.startswith("/"):
        parser.error("the path starts with /, as over Wi-Fi")
    if args.command == "post" and (part := refused(args.path)):
        parser.error(f"refusing a path containing {part!r}")
    if args.command == "post":
        try:
            json.loads(args.body)
        except ValueError as err:
            parser.error(f"the body is not JSON: {err}")

    from bleak import BleakClient, BleakScanner

    t0 = time.monotonic()

    def log(line: str) -> None:
        print(f"{time.monotonic() - t0:8.3f}s {line}", flush=True)

    log(f"started {time.strftime('%Y-%m-%d %H:%M:%S %z')}; python {sys.version.split()[0]}")
    if args.address:
        device = await BleakScanner.find_device_by_address(args.address, timeout=20)
    else:
        device = await BleakScanner.find_device_by_filter(
            lambda d, a: (a.local_name or d.name or "").startswith(LOCAL_NAME_PREFIX), timeout=20
        )
    if device is None:
        log("projector not found (is another client connected?)")
        return 1
    log(f"found {device.address} {device.name}")
    async with BleakClient(device, timeout=30) as client:
        log(f"connected; mtu {client.mtu_size}")
        service = client.services.get_service(SERVICE)
        if service is None:
            log(f"service {SERVICE} not found")
            return 1
        cp_response = True
        for char in service.characteristics:
            log(f"   char {char.uuid} [{','.join(char.properties)}]")
            if char.uuid.lower() == CP:
                cp_response = "write-without-response" not in char.properties
        log(f"control point frames are written {'with' if cp_response else 'without'} response")
        if args.no_secure:
            log("not encrypting the link (--no-secure)")
        else:
            await secure(client, log)
        link = BleakLink(client, log, cp_response=cp_response)
        method = Method.GET if args.command == "get" else Method.POST
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await request(link, method, args.path, getattr(args, "body", None), seq=args.seq, log=log)
        except TunnelError as err:
            log(f"FAILED: {err}")
            return 1
        except TimeoutError:
            log(f"FAILED: no result within {REQUEST_TIMEOUT:.0f} s")
            return 1
        except Exception as err:
            log(f"FAILED: {type(err).__name__}: {err}")
            return 1
        log(f"HTTP {response.http_status}, {len(response.body)} bytes, {response.retries} re-polls")
        print(response.text())
    log("disconnected")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
