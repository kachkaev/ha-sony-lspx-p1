"""Framing of the Bluetooth tunnel probe (tools/probes/web_tunnel.py), against docs/protocol.md.

The expected bytes come from the write-up, which was reconstructed from the official app; no projector has
answered a tunnel request yet, so these tests only keep the probe and the write-up in agreement.
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import bleak
import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "probes" / "web_tunnel.py"
_SPEC = importlib.util.spec_from_file_location("web_tunnel", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
wt = importlib.util.module_from_spec(_SPEC)
sys.modules["web_tunnel"] = wt
_SPEC.loader.exec_module(wt)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """No Home Assistant needed."""


def test_request_frames() -> None:
    path = b"/v1/settings/time"
    assert wt.frame_request(0, wt.Method.GET, path).hex() == "000000500011"
    assert wt.frame_request(7, wt.Method.DELETE, b"/v1/settings/timers/3").hex() == "000700580015"
    post_path, body = b"/v1/settings/posterResume", b'{"posterResume":true}'
    assert wt.frame_request(0, wt.Method.POST, post_path, body).hex() == "00000040001900000015"
    assert wt.frame_post_body(2, body).hex() == "000200410015"
    assert wt.frame_call(4, 0x42).hex() == "00040042"
    assert wt.frame_call(0x10003, 0x53).hex() == "00030053"  # a u16 counter wraps


def test_parse_notifications() -> None:
    assert wt.parse_notification(bytes.fromhex("0000005000")) == wt.Notification(0, 0x50, 0)
    ok = wt.parse_notification(bytes.fromhex("000200520000c80000002a"))
    assert (ok.http_status, ok.total_length) == (200, 42)
    assert wt.parse_notification(bytes.fromhex("0002005200")).http_status is None  # too short: status 0, no body
    assert wt.parse_notification(bytes.fromhex("00030053000014")).chunk_length == 20
    assert wt.parse_notification(bytes.fromhex("0003005300")).chunk_length is None
    busy = wt.parse_notification(bytes.fromhex("00020052c0"))
    assert busy.error == 0xC0
    assert "timeout" in busy.describe()
    assert "http_status=200" in ok.describe()
    with pytest.raises(wt.TunnelError, match="shorter"):
        wt.parse_notification(bytes.fromhex("00000050"))
    with pytest.raises(wt.TunnelError, match="op the app does not know"):
        wt.parse_notification(bytes.fromhex("0000006100"))


def test_body_assembler_joins_bytes() -> None:
    text = "Grüße".encode()
    assembler = wt.BodyAssembler(len(text))
    assembler.add(text[:3])  # splits the ü
    assert (assembler.remaining, assembler.complete) == (len(text) - 3, False)
    assembler.add(text[3:])
    assert assembler.complete
    assert assembler.body().decode() == "Grüße"
    with pytest.raises(wt.TunnelError, match="empty body chunk"):
        wt.BodyAssembler(5).add(b"")


class FakeTunnel:
    """The projector's side as docs/protocol.md describes it."""

    def __init__(self, status: int = 200, body: bytes = b"", chunk: int = 20, busy: int = 0) -> None:
        self.status, self.body, self.chunk, self.busy = status, body, chunk, busy
        self.writes: list[tuple[str, str]] = []
        self.param = b""
        self.sent = 0
        self._notes: asyncio.Queue[bytes] = asyncio.Queue()

    async def enable_notifications(self) -> None:
        self.writes.append(("cccd", "0100"))

    async def write(self, char: str, data: bytes) -> None:
        self.writes.append(("cp" if char == wt.CP else "param", data.hex()))
        if char == wt.PARAM:
            self.param = data
            return
        seq, op = struct.unpack_from(">HH", data)
        head = struct.pack(">HHB", seq, op, 0)
        if op in wt.RESPONSE_OPS:
            if self.busy:
                self.busy -= 1
                self._notes.put_nowait(struct.pack(">HHB", seq, op, 0xC0))
                return
            self._notes.put_nowait(head + struct.pack(">HI", self.status, len(self.body)))
        elif op in wt.BODY_OPS:
            self.param = self.body[self.sent : self.sent + self.chunk]
            self.sent += len(self.param)
            self._notes.put_nowait(head + struct.pack(">H", len(self.param)))
        else:
            self._notes.put_nowait(head)

    async def read(self, char: str) -> bytes:
        assert char == wt.PARAM
        return self.param

    async def notification(self) -> bytes:
        return await self._notes.get()

    def drain(self) -> list[bytes]:
        return []


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_get_sequence_and_chunks() -> None:
    body = b'{"status":"200","result":{"time":1782914400,"timezone":"GMT+0"}}'
    fake = FakeTunnel(body=body, chunk=20)
    response = await wt.request(fake, wt.Method.GET, "/v1/settings/time", log=lambda _line: None)
    assert response.http_status == 200
    assert response.body == body
    assert fake.writes[:5] == [
        ("cccd", "0100"),
        ("param", b"/v1/settings/time".hex()),
        ("cp", "000000500011"),
        ("cp", "00020052"),
        ("cp", "00030053"),
    ]
    # 65 bytes in chunks of 20: four body reads, the counter growing by two each time.
    assert [w for w in fake.writes if w[1].endswith("0053")] == [
        ("cp", "00030053"),
        ("cp", "00050053"),
        ("cp", "00070053"),
        ("cp", "00090053"),
    ]
    assert response.next_seq == 11


async def test_post_sequence_with_retries() -> None:
    fake = FakeTunnel(body=b'{"status":"200","result":{"posterResume":true}}', chunk=512, busy=2)
    lines: list[str] = []
    response = await wt.request(
        fake,
        wt.Method.POST,
        "/v1/settings/posterResume",
        '{"posterResume":true}',
        seq=10,
        log=lines.append,
        sleep=_no_sleep,
    )
    assert response.retries == 2
    assert response.text() == '{"status":"200","result":{"posterResume":true}}'
    assert fake.writes == [
        ("cccd", "0100"),
        ("param", b"/v1/settings/posterResume".hex()),
        ("cp", "000a0040001900000015"),
        ("param", b'{"posterResume":true}'.hex()),
        ("cp", "000c00410015"),
        ("cp", "000e0042"),  # error 0xC0: re-sent unchanged
        ("cp", "000e0042"),
        ("cp", "000e0042"),
        ("cp", "000f0043"),
    ]
    assert any("re-poll 2/30" in line for line in lines)


async def test_empty_response_reads_nothing() -> None:
    fake = FakeTunnel(status=204)
    response = await wt.request(fake, wt.Method.DELETE, "/v1/settings/timers/3", log=lambda _line: None)
    assert (response.http_status, response.body, response.next_seq) == (204, b"", 3)
    assert fake.writes[-1] == ("cp", "0002005a")


async def test_gives_up_after_30_re_polls() -> None:
    fake = FakeTunnel(busy=100)
    with pytest.raises(wt.TunnelError, match="after 30 re-polls"):
        await wt.request(fake, wt.Method.GET, "/v1/settings/time", log=lambda _line: None, sleep=_no_sleep)


async def test_wrong_op_in_notification() -> None:
    fake = FakeTunnel()

    async def wrong() -> bytes:
        return bytes.fromhex("0000004000")

    fake.notification = wrong  # type: ignore[method-assign]
    with pytest.raises(wt.TunnelError, match="expected 0x50"):
        await wt.request(fake, wt.Method.GET, "/v1/settings/time", log=lambda _line: None)


def test_refused_paths() -> None:
    assert wt.refused("/v1/settings/firmwareUpdateForce") == "firmware"
    assert wt.refused("/settings/setWifi") == "wifi"
    assert wt.refused("/v1/settings/posterResume") is None
    assert wt.refused("/v1/settings/time") is None


class FakeBleakClient:
    """Just enough of BleakClient for the probe's main(), on top of FakeTunnel."""

    mtu_size = 23

    def __init__(self, tunnel: FakeTunnel) -> None:
        self.tunnel = tunnel
        self.callback: Callable[[str, bytearray], None] | None = None
        char = SimpleNamespace(uuid=wt.CP, properties=["write", "notify"])
        service = SimpleNamespace(characteristics=[char])
        self.services = SimpleNamespace(get_service=lambda _uuid: service)

    async def __aenter__(self) -> FakeBleakClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def read_gatt_char(self, char: str) -> bytes:
        return b"\x00" if char == wt.SECURITY_PROBE else await self.tunnel.read(char)

    async def start_notify(self, _char: str, callback: Callable[[str, bytearray], None]) -> None:
        self.callback = callback

    async def write_gatt_char(self, char: str, data: bytes, response: bool) -> None:
        assert response
        await self.tunnel.write(char, data)
        assert self.callback is not None
        while not self.tunnel._notes.empty():
            self.callback(char, bytearray(self.tunnel._notes.get_nowait()))


async def test_main_runs_a_get(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    tunnel = FakeTunnel(body=b'{"status":"200"}')
    device = SimpleNamespace(address="AA:BB", name="LSPXP1_1A2B")

    async def find(*_args: object, **_kwargs: object) -> object:
        return device

    monkeypatch.setattr(bleak.BleakScanner, "find_device_by_filter", find)
    monkeypatch.setattr(bleak, "BleakClient", lambda *_a, **_k: FakeBleakClient(tunnel))
    monkeypatch.setattr(sys, "argv", ["web_tunnel.py", "get", "/v1/settings/time"])
    assert await wt.main() == 0
    out = capsys.readouterr().out
    assert "-> write  ee00270d 000000500011" in out
    assert "<- notify ee00270d 000200520000c800000010" in out
    assert "HTTP 200, 16 bytes, 0 re-polls" in out
    assert '\n{"status":"200"}\n' in out
    assert out.rstrip().endswith("disconnected")
