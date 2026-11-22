"""Smoke tests for tools/lspx_cli.py: every subcommand against the fake projector and the fake web API.

The tool is outside the integration, so its coverage is not measured (see [tool.coverage.run] in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

import custom_components.sony_lspx_p1 as integration
from custom_components.sony_lspx_p1 import client as client_module
from custom_components.sony_lspx_p1 import protocol, web_api
from custom_components.sony_lspx_p1.client import (
    ControllerNotFoundError,
    OwnControllerError,
    OwnControllerUnknownError,
)
from custom_components.sony_lspx_p1.protocol import BondOp, ErrorCode, Op

from .conftest import ADDRESS, NAME
from .fake_projector import FakeBleakClient, FakeProjector
from .fake_web_api import HOST, FakeWebApi

CLI_PATH = Path(__file__).resolve().parent.parent / "tools" / "lspx_cli.py"


def _load_cli() -> ModuleType:
    """Import the tool so that its ``sony_lspx_p1`` is the package the tests and fakes use.

    The tool puts custom_components on sys.path and imports ``sony_lspx_p1``; a second copy of the package would have
    its own exception classes and its own ``establish_connection`` to patch.
    """
    for name, module in (("", integration), (".client", client_module), (".protocol", protocol), (".web_api", web_api)):
        sys.modules.setdefault(f"sony_lspx_p1{name}", module)
    path = list(sys.path)
    spec = importlib.util.spec_from_file_location("lspx_cli", CLI_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = path
    return module


cli = _load_cli()

MY_ID = 0x1001


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """No Home Assistant needed."""


class FakeClock:
    """Stands in for the tool's time.monotonic and asyncio.sleep, so --watch and --seconds pass at once."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if self.on_sleep:
            self.on_sleep(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=fake.monotonic))
    monkeypatch.setattr(cli, "asyncio", SimpleNamespace(sleep=fake.sleep))
    return fake


@pytest.fixture
def projector() -> FakeProjector:
    """A projector this tool has been added to, as "Home Assistant (dev)"."""
    fake = FakeProjector()
    fake.controllers[MY_ID] = "Home Assistant (dev)"
    fake.my_id = MY_ID
    fake.endpoint = (80, HOST)
    return fake


@pytest.fixture
def api(aioclient_mock: AiohttpClientMocker) -> FakeWebApi:
    return FakeWebApi(aioclient_mock)


@pytest.fixture
def lookups() -> list[str]:
    """One entry per device lookup (a scan, on a real computer)."""
    return []


@pytest.fixture
def run(
    clock: FakeClock,
    fake_connection: list[FakeBleakClient],
    aioclient_mock: AiohttpClientMocker,
    lookups: list[str],
    capsys: pytest.CaptureFixture[str],
) -> Callable[..., Awaitable[str]]:
    """Run the tool with these arguments against the fakes; returns what it printed."""

    async def find_device() -> BLEDevice:
        lookups.append("find")
        return BLEDevice(ADDRESS, NAME, details=None)

    def http_session() -> Any:
        return aioclient_mock.create_session(asyncio.get_running_loop())

    async def run(*argv: str) -> str:
        capsys.readouterr()
        try:
            await cli.main(list(argv), find_device=find_device, http_session=http_session)
        finally:
            out = capsys.readouterr().out
            print(out)  # shown when a test fails
        # The tool always disconnects at the end.
        assert not any(link.is_connected for link in fake_connection)
        return out

    return run


@pytest.fixture
def no_debug_logging() -> Generator[None]:
    """listen turns on the client's debug logging; put it back afterwards."""
    logger = logging.getLogger("sony_lspx_p1.client")
    level = logger.level
    yield
    logger.setLevel(level)


# --- Bluetooth commands ---------------------------------------------------------


async def test_scan(run: Callable[..., Awaitable[str]], fake_connection: list[FakeBleakClient]) -> None:
    assert await run("scan") == f"{ADDRESS} {NAME}\n"
    assert not fake_connection  # a scan does not connect


@pytest.mark.parametrize("found", [True, False])
async def test_find(monkeypatch: pytest.MonkeyPatch, found: bool) -> None:
    """The real lookup: the first device whose name starts as the projector's does."""
    devices = [BLEDevice("11:22:33:44:55:66", "Speaker", details=None), BLEDevice(ADDRESS, NAME, details=None)]

    async def find_device_by_filter(match: Callable[[BLEDevice, Any], bool], **kwargs: Any) -> BLEDevice | None:
        adv = SimpleNamespace(local_name=None)
        return next((d for d in devices if match(d, adv)), None) if found else None

    monkeypatch.setattr(cli, "BleakScanner", SimpleNamespace(find_device_by_filter=find_device_by_filter))
    if found:
        assert (await cli.find()).address == ADDRESS
    else:
        with pytest.raises(SystemExit, match="projector not found"):
            await cli.main(["state"])


async def test_state(run: Callable[..., Awaitable[str]]) -> None:
    out = await run("state")
    assert '"power": true' in out
    assert '"volume": 67' in out
    assert re.search(r"^read in \d+\.\ds$", out, re.MULTILINE)


@pytest.mark.parametrize(
    ("name", "value", "attr", "expected"),
    [
        ("volume", "40", "volume", 40),
        ("xloud", "off", "xloud", 0),
        ("brightness", "70", "brightness", 70),
        ("keystone", "-3", "keystone", -3),
        ("picture_mode", "1", "picture_mode", 1),
        ("picture_contrast", "40", "picture", {"brightness": 128, "contrast": 40, "saturation": 88, "sharpness": 7}),
    ],
)
async def test_set(
    run: Callable[..., Awaitable[str]], projector: FakeProjector, *, name: str, value: str, attr: str, expected: Any
) -> None:
    out = await run("set", name, value)
    assert getattr(projector, attr) == expected
    assert out.startswith("-> ")


async def test_power(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    out = await run("power", "off", "--watch", "2")
    assert projector.power == 0
    lines = out.splitlines()
    assert lines[0] == "  0.0s  set power off: ACK"
    assert lines[1:] == ["  1.0s  power=standby  volume=67", "  2.0s  power=standby  volume=67"]


async def test_restrict(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    assert await run("restrict") == "False\n"
    assert await run("restrict", "on") == "-> True\n"
    assert projector.restrict == 1


async def test_controllers(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.blocked.add(0)
    out = await run("controllers")
    assert out.splitlines() == [
        "    0  blocked  iPhone",
        f"{MY_ID:5d}  allowed  Home Assistant (dev)  (this controller)",
    ]


async def test_controllers_without_own_id(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.my_id = None
    out = await run("controllers")
    assert out.splitlines()[0].startswith("this controller's id: BondCommandFailed")
    assert "(this controller)" not in out


@pytest.mark.parametrize(
    ("cmd", "op", "after"),
    [
        ("remove", BondOp.DELETE_CONTROLLER, f"{MY_ID:5d}  allowed  Home Assistant (dev)"),
        ("block", BondOp.BLOCK_CONTROLLER, "    0  blocked  iPhone"),
        ("allow", BondOp.ALLOW_CONTROLLER, "    0  allowed  iPhone"),
    ],
)
async def test_manage_controller(
    run: Callable[..., Awaitable[str]], projector: FakeProjector, cmd: str, op: BondOp, after: str
) -> None:
    projector.blocked.add(0)
    out = await run(cmd, "0")
    assert op in projector.bond_ops
    assert out.splitlines()[0] == after


@pytest.mark.parametrize("cmd", ["remove", "block"])
async def test_never_cuts_off_its_own_entry(
    run: Callable[..., Awaitable[str]], projector: FakeProjector, cmd: str
) -> None:
    with pytest.raises(OwnControllerError):
        await run(cmd, str(MY_ID))
    assert projector.controllers == {0: "iPhone", MY_ID: "Home Assistant (dev)"}
    assert not projector.blocked
    assert not set(projector.bond_ops) & {BondOp.DELETE_CONTROLLER, BondOp.BLOCK_CONTROLLER}


@pytest.mark.parametrize("cmd", ["remove", "block"])
async def test_changes_nothing_without_knowing_its_own_entry(
    run: Callable[..., Awaitable[str]], projector: FakeProjector, cmd: str
) -> None:
    projector.failing_bond_ops.add(BondOp.GET_MY_ID)
    with pytest.raises(OwnControllerUnknownError):
        await run(cmd, "0")
    assert projector.controllers == {0: "iPhone", MY_ID: "Home Assistant (dev)"}
    assert not set(projector.bond_ops) & {BondOp.DELETE_CONTROLLER, BondOp.BLOCK_CONTROLLER}


async def test_manage_unknown_controller(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    with pytest.raises(ControllerNotFoundError):
        await run("remove", "77")
    assert len(projector.controllers) == 2


async def test_manage_controller_needs_a_numeric_id(
    run: Callable[..., Awaitable[str]], lookups: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        await run("remove", "iPhone")
    assert exc.value.code == 2
    assert not lookups


@pytest.mark.parametrize(("theme", "poster"), [("Clock 3", 7), ("night dreams", 5), ("13", 13)])
async def test_poster(run: Callable[..., Awaitable[str]], projector: FakeProjector, theme: str, poster: int) -> None:
    out = await run("poster", theme, "--watch", "4")
    assert projector.poster == poster
    lines = out.splitlines()
    assert lines[0] == "  0.0s  poster: accepted"
    assert lines[1] == f"  2.0s  {{'app_state': 16, 'poster': {poster}, 'net_topology': 17}}"
    assert len(lines) == 3


async def test_hdmi(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.topology, projector.app_state = 0x11, 0x10
    out = await run("hdmi", "--watch", "2")
    assert projector.topology == 0x30
    assert out.splitlines()[0] == "  0.0s  hdmi: accepted"
    assert "'net_topology': 48" in out.splitlines()[1]


async def test_hdmi_watch_reports_failed_reads(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.rejections[Op.GET_POSTER] = ErrorCode.UNKNOWN_COMMAND
    out = await run("hdmi", "--watch", "2")
    assert "'poster': 'error: " in out


async def test_watch_hdmi(run: Callable[..., Awaitable[str]], projector: FakeProjector, clock: FakeClock) -> None:
    def unplug(_seconds: float) -> None:
        if clock.now >= 1002:
            projector.hdmi = [1, 0]

    clock.on_sleep = unplug
    out = await run("watch-hdmi", "--seconds", "5")
    assert out.splitlines() == [
        "   0.0s  0104  enabled=1 status=CONNECTED  app=HDMI",
        "   3.0s  0100  enabled=1 status=DISCONNECTED  app=HDMI",
    ]


async def test_watch_hdmi_reports_errors(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.hdmi = [1, 99]  # a status the app does not name
    out = await run("watch-hdmi", "--seconds", "1")
    assert out.splitlines() == ["   0.0s  0163  enabled=1 status=99  app=HDMI"]
    projector.busy = True
    out = await run("watch-hdmi", "--seconds", "1")
    assert out.splitlines()[0].startswith("   0.0s  error: ")


@pytest.mark.usefixtures("no_debug_logging")
async def test_listen(
    run: Callable[..., Awaitable[str]], clock: FakeClock, fake_connection: list[FakeBleakClient]
) -> None:
    clock.on_sleep = lambda _seconds: fake_connection[-1].notify(bytes.fromhex("01800000"))  # power to standby
    out = await run("listen", "--seconds", "30")
    lines = out.splitlines()
    assert lines[0] == "power: True"
    assert lines[1].startswith("listening for 30 s")
    assert lines[2].startswith("  -> ")
    assert "power" in lines[2].lower()
    assert lines[3] == "power: True"


async def test_weather_read(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.weather = 3
    out = await run("weather")
    assert out == "showing {'power': True, 'app_state': 96, 'poster': 10}; weather code before: 3\n"


async def test_weather_send(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    out = await run("weather", "7")
    assert projector.weather == 7
    lines = out.splitlines()
    assert lines[1] == "  0.0s  set weather 7: ACK"
    assert lines[2] == "weather code after: 7"
    assert "note:" not in out


async def test_weather_outside_the_apps_codes(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.rejections[Op.GET_WEATHER] = ErrorCode.UNKNOWN_COMMAND
    projector.rejections[Op.GET_POSTER] = ErrorCode.UNKNOWN_COMMAND
    out = await run("weather", "12")
    assert "'poster': 'error: " in out
    assert "weather code before: CommandRejected" in out
    assert "note: 12 is not one of the app's codes (1-11), sending anyway" in out
    assert projector.weather == 12


async def test_endpoint(run: Callable[..., Awaitable[str]]) -> None:
    out = await run("endpoint")
    assert out.splitlines() == [
        f"raw 0050{HOST.encode().hex()}: port 80, address '{HOST}', usable host {HOST}",
        "network topology 48 (WIRELESS_HDMI)",
    ]


async def test_endpoint_off_wifi(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.endpoint = (80, "0.0.0.0")
    projector.topology = 0x99  # not one the app names
    out = await run("endpoint")
    assert out.splitlines()[1] == "network topology 153"


async def test_reboot(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    out = await run("reboot", "--watch", "10")
    assert [op for op, _ in projector.log].count(Op.REBOOT) == 1
    assert out.splitlines() == ["  0.0s  reboot: link dropped", "  2.0s  answering again, power=on"]


async def test_reboot_waits_for_the_projector(
    run: Callable[..., Awaitable[str]], projector: FakeProjector, clock: FakeClock
) -> None:
    # The main board answers TIMEOUT while it starts again, until 4 s after the reboot.
    clock.on_sleep = lambda seconds: setattr(projector, "busy", clock.now + seconds < 1004)
    out = await run("reboot", "--watch", "10")
    lines = out.splitlines()
    assert lines[0] == "  0.0s  reboot: link dropped"
    assert lines[1].startswith("  2.0s  power read: ProjectorBusyError: ")
    assert lines[2:] == ["  4.0s  answering again, power=on"]
    assert [op for op, _ in projector.log].count(Op.REBOOT) == 1


async def test_reboot_gives_up_after_watch(
    run: Callable[..., Awaitable[str]], projector: FakeProjector, clock: FakeClock
) -> None:
    clock.on_sleep = lambda _seconds: setattr(projector, "busy", True)
    out = await run("reboot", "--watch", "4")
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[1].startswith("  2.0s  power read: ProjectorBusyError: ")
    assert lines[2].startswith("  4.0s  power read: ProjectorBusyError: ")
    assert [op for op, _ in projector.log].count(Op.REBOOT) == 1


async def test_reboot_refused(run: Callable[..., Awaitable[str]], projector: FakeProjector) -> None:
    projector.rejections[Op.REBOOT] = ErrorCode.UNKNOWN_COMMAND
    with pytest.raises(protocol.CommandRejected):
        await run("reboot")
    assert [op for op, _ in projector.log].count(Op.REBOOT) == 1  # not retried


# --- over Wi-Fi ---------------------------------------------------------------


async def test_get_time_with_host(
    run: Callable[..., Awaitable[str]], api: FakeWebApi, fake_connection: list[FakeBleakClient], lookups: list[str]
) -> None:
    out = await run("get-time", "--host", HOST)
    assert out == "projector clock: 1970-01-01 00:00:00 (0), timezone 'GMT+0'\n"
    assert api.calls("GET", web_api.TIME_PATH) == [None]
    assert not lookups  # no Bluetooth needed
    assert not fake_connection


async def test_get_time_over_bluetooth(run: Callable[..., Awaitable[str]], api: FakeWebApi) -> None:
    out = await run("get-time")
    assert out.splitlines() == [
        f"endpoint: port 80, address '{HOST}' (the app always uses port 80)",
        "projector clock: 1970-01-01 00:00:00 (0), timezone 'GMT+0'",
    ]


async def test_get_time_off_wifi(run: Callable[..., Awaitable[str]], projector: FakeProjector, api: FakeWebApi) -> None:
    projector.endpoint = (80, "0.0.0.0")
    with pytest.raises(SystemExit, match="no address on a Wi-Fi network"):
        await run("get-time")
    assert not api.requests


async def test_get_time_failing(run: Callable[..., Awaitable[str]], api: FakeWebApi) -> None:
    api.fail[f"GET {web_api.TIME_PATH}"] = 500
    out = await run("get-time", "--host", HOST)
    assert out.startswith(f"projector clock: GET http://{HOST}/v1/settings/time failed: ")


async def test_set_time(run: Callable[..., Awaitable[str]], api: FakeWebApi) -> None:
    out = await run("set-time", "--host", HOST, "--shift", "90")
    (body,) = api.calls("POST", web_api.TIME_PATH)
    lines = out.splitlines()
    assert lines[1].startswith("local time here: ")
    assert lines[1].endswith(f"({body['time']}) (shifted by 90.0 min)")
    assert lines[2].startswith(f"POST http://{HOST}/v1/settings/time: accepted in 0.0s; reply ")
    assert lines[-1] == "Check the clock poster (Clock 1-4) on the projector."


async def test_set_time_failing(run: Callable[..., Awaitable[str]], api: FakeWebApi) -> None:
    api.fail[f"POST {web_api.TIME_PATH}"] = 500
    with pytest.raises(SystemExit, match=f"POST http://{HOST}/v1/settings/time failed after 0.0s"):
        await run("set-time", "--host", HOST)


async def test_get_settings(run: Callable[..., Awaitable[str]], api: FakeWebApi) -> None:
    api.add_timer(web_api.TIMER_ON, 7 * 3600)
    out = await run("get-settings")
    assert f"GET http://{HOST}/v1/settings/timers: HTTP 200" in out
    assert f"GET http://{HOST}/v1/settings/posterResume: HTTP 200" in out
    assert f"GET http://{HOST}/v2/poster/duration: HTTP 200" in out
    assert "  timer id1: timer_on on at 1970-01-01 07:00:00 (25200)" in out
    assert "timer_on: on, daily at 07:00" in out
    assert "timer_off: off" in out
    assert {method for method, _, _ in api.requests} == {"GET"}  # read only


async def test_get_settings_failing(
    run: Callable[..., Awaitable[str]], api: FakeWebApi, lookups: list[str], fake_connection: list[FakeBleakClient]
) -> None:
    api.fail[f"* {web_api.TIMERS_PATH}"] = TimeoutError()
    out = await run("get-settings", "--host", HOST)
    assert f"GET http://{HOST}/v1/settings/timers failed: " in out
    assert "timers: " in out
    assert not lookups
    assert not fake_connection
