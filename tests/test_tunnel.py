"""The HTTP API through the Bluetooth tunnel (tunnel.py, LSPXClient.web_request) against the fake projector.

The fake answers GET and POST /v1/settings/time as one projector did (firmware 3.1.15, 2026-11-24): the frames in
test_frames_as_seen_on_a_projector are those of that session. Everything else (errors, re-polls, several reads,
stale notifications) follows the app and was never seen.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.sony_lspx_p1 import client as client_module
from custom_components.sony_lspx_p1 import tunnel
from custom_components.sony_lspx_p1.client import LSPXClient
from custom_components.sony_lspx_p1.protocol import ErrorCode, Op

from .fake_projector import FakeBleakClient, FakeProjector, FakeTunnel

TIME = "/v1/settings/time"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """No Home Assistant needed."""


@pytest.fixture
def client(ble_device: BLEDevice, fake_connection: list[FakeBleakClient]) -> LSPXClient:
    return LSPXClient(ble_device, idle_disconnect=False)


@pytest.fixture
def fake(projector: FakeProjector) -> FakeTunnel:
    assert projector.tunnel is not None
    return projector.tunnel


class Recorder:
    """A tunnel.Link that records frames and answers from a script (for the framing alone)."""

    def __init__(self, notifications: list[bytes], reads: list[bytes]) -> None:
        self.written: list[tuple[str, bytes]] = []
        self._notifications = notifications
        self._reads = reads

    async def write(self, char: str, data: bytes) -> None:
        self.written.append(("param" if char == tunnel.CHAR_PARAM else "control", data))

    async def read(self, char: str) -> bytes:
        return self._reads.pop(0)

    async def notification(self) -> bytes:
        return self._notifications.pop(0)


async def test_frames_as_seen_on_a_projector() -> None:
    body = b'{"code": 0, "result": {"timezone": "GMT+0", "time": 1795563364}}'
    link = Recorder(
        [bytes.fromhex("0000005000"), bytes.fromhex("000200520000c800000040"), bytes.fromhex("00030053000040")],
        [body],
    )
    counter = tunnel.Counter()
    response = await tunnel.request(link, tunnel.Method.GET, TIME, counter=counter)
    assert response == tunnel.Response(200, body)
    assert link.written == [
        ("param", TIME.encode()),
        ("control", bytes.fromhex("000000500011")),
        ("control", bytes.fromhex("00020052")),
        ("control", bytes.fromhex("00030053")),
    ]
    assert counter.value == 5

    # The POST that followed, from seq 5.
    sent = b'{"time":1795557978}'
    link = Recorder(
        [
            bytes.fromhex("0005004000"),
            bytes.fromhex("0007004100"),
            bytes.fromhex("000900420000c800000040"),
            bytes.fromhex("000a0043000040"),
        ],
        [body],
    )
    await tunnel.request(link, tunnel.Method.POST, TIME, sent.decode(), counter=counter)
    assert link.written == [
        ("param", TIME.encode()),
        ("control", bytes.fromhex("00050040001100000013")),
        ("param", sent),
        ("control", bytes.fromhex("000700410013")),
        ("control", bytes.fromhex("00090042")),
        ("control", bytes.fromhex("000a0043")),
    ]
    assert counter.value == 12


def test_parse_notification() -> None:
    assert tunnel.parse_notification(bytes.fromhex("000200520000c800000040")) == tunnel.Notification(
        2, 0x52, 0, http_status=200, total_length=64
    )
    assert tunnel.parse_notification(bytes.fromhex("0002005200")).http_status is None  # too short for a status
    assert tunnel.parse_notification(bytes.fromhex("00020052c0")).error == 0xC0
    with pytest.raises(tunnel.TunnelError, match="shorter than 5 bytes"):
        tunnel.parse_notification(bytes.fromhex("00000050"))
    assert tunnel.frame_call(0x10003, 0x53).hex() == "00030053"  # the counter wraps at 16 bits


async def test_get_and_post(client: LSPXClient, fake: FakeTunnel) -> None:
    response = await client.web_request(tunnel.Method.GET, TIME)
    assert response.http_status == 200
    assert json.loads(response.text()) == {"code": 0, "result": {"timezone": "GMT+0", "time": 1782914400}}
    await client.web_request(tunnel.Method.POST, TIME, '{"time":1782918000}')
    assert fake.time == 1782918000
    assert fake.requests == [("GET", TIME, b""), ("POST", TIME, b'{"time":1782918000}')]
    # Counted apart from the commands, which answer in a fraction of that.
    assert (client.stats.commands, client.stats.slowest_reply, client.stats.tunnel_requests) == (0, None, 2)
    assert client.stats.last_tunnel_request["request"] == f"POST {TIME}"
    assert "error" not in client.stats.last_tunnel_request
    assert client.is_connected  # kept for the next command, like any command


async def test_counter_runs_on_across_requests_and_links(
    client: LSPXClient, fake: FakeTunnel, fake_connection: list[FakeBleakClient]
) -> None:
    await client.web_request(tunnel.Method.GET, TIME)
    await client.disconnect()
    seen: list[int] = []
    original = fake.control

    def control(frame: bytes) -> bytes | None:
        seen.append(int.from_bytes(frame[:2], "big"))
        return original(frame)

    fake.control = control  # type: ignore[method-assign]
    await client.web_request(tunnel.Method.GET, TIME)
    assert seen == [5, 7, 8]  # after 0, 2 and 3 on the link before, as the app's counter does
    assert len(fake_connection) == 2


async def test_counter_moves_past_a_failed_step(client: LSPXClient, fake: FakeTunnel) -> None:
    """Its answer may still come; the next request must not take it for one of its own."""
    fake.silent.add(0x52)
    seen: list[int] = []
    original = fake.control

    def control(frame: bytes) -> bytes | None:
        seen.append(int.from_bytes(frame[:2], "big"))
        return original(frame)

    fake.control = control  # type: ignore[method-assign]
    with patch.object(tunnel, "STEP_TIMEOUT", 0.5), pytest.raises(tunnel.TunnelError):
        await client.web_request(tunnel.Method.GET, TIME)
    fake.silent.clear()
    fake.extra = [bytes.fromhex("000200520000c800000040")]  # the late answer to seq 2, on the next link
    response = await client.web_request(tunnel.Method.GET, TIME)
    assert response.http_status == 200
    assert seen == [0, 2, 3, 5, 6]


async def test_several_reads_slow_response_and_re_polls(client: LSPXClient, fake: FakeTunnel) -> None:
    fake.chunk = 10
    fake.response_delay = 0.05
    fake.busy_polls = 2  # the app asks again every second while the response op answers an error
    with patch.object(tunnel, "RETRY_DELAY", 0):
        response = await client.web_request(tunnel.Method.GET, TIME)
    assert json.loads(response.text())["result"]["time"] == 1782914400


async def test_stale_and_foreign_notifications_are_ignored(client: LSPXClient, fake: FakeTunnel) -> None:
    # The last frame of a request before (sent again on subscribing, say), another op, and junk.
    fake.extra = [bytes.fromhex("00030053000040"), bytes.fromhex("0000005300"), bytes.fromhex("0000")]
    response = await client.web_request(tunnel.Method.GET, TIME)
    assert response.http_status == 200


async def test_too_many_errors_while_waiting_for_the_response(client: LSPXClient, fake: FakeTunnel) -> None:
    fake.busy_polls = tunnel.RETRY_LIMIT + 1
    with patch.object(tunnel, "RETRY_DELAY", 0), pytest.raises(tunnel.TunnelError, match="0xC0") as err:
        await client.web_request(tunnel.Method.GET, TIME)
    assert err.value.code == ErrorCode.TIMEOUT
    assert not client.is_connected  # a link in an unknown state is dropped
    fake.busy_polls = 0
    assert (await client.web_request(tunnel.Method.GET, TIME)).http_status == 200


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda f: f.errors.__setitem__(0x50, 0xA0), "error 0xA0 to op 0x50"),
        (lambda f: f.errors.__setitem__(0x53, 0xD0), "error 0xD0 to op 0x53"),
        (lambda f: setattr(f, "empty_reads", True), "empty body chunk, 64 bytes missing"),
        (lambda f: setattr(f, "body", b"x" * (tunnel.MAX_BODY + 1)), "a body of 4097 bytes"),
        (lambda f: setattr(f, "seq_offset", 1), "no answer to op 0x50 within [01] s"),  # seq not echoed
        (lambda f: f.silent.add(0x52), "no answer to op 0x52 within [01] s"),
    ],
    ids=["refused", "body_error", "empty_read", "too_long", "seq_not_echoed", "silent"],
)
async def test_failures(client: LSPXClient, fake: FakeTunnel, setup, message: str) -> None:
    setup(fake)
    with patch.object(tunnel, "STEP_TIMEOUT", 0.5), pytest.raises(tunnel.TunnelError, match=message):
        await client.web_request(tunnel.Method.GET, TIME)
    assert client.stats.last_tunnel_request["error"].startswith("TunnelError: ")
    assert client.stats.last_command_error is None


async def test_no_status_in_the_response() -> None:
    link = Recorder([bytes.fromhex("0000005000"), bytes.fromhex("0002005200")], [])
    with pytest.raises(tunnel.TunnelError, match="no HTTP status"):
        await tunnel.request(link, tunnel.Method.GET, TIME, counter=tunnel.Counter())


async def test_whole_request_timeout(client: LSPXClient, fake: FakeTunnel) -> None:
    fake.response_delay = 0.1
    with (
        patch.object(client_module, "TUNNEL_TIMEOUT", 0.05),
        pytest.raises(tunnel.TunnelError, match="no result within 0 s"),
    ):
        await client.web_request(tunnel.Method.POST, TIME, '{"time":1}')
    await asyncio.sleep(0.1)  # the late notification, into a link already dropped


async def test_bluetooth_timeout_is_not_a_tunnel_error(client: LSPXClient, monkeypatch: pytest.MonkeyPatch) -> None:
    original = FakeBleakClient.write_gatt_char

    async def write(self: FakeBleakClient, char: str, data: bytes, response: bool = True) -> None:
        if char == tunnel.CHAR_PARAM:
            raise TimeoutError
        await original(self, char, data, response)

    monkeypatch.setattr(FakeBleakClient, "write_gatt_char", write)
    with pytest.raises(TimeoutError):
        await client.web_request(tunnel.Method.GET, TIME)
    assert not client.is_connected


async def test_missing_tunnel(client: LSPXClient, projector: FakeProjector) -> None:
    projector.tunnel = None
    with pytest.raises(tunnel.TunnelMissingError):
        await client.web_request(tunnel.Method.GET, TIME)
    assert client.is_connected  # nothing wrong with the link
    assert client.stats.last_tunnel_request["error"].startswith("TunnelMissingError: ")
    assert await client.command(Op.GET_POWER) == b"\x10"


async def test_link_lost_or_closed_during_a_request(
    client: LSPXClient, fake: FakeTunnel, fake_connection: list[FakeBleakClient]
) -> None:
    fake.silent.add(0x52)
    task = asyncio.create_task(client.web_request(tunnel.Method.GET, TIME))
    await asyncio.sleep(0.01)
    fake_connection[-1].drop()
    with pytest.raises(BleakError, match="disconnected during a request"):
        await task

    task = asyncio.create_task(client.web_request(tunnel.Method.GET, TIME))
    await asyncio.sleep(0.01)
    await client.close()  # unloading: the request ends at once
    with pytest.raises(BleakError, match="disconnected during a request"):
        await task


async def test_body_is_made_once_the_link_is_free(client: LSPXClient, fake: FakeTunnel) -> None:
    made: list[bool] = []

    def body() -> str:
        made.append(True)
        return '{"time":7}'

    async with client._lock:  # a poll's read, say
        task = asyncio.create_task(client.web_request(tunnel.Method.POST, TIME, body))
        await asyncio.sleep(0.02)
        assert made == []
    await task
    assert made == [True]
    assert fake.time == 7


async def test_commands_wait_for_a_request(client: LSPXClient, fake: FakeTunnel) -> None:
    """Like the app, a request holds the link: no command lands between its steps."""
    fake.response_delay = 0.1
    request = asyncio.create_task(client.web_request(tunnel.Method.POST, TIME, '{"time":1}'))
    await asyncio.sleep(0.02)
    command = asyncio.create_task(client.command(Op.GET_POWER))
    await asyncio.sleep(0.02)
    assert not command.done()
    await request
    assert await command == b"\x10"


async def test_without_encryption(client: LSPXClient, fake: FakeTunnel, fake_connection) -> None:
    """Refused with access denied (inferred from the app: it needs the bond, like the commands)."""
    await client.command(Op.GET_POWER)
    fake_connection[-1].encrypted = False
    with pytest.raises(tunnel.TunnelError) as err:
        await client.web_request(tunnel.Method.GET, TIME)
    assert err.value.code == ErrorCode.ACCESS_DENIED


async def test_unknown_path(client: LSPXClient, fake: FakeTunnel) -> None:
    response = await client.web_request(tunnel.Method.GET, "/v1/other")
    assert response.text() == '{"code": 404}'
    fake.errors[0x41] = 0x02
    with pytest.raises(tunnel.TunnelError, match="op 0x41"):
        await client.web_request(tunnel.Method.POST, TIME, "{}")
