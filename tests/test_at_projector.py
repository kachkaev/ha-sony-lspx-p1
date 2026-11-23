"""tools/at_projector.py, the guided session at the projector, against the fake projector.

Above all: whatever happens (every step run, Ctrl-C, a lost link), the projector ends as it started, nothing is sent
that the session must never send (volume, Restart, pairing, changes to "Smartphones added", which it only reads).
Like tests/test_lspx_cli.py, these do not count towards the integration's coverage.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import struct
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

import custom_components.sony_lspx_p1 as integration
from custom_components.sony_lspx_p1 import client as client_module
from custom_components.sony_lspx_p1 import web_api
from custom_components.sony_lspx_p1.client import make_ble_device
from custom_components.sony_lspx_p1.protocol import (
    BondOp,
    NetTopology,
    Op,
    Poster,
    encode_bond_activation,
    encode_command,
)

from . import fake_projector
from .conftest import ADDRESS, NAME
from .fake_projector import FakeBleakClient, FakeProjector

TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "at_projector.py"


def _load_tool() -> ModuleType:
    """Import the tool so that its ``sony_lspx_p1`` is the package the tests and fakes use (as test_lspx_cli does)."""
    for name, module in list(sys.modules.items()):
        if name == integration.__name__ or name.startswith(f"{integration.__name__}."):
            sys.modules[name.removeprefix("custom_components.")] = module
    path = list(sys.path)
    spec = importlib.util.spec_from_file_location("at_projector", TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up there
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = path
    assert module.LSPXClient is client_module.LSPXClient
    return module


at = _load_tool()

pytestmark = pytest.mark.no_cover

MY_ID = 0x1001  # this computer's entry, registered beforehand with tools/lspx_cli.py pair


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """No Home Assistant needed."""


@pytest.fixture(autouse=True)
def no_copy_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake never repeats replies: nothing to wait for before sending an opcode again."""
    monkeypatch.setattr(client_module, "DUPLICATE_WINDOW", 0)


class FakeClock:
    """The session's waits pass at once (or, with ``real``, take that many real seconds per wait at most)."""

    real = 0.0

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(min(seconds, self.real))


Reply = str | Callable[[], Any]


class ScriptedConsole:
    """Answers the session's questions in order. Each answer is checked against a fragment of the question."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.script: list[tuple[str, Reply]] = []
        self.during_countdowns: list[str] = []  # typed during the countdowns, one each, in order

    def expect(self, fragment: str, reply: Reply = "") -> ScriptedConsole:
        self.script.append((fragment, reply))
        return self

    def say(self, text: str) -> None:
        self.said.append(text)

    async def waiting_line(self) -> str:
        if self.during_countdowns:
            await asyncio.sleep(0)
            return self.during_countdowns.pop(0)
        await asyncio.Event().wait()  # nothing typed: the countdown runs out
        raise AssertionError("unreachable")

    async def line(self, prompt: str) -> str:
        for _ in range(50):  # let background reads (the HDMI watch) run, as a person takes a while to answer
            await asyncio.sleep(0)
        # The last question (background reads may have said something since).
        question = next(t for t in reversed(self.said) if "(s: skip" in t or t.startswith("Type one of"))
        if not self.script:
            raise AssertionError(f"no answer scripted for: {question}")
        fragment, reply = self.script.pop(0)
        assert fragment in question, f"expected a question with {fragment!r}, got: {question}"
        if callable(reply):
            reply = reply()
            if inspect.isawaitable(reply):
                reply = await reply
        return str(reply)

    def text(self) -> str:
        return "\n".join(self.said)


class FakeTunnel:
    """The projector's clock behind the Bluetooth tunnel (never tried on a real one), as docs/protocol.md has it."""

    def __init__(self, offset: int = -300) -> None:
        self.offset = offset  # the projector's clock minus this computer's local time, in seconds
        self.requests: list[tuple[str, str | None]] = []

    async def __call__(self, method: Any, body: str | None) -> Any:
        now = web_api.local_unix_seconds(datetime.now().astimezone())
        self.requests.append((method.name, body))
        if method.name == "POST":
            assert body is not None
            self.offset = json.loads(body)["time"] - now
        reply = json.dumps({"status": "200", "result": {"time": now + self.offset, "timezone": "GMT+0"}})
        return at.web_tunnel.Response(200, reply.encode(), 0, 0)


@pytest.fixture
def projector() -> FakeProjector:
    """Registered with this computer (tools/lspx_cli.py pair); on, showing HDMI, as tests/fake_projector.py starts."""
    fake = FakeProjector()
    fake.controllers[MY_ID] = "Home Assistant (dev)"
    fake.my_id = MY_ID
    fake.weather = 5  # what the one real projector held (the fake's 0 is none of the app's codes)
    return fake


@pytest.fixture
def found() -> list[bool]:
    """Whether each scan finds the projector (True once the list runs out)."""
    return []


@pytest.fixture
def run(tmp_path: Path, fake_connection: list[FakeBleakClient], found: list[bool]) -> Callable[..., Awaitable[Any]]:
    async def find_device() -> BLEDevice | None:
        if found and not found.pop(0):
            return None
        return make_ble_device(ADDRESS, NAME)

    async def run(console: ScriptedConsole, *argv: str, tunnel: FakeTunnel | None = None) -> Any:
        session = await at.main(
            ["--results", str(tmp_path / "results"), *argv],
            console=console,
            find_device=find_device,
            clock=FakeClock(),
            tunnel_request=tunnel or FakeTunnel(),
        )
        assert not console.script, f"answers left over: {console.script}"
        assert not any(link.is_connected for link in fake_connection)  # the session lets go of the link at the end
        return session

    return run


def snapshot(projector: FakeProjector) -> dict[str, Any]:
    """What the session may change and must put back."""
    return {
        "power": projector.power,
        "app_state": projector.app_state,
        "topology": projector.topology,
        "poster": projector.poster,
        "weather": projector.weather,
        "direction": projector.direction,
        "auto_direction": projector.auto_direction,
        "auto_standby": list(projector.auto_standby),
        "volume": projector.volume,
        "restrict": projector.restrict,
        "blocked": set(projector.blocked),
        "controllers": dict(projector.controllers),
    }


def sent_ops(projector: FakeProjector) -> set[str]:
    return {Op(op).name for op, _ in projector.log}


def results_of(tmp_path: Path) -> dict[str, Any]:
    (directory,) = (tmp_path / "results").iterdir()
    return json.loads((directory / "results.json").read_text())


def ready(console: ScriptedConsole) -> ScriptedConsole:
    return console.expect("Ready")


# --- a whole session ------------------------------------------------------------------------------------------


async def test_whole_session_puts_everything_back(
    run: Callable[..., Awaitable[Any]],
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
    tmp_path: Path,
) -> None:
    projector.poster = 5  # Night dreams, the poster it showed before HDMI: put back too
    before = snapshot(projector)
    tunnel = FakeTunnel(offset=-300)

    def press(power: int) -> str:
        projector.power = power
        fake_connection[-1].notify(bytes([1, 0x80, 0x00, power]))  # as docs/protocol.md expects one to look
        return ""

    def hdmi(status: int, reply: str) -> Callable[[], str]:
        def plug() -> str:
            projector.hdmi = [1, status]
            return reply

        return plug

    console = ready(ScriptedConsole())
    # Weather
    console.expect("Start?").expect("Code 7", "y").expect("Code 11", "y").expect("Code 1 ", "o more like fog")
    console.expect("code 3", "y").expect("code 4", "y").expect("code 9", "o snowflakes")
    # Clock
    console.expect("Start?").expect("Does Clock 1 show", "y").expect("90 minutes back over tunnel")
    console.expect("jump 90 minutes", "y").expect("back to the time", "y")
    # Direction
    console.expect("Start?").expect("Auto picture direction is now off", "n")
    console.expect("Direction 1", "u").expect("Direction 0", "h").expect("Direction 3", "v")
    # Notifications
    console.expect("Start?").expect("lightly once", lambda: press(0)).expect("again", lambda: press(0x10))
    console.expect("come back on", "y")
    # HDMI
    console.expect("Start?").expect("HDMI source at hand", "y")
    console.expect("What happened", hdmi(1, "u")).expect("What happened", hdmi(4, "p")).expect("What happened", "d")

    session = await run(console, tunnel=tunnel)

    assert snapshot(projector) == before  # "Smartphones added" too
    assert abs(tunnel.offset - -300) <= 2  # the clock is back as it was
    assert [m for m, _ in tunnel.requests].count("POST") == 2
    assert "SET_VOLUME" not in sent_ops(projector)
    assert "REBOOT" not in sent_ops(projector)
    # "Smartphones added" is only read: no pairing, no change to the list or its restriction.
    assert {BondOp(op).name for op in projector.bond_ops} <= {"GET_PIN_CODE_FLAG"}
    assert not projector.bonding_open

    data = results_of(tmp_path)
    assert {k: v["status"] for k, v in data["steps"].items()} == dict.fromkeys(
        ("setup", *at.STEP_KEYS, "finish"), "done"
    )
    assert data["pending_restores"] == []
    assert data["differences"] == []
    assert data["finished"]
    events = data["steps"]
    assert [e["code"] for e in events["weather"]["events"] if e["kind"] == "set-weather"] == [7, 11, 1, 3, 4, 9]
    assert [e["data"] for e in events["notifications"]["events"] if e["kind"] == "notification"] == ["00", "10"]
    assert any(e["kind"] == "hdmi-status" for e in events["hdmi"]["events"])
    assert not session.client.is_connected

    # The raw frames are kept, and the shareable copy leaves out the address, the projector's and the phone's names.
    directory = session.results.directory
    frames = [json.loads(line) for line in (directory / "frames.jsonl").read_text().splitlines()]
    assert {"command", "bonding", "smartphones"} <= {f["char"] for f in frames}
    assert "bonding-name" not in {f["char"] for f in frames}
    assert any(f["what"] == "SET_WEATHER (0x0069)" for f in frames)
    share = (directory / "shareable.md").read_text()
    assert "## Weather poster pictures (#2)" in share
    assert "more like fog" in share
    for private in (ADDRESS, NAME, "iPhone"):
        assert private not in share
    assert "Home Assistant (dev)" in share  # generic names stay
    assert "Give the connection back" in console.text()


# --- putting things back ------------------------------------------------------------------------------------------


async def test_ctrl_c_puts_back_what_the_step_changed(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True  # registered before: no pairing
    before = snapshot(projector)

    async def ctrl_c() -> str:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()  # what asyncio.run does on the first Ctrl-C
        await asyncio.sleep(0)
        raise AssertionError("not cancelled")

    console = ready(ScriptedConsole()).expect("Start?").expect("Auto picture direction is now off", "n")
    console.expect("Direction 1", ctrl_c)
    await run(console, "--step", "direction")

    assert projector.direction != 1
    assert snapshot(projector) == before  # direction and auto direction, and HDMI again (for Clock 1)
    data = results_of(tmp_path)
    assert data["steps"]["direction"]["status"] == "interrupted"
    assert data["pending_restores"] == []
    assert not data["finished"]
    assert "Interrupted: putting back" in console.text()


async def test_lost_link_leaves_it_for_restore_only(
    run: Callable[..., Awaitable[Any]],
    projector: FakeProjector,
    fake_connection: list[FakeBleakClient],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    projector.bonded = True
    before = snapshot(projector)
    connect = client_module.establish_connection

    def lose_the_link() -> str:
        async def unreachable(*args: Any, **kwargs: Any) -> Any:
            raise BleakError("device not found")

        monkeypatch.setattr(client_module, "establish_connection", unreachable)
        fake_connection[-1].drop()
        return "q"

    console = ready(ScriptedConsole()).expect("Start?").expect("Auto picture direction is now off", "n")
    console.expect("Direction 1", lose_the_link)
    await run(console, "--step", "direction")

    assert projector.direction == 1  # could not be put back
    data = results_of(tmp_path)
    assert {p["kind"] for p in data["pending_restores"]} == {"direction", "auto_direction", "source"}
    assert "NOT PUT BACK: picture direction: back to 2" in console.text()
    assert "--restore-only" in console.text()

    monkeypatch.setattr(client_module, "establish_connection", connect)
    again = ready(ScriptedConsole())
    await run(again, "--restore-only")
    assert snapshot(projector) == before
    assert results_of(tmp_path)["pending_restores"] == []
    assert "Putting back what an earlier run left changed" in again.text()
    assert not results_of(tmp_path)["finished"]  # --restore-only leaves the session to be continued

    later = ready(ScriptedConsole()).expect("Start?", "q")
    await run(later, "--step", "direction")
    assert "Continuing the session in" in later.text()


async def test_a_new_session_puts_back_first(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """--new carries over what the last session left changed, and puts it back before reading the projector."""
    projector.bonded = True
    session_dir = tmp_path / "results" / "2026-01-01-000000"
    session_dir.mkdir(parents=True)
    old = at.Results(session_dir)
    old.data["pending_restores"] = [{"kind": "direction", "value": 2, "label": "picture direction", "scope": "step"}]
    old.save()
    projector.direction = 0

    console = ready(ScriptedConsole()).expect("Start?", "q")
    await run(console, "--new", "--step", "direction")
    assert projector.direction == 2
    new_dir = max((tmp_path / "results").iterdir())
    new = json.loads((new_dir / "results.json").read_text())
    assert new["baseline"]["state"]["direction"] == 2  # read after putting it back
    assert json.loads((session_dir / "results.json").read_text())["pending_restores"] == []


# --- the link -------------------------------------------------------------------------------------------------------


async def test_says_how_to_free_a_busy_link(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, found: list[bool], tmp_path: Path
) -> None:
    projector.bonded = True
    found += [False, False]
    console = ready(ScriptedConsole()).expect("Try again?").expect("Try again?").expect("Start?", "q")
    await run(console, "--step", "weather")
    assert console.text().count("Cannot reach the projector") == 2
    assert "the three dots -> Disable" in console.text()
    assert [e["kind"] for e in results_of(tmp_path)["steps"]["setup"]["events"]][:2] == ["answer", "not-found"]


async def test_resumes_the_last_session(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True
    console = (
        ready(ScriptedConsole()).expect("Start?").expect("Code 7", "n").expect("Code 11", "n").expect("Code 1 ", "n")
    )
    console.expect("now show", "n").expect("Start?", "q")
    await run(console, "--step", "weather,direction")
    assert results_of(tmp_path)["steps"]["weather"]["status"] == "done"
    assert projector.weather == 5  # put back

    again = ready(ScriptedConsole()).expect("Start?", "s")
    await run(again, "--step", "weather,direction")
    assert "Continuing the session in" in again.text()
    assert "=== Weather" not in again.text()  # done already
    data = results_of(tmp_path)
    assert data["steps"]["direction"]["status"] == "skipped"
    assert not data["finished"]  # clock, notifications and hdmi are still to come


# --- what the session must never do -----------------------------------------------------------------------------


def test_refuses_what_it_must_never_send() -> None:
    client = at.SessionClient(make_ble_device(ADDRESS, NAME), lambda *_: None)
    refused = [
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_VOLUME, b"\x28")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_XLOUD, b"\x00")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.REBOOT)),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_NET_TOPOLOGY, b"\x20")),  # Wi-Fi Direct
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_AUTO_STANDBY, b"\x02\x01\x00")),  # smartphone detection
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.DELETE_CONTROLLER, 0x10, 0x01])),
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.BLOCK_CONTROLLER, 0x10, 0x01])),
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.SET_PIN_CODE_FLAG, 1])),
        (fake_projector.CHAR_BOND_CONTROL, encode_bond_activation()),  # never pairs
        (fake_projector.CHAR_BONDING_NAME, b"x"),
        (at.TUNNEL_PARAM, b"/settings/setWifi"),
        (at.TUNNEL_PARAM, b'{"time":1,"ssid":"x"}'),
        (at.TUNNEL_CP, bytes.fromhex("000000580011")),  # DELETE
    ]
    for char, data in refused:
        with pytest.raises(at.UnsafeCommand):
            client.check(char, data)
    allowed = [
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_DIRECTION, b"\x01")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.GET_VOLUME)),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_NET_TOPOLOGY, b"\x30")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_NET_TOPOLOGY, b"\x11")),
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.GET_PIN_CODE_FLAG])),
        (at.TUNNEL_PARAM, web_api.TIME_PATH.encode()),
        (at.TUNNEL_PARAM, b'{"time":1}'),
        (at.TUNNEL_CP, bytes.fromhex("000000500011")),
    ]
    for char, data in allowed:
        client.check(char, data)


async def test_refused_before_it_is_sent(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step that tried to change the volume fails, and nothing reaches the projector."""
    projector.bonded = True

    async def louder(self: Any) -> None:
        await self.c.set_volume(100)

    monkeypatch.setattr(at.Session, "step_weather", louder)
    console = ready(ScriptedConsole()).expect("Start?")
    await run(console, "--step", "weather")
    assert projector.volume == 67
    assert "SET_VOLUME" not in sent_ops(projector)
    assert "The step failed: UnsafeCommand: refusing to send SET_VOLUME (0x0200)" in console.text()


# --- steps on their own --------------------------------------------------------------------------------------------


async def test_weather_refused(run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path) -> None:
    """A NAK to SetWeather (UNKNOWN_COMMAND, say) is the answer #2 asks for, recorded as such."""
    projector.bonded = True
    projector.rejections[Op.SET_WEATHER] = 0x03
    console = (
        ready(ScriptedConsole()).expect("Start?").expect("Code 7", "n").expect("Code 11", "n").expect("Code 1 ", "n")
    )
    await run(console, "--step", "weather")
    events = results_of(tmp_path)["steps"]["weather"]["events"]
    assert next(e["rejected"] for e in events if e["kind"] == "set-weather").startswith("CommandRejected: ")
    assert "The projector refused every SetWeather" in console.text()
    assert "So SetWeather does not change the picture here" not in console.text()


async def test_clock_without_the_tunnel_service(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real tunnel route, on a projector without the service: recorded, and nothing sent to set the clock."""
    projector.bonded = True
    monkeypatch.setattr(fake_projector._FakeServices, "get_service", lambda self, uuid: None, raising=False)

    async def main(*args: Any, **kwargs: Any) -> Any:
        return await original(*args, **{**kwargs, "tunnel_request": None})

    original = at.main
    monkeypatch.setattr(at, "main", main)
    console = ready(ScriptedConsole()).expect("Start?")
    await run(console, "--step", "clock")
    events = results_of(tmp_path)["steps"]["clock"]["events"]
    tunnel = [e for e in events if e["kind"] == "tunnel"]
    assert tunnel == [tunnel[0]]
    assert "no service" in tunnel[0]["error"]
    assert any(e["kind"] == "wifi" and e["usable"] is False for e in events)
    assert results_of(tmp_path)["steps"]["clock"]["status"] == "done"


# --- redaction --------------------------------------------------------------------------------------------------------


def test_redactor(tmp_path: Path) -> None:
    results = at.Results(tmp_path)
    results.data["projector"] = {"address": "1A2B3C4D-0000-1111-2222-333344445555", "name": NAME}
    results.data["baseline"] = {
        "state": {
            "controllers": [
                {"id": 1, "name": "Alex's iPhone", "allowed": True},
                {"id": 2, "name": "Home Assistant", "allowed": True},
            ]
        }
    }
    redact = at.Redactor(results)
    text = redact.text(
        f"1a2b3c4d-0000-1111-2222-333344445555 {NAME} AA:BB:CC:DD:EE:FF Alex's iPhone Home Assistant"
        " 192.168.1.20 192.168.8.100 0.0.0.0 3.1.15"
    )
    assert text == (
        "<projector address> LSPXP1_xxxx <address> <smartphone 1> Home Assistant <home address> <192.168.8.x>"
        " 0.0.0.0 3.1.15"
    )
    # As in bleak's errors on BlueZ (an adapter's address, a device path); the hex frames themselves stay as they are.
    assert redact.text("/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF on 00:1A:7D:DA:71:13") == (
        "/org/bluez/hci0/dev_<address> on <address>"
    )
    endpoint = bytes([2, 0, 0x64, 0, 80]) + b"192.168.1.20"
    shown = redact.frame({"t": 1.0, "step": "clock", "dir": "<-", "char": "command", "hex": endpoint.hex()})
    assert "3139322e" not in shown
    assert "<address: 12 bytes>" in shown
    names = redact.frame({"t": 1.0, "step": "setup", "dir": "<- read", "char": "smartphones", "hex": "0001004869"})
    assert "<5 bytes: smartphone names>" in names


async def test_list_steps() -> None:
    console = ScriptedConsole()
    await at.main(["--list"], console=console)
    text = console.text()
    assert text.startswith("1. weather: Weather poster pictures (#2)")
    assert "5. hdmi: " in text
    assert "detection" not in text


async def test_switched_on_for_the_checks_and_back_to_standby(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projector.bonded = True
    projector.power = 0
    # MAIN_BOARD_STARTING for a while after switching on, as after a long standby; the fake counts real seconds, so
    # the session's waits take a little real time here.
    projector.cold_start = 0.05
    monkeypatch.setattr(FakeClock, "real", 0.01)
    before = snapshot(projector)
    console = (
        ready(ScriptedConsole()).expect("Start?").expect("Code 7", "n").expect("Code 11", "n").expect("Code 1 ", "n")
    )
    console.expect("now show", "n")
    await run(console, "--step", "weather")
    assert snapshot(projector) == before
    assert "Switching the projector on" in console.text()
    assert any(e["kind"] == "main-board-up" for e in results_of(tmp_path)["steps"]["weather"]["events"])


async def test_power_left_in_standby_by_the_button_comes_back_on(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector
) -> None:
    """On at the start: if the power button leaves it in standby, the session switches it on, and keeps it on."""
    projector.bonded = True
    before = snapshot(projector)

    def standby() -> str:
        projector.power = 0
        return ""

    console = ready(ScriptedConsole()).expect("Start?").expect("lightly once", standby).expect("again", "n")
    console.expect("come back on", "n stayed off")
    await run(console, "--step", "notifications")
    assert snapshot(projector) == before


# --- the paths that put things back ---------------------------------------------------------------------------------


def cancel_now() -> Callable[[], Awaitable[str]]:
    """An answer that is a Ctrl-C instead: the task is cancelled, as asyncio.run does on the first one."""

    async def ctrl_c() -> str:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        raise AssertionError("not cancelled")

    return ctrl_c


def cancel_on_restore(monkeypatch: pytest.MonkeyPatch, kinds: list[str]) -> list[str]:
    """Cancel the task (a Ctrl-C) as each change of these kinds starts being put back, once each, in this order."""
    restored: list[str] = []
    original = at.Session._restore

    async def restore(self: Any, change: Any) -> None:
        restored.append(change.kind)
        if kinds and change.kind == kinds[0]:
            kinds.pop(0)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        await original(self, change)

    monkeypatch.setattr(at.Session, "_restore", restore)
    return restored


async def test_ctrl_c_while_putting_back_at_the_end_goes_on(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first Ctrl-C during the end of the session (the switch back to HDMI, say) starts putting back again."""
    projector.bonded = True
    before = snapshot(projector)
    restored = cancel_on_restore(monkeypatch, ["source"])
    console = ready(ScriptedConsole()).expect("Start?").expect("Code 7", "n").expect("Code 11", "n")
    console.expect("Code 1 ", "n").expect("now show", "n")
    session = await run(console, "--step", "weather")
    assert restored.count("source") == 2
    assert snapshot(projector) == before
    assert session.interrupts == 1
    assert results_of(tmp_path)["pending_restores"] == []


async def test_a_second_ctrl_c_stops_without_bluetooth(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Interrupted at a question and again while the step puts back: the rest stays noted for --restore-only."""
    projector.bonded = True
    before = snapshot(projector)
    cancel_on_restore(monkeypatch, ["direction"])
    console = ready(ScriptedConsole()).expect("Start?").expect("Auto picture direction is now off", "n")
    console.expect("Direction 1", cancel_now())
    session = await run(console, "--step", "direction")
    assert session.interrupts == 2
    assert "Interrupted again: stopping" in console.text()
    assert "NOT PUT BACK (run this tool with --restore-only" in console.text()
    pending = {p["kind"] for p in results_of(tmp_path)["pending_restores"]}
    assert pending == {"direction", "auto_direction", "source"}
    assert projector.direction == 1

    monkeypatch.setattr(at.Session, "_restore", ORIGINAL_RESTORE)
    await run(ready(ScriptedConsole()), "--restore-only")
    assert snapshot(projector) == before


async def test_starting_on_a_poster_comes_back_with_its_topology(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector
) -> None:
    """On a poster with topology 16 (as one real projector read): back from HDMI, the topology is 16 again."""
    projector.bonded = True
    projector.app_state, projector.topology, projector.poster = 0x10, 16, 5
    before = snapshot(projector)
    console = ready(ScriptedConsole()).expect("Start?").expect("HDMI source at hand", "y").expect("What happened", "d")
    await run(console, "--step", "hdmi")
    assert NetTopology.WIRELESS_HDMI in [p[0] for op, p in projector.log if op == Op.SET_NET_TOPOLOGY]
    assert snapshot(projector) == before


async def test_a_pending_source_is_put_back_with_the_firmware_of_its_session(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """--new puts back first, before reading the projector: what the change needs is in the change itself."""
    projector.bonded = True
    projector.power = 0  # and in standby: switched on to choose it, then back to standby
    session_dir = tmp_path / "results" / "2026-01-01-000000"
    session_dir.mkdir(parents=True)
    old = at.Results(session_dir)
    source = {"app_state": 0x10, "poster": 5, "net_topology": 17, "software_version": "3.1.15"}
    old.data["pending_restores"] = [
        {"kind": "power", "value": False, "label": "power", "scope": "session"},
        {"kind": "source", "value": source, "label": "source", "scope": "session"},
    ]
    old.save()
    await run(ready(ScriptedConsole()).expect("Start?", "q"), "--new", "--step", "weather")
    assert (projector.power, projector.app_state, projector.poster) == (0, 0x10, 5)
    # Leaving HDMI on firmware 3.x goes back to topology 17, as the app does (16 without the version).
    assert [p[0] for op, p in projector.log if op == Op.SET_NET_TOPOLOGY] == [17]


async def test_the_notifications_step_switches_it_on_again_when_cut_short(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector
) -> None:
    projector.bonded = True
    before = snapshot(projector)

    def standby() -> str:
        projector.power = 0
        return ""

    console = ready(ScriptedConsole()).expect("Start?").expect("lightly once", standby).expect("again", cancel_now())
    await run(console, "--step", "notifications")
    assert snapshot(projector) == before


async def test_an_unknown_weather_code_is_not_put_back(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """0 is none of the app's codes 1-11: the session asks first, and does not keep a change it may never make."""
    projector.bonded = True
    projector.weather = 0
    console = ready(ScriptedConsole()).expect("Start?").expect("cannot be put back exactly", "y")
    console.expect("Code 7", "n").expect("Code 11", "n").expect("Code 1 ", "n").expect("now show", "n")
    await run(console, "--step", "weather")
    data = results_of(tmp_path)
    assert data["pending_restores"] == []
    assert data["steps"]["weather"]["status"] == "done"
    assert [c["label"] for c in data["not_put_back"]] == ["weather code"]


# --- the entry the session made -------------------------------------------------------------------------------------


ORIGINAL_RESTORE = at.Session._restore


# --- from a long standby ----------------------------------------------------------------------------------------------


def weather_answers(console: ScriptedConsole) -> ScriptedConsole:
    console.expect("Start?").expect("Code 7", "n").expect("Code 11", "n").expect("Code 1 ", "n")
    return console.expect("now show", "n")


async def test_from_a_long_standby_the_source_is_read_once_it_is_up(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """The main board sleeps after about 30 minutes of standby: what it shows is read after switching on."""
    projector.bonded = True
    projector.power, projector.main_board_asleep = 0, True
    projector.app_state, projector.poster, projector.topology = 0x10, 5, 16
    before = snapshot(projector)
    await run(weather_answers(ready(ScriptedConsole())), "--step", "weather")
    assert snapshot(projector) == before
    baseline = results_of(tmp_path)["baseline"]["state"]
    assert (baseline["app_state"], baseline["poster"], baseline["net_topology"]) == (0x10, 5, 16)
    assert baseline["software_version"] == "3.1.15"


async def test_a_source_put_back_later_from_standby_ends_in_standby(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In standby at the start; the source could not be put back, the power could: --restore-only switches on to put
    it back, and goes back to standby."""
    projector.bonded = True
    projector.power = 0
    before = snapshot(projector)
    original = at.Session.restore_source

    async def failing(self: Any, value: Any) -> None:
        raise BleakError("the switch timed out")

    monkeypatch.setattr(at.Session, "restore_source", failing)
    await run(weather_answers(ready(ScriptedConsole())), "--step", "weather")
    assert [p["kind"] for p in results_of(tmp_path)["pending_restores"]] == ["source"]
    assert projector.power == 0
    assert projector.poster == int(Poster.WEATHER)

    monkeypatch.setattr(at.Session, "restore_source", original)
    await run(ready(ScriptedConsole()), "--restore-only")
    assert snapshot(projector) == before
    assert results_of(tmp_path)["pending_restores"] == []


# --- review round 3 -----------------------------------------------------------------------------------------------


async def test_ctrl_c_while_switching_on_to_put_back_still_ends_in_standby(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In standby, a pending source is put back by switching on; a Ctrl-C during that start-up puts the power back."""
    projector.bonded = True
    projector.power, projector.app_state, projector.poster = 0, 0x10, 11
    session_dir = tmp_path / "results" / "2026-01-01-000000"
    session_dir.mkdir(parents=True)
    old = at.Results(session_dir)
    source = {"app_state": 0x10, "poster": 5, "net_topology": 17, "software_version": "3.1.15"}
    old.data["pending_restores"] = [{"kind": "source", "value": source, "label": "source", "scope": "session"}]
    old.data["baseline"] = {"state": {"power": False}}
    old.save()
    original = at.Session.wait_until_up
    calls: list[int] = []

    async def wait_until_up(self: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        return await original(self)

    monkeypatch.setattr(at.Session, "wait_until_up", wait_until_up)
    await run(ready(ScriptedConsole()), "--restore-only")
    assert (projector.power, projector.app_state, projector.poster) == (0, 0x10, 5)
    assert json.loads((session_dir / "results.json").read_text())["pending_restores"] == []


async def test_a_source_unknown_at_the_start_is_read_before_it_is_changed(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Started right after switching on: the main board does not answer yet, so the source is read once it is up."""
    projector.bonded = True
    projector.app_state, projector.poster, projector.topology = 0x10, 5, 16
    projector.main_board_up_at = asyncio.get_running_loop().time() + 0.05  # MAIN_BOARD_STARTING until then
    monkeypatch.setattr(FakeClock, "real", 0.01)
    before = snapshot(projector)
    await run(weather_answers(ready(ScriptedConsole())), "--step", "weather")
    assert snapshot(projector) == before
    assert results_of(tmp_path)["baseline"]["state"]["app_state"] == 0x10


async def test_a_source_that_cannot_be_chosen_again_is_asked_about_first(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True
    projector.app_state = 0x20  # the content viewer: not a poster or HDMI
    console = ready(ScriptedConsole()).expect("cannot bring back at the end", "y").expect("Start?")
    console.expect("cannot choose again", "")
    await run(console, "--step", "weather")
    assert projector.app_state == 0x20
    assert (
        results_of(tmp_path)["steps"]["weather"]["status"]
        == "ended early (the source from the start could not be put back)"
    )
    assert [c["label"] for c in results_of(tmp_path)["not_put_back"]] == ["source: content_viewer"]


async def test_a_clock_not_back_is_said(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True

    class StuckTunnel(FakeTunnel):
        async def __call__(self, method: Any, body: str | None) -> Any:
            if method.name == "POST" and self.requests:
                self.requests.append((method.name, body))  # takes the jump, then no other time
                now = web_api.local_unix_seconds(datetime.now().astimezone())
                return at.web_tunnel.Response(200, json.dumps({"result": {"time": now + self.offset}}).encode(), 0, 0)
            return await super().__call__(method, body)

    console = ready(ScriptedConsole()).expect("Start?").expect("Does Clock 1 show", "y").expect("90 minutes back")
    console.expect("jump 90 minutes", "y").expect("back to the time", "n")
    await run(console, "--step", "clock", tunnel=StuckTunnel())
    assert "The clock may not be back as it was" in console.text()
    assert "open Sony's app once" in console.text()
    # Listed at the end and in the shareable copy, and the end check reads the clock again.
    assert "NOT PUT BACK, and the session cannot (by hand):" in console.text()
    data = results_of(tmp_path)
    assert data["not_put_back"][0]["label"] == "clock over tunnel"
    # (Here the tunnel took no write at all, so the clock reads as before: the end check read it again to see.)
    assert any(e["kind"] == "tunnel" for e in data["steps"]["finish"]["events"])
    share = next((tmp_path / "results").iterdir()) / "shareable.md"
    assert "Not put back, and the session cannot:** clock over tunnel" in share.read_text()


async def test_the_real_tunnel_path_against_a_fake_tunnel(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The session's own tunnel code (the exclusive link, the allow-list, the frames), against docs/protocol.md."""
    projector.bonded = True
    clock = {"offset": -120}
    state: dict[str, Any] = {"param": b"", "body": b""}
    cp, param = at.TUNNEL_CP, at.TUNNEL_PARAM
    service = SimpleNamespace(characteristics=[SimpleNamespace(uuid=cp, properties=["write", "notify"])])
    monkeypatch.setattr(fake_projector._FakeServices, "get_service", lambda self, uuid: service, raising=False)
    write, read = FakeBleakClient.write_gatt_char, FakeBleakClient.read_gatt_char

    async def tunnel_write(self: Any, char: str, data: bytes, response: bool = True) -> None:
        if char == param:
            state["param"] = bytes(data)
            return
        if char != cp:
            return await write(self, char, data, response)
        seq, op = struct.unpack(">HH", data[:4])
        reply = struct.pack(">HHB", seq, op, 0)
        now = web_api.local_unix_seconds(datetime.now().astimezone())
        if op in (0x40, 0x50):
            state["path"], state["method"] = state["param"].decode(), op
        elif op == 0x41:
            state["post"] = json.loads(state["param"])
        elif op in (0x42, 0x52):
            assert state["path"] == web_api.TIME_PATH
            if op == 0x42:
                clock["offset"] = state["post"]["time"] - now
            state["body"] = json.dumps({"status": "200", "result": {"time": now + clock["offset"]}}).encode()
            reply += struct.pack(">HI", 200, len(state["body"]))
        elif op in (0x43, 0x53):
            reply += struct.pack(">H", len(state["body"]))
        asyncio.get_running_loop().call_soon(self._notify[cp], None, bytearray(reply))

    async def tunnel_read(self: Any, char: str) -> bytearray:
        if char == param:
            return bytearray(state["body"])
        return await read(self, char)

    monkeypatch.setattr(FakeBleakClient, "write_gatt_char", tunnel_write)
    monkeypatch.setattr(FakeBleakClient, "read_gatt_char", tunnel_read)

    async def main(*args: Any, **kwargs: Any) -> Any:
        return await original(*args, **{**kwargs, "tunnel_request": None})

    original = at.main
    monkeypatch.setattr(at, "main", main)
    console = ready(ScriptedConsole()).expect("Start?").expect("Does Clock 1 show", "y").expect("90 minutes back")
    console.expect("jump 90 minutes", "y").expect("back to the time", "y")
    await run(console, "--step", "clock")
    assert abs(clock["offset"] - -120) <= 2
    assert "may not be back" not in console.text()
    frames = [json.loads(line) for line in next((tmp_path / "results").iterdir()).joinpath("frames.jsonl").open()]
    assert {"tunnel-control", "tunnel-param"} <= {f["char"] for f in frames}


# --- review round 4 -----------------------------------------------------------------------------------------------


async def test_a_continued_run_puts_back_what_it_found_at_its_own_start(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """On at the first run, in standby at the next (someone switched it off in between): it ends in standby."""
    projector.bonded = True
    await run(ready(ScriptedConsole()).expect("Start?", "q"), "--step", "direction")
    projector.power = 0
    before = snapshot(projector)
    console = ready(ScriptedConsole()).expect("Start?").expect("Auto picture direction is now off", "n")
    console.expect("Direction 1", "u").expect("Direction 0", "h").expect("Direction 3", "v")
    await run(console, "--step", "direction")
    assert snapshot(projector) == before
    data = results_of(tmp_path)
    assert data["baseline"]["state"]["power"] is True
    assert data["later_starts"][-1]["state"]["power"] is False
    assert data["differences"] == []


async def test_a_failed_standby_after_putting_back_is_tried_again(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projector.bonded = True
    projector.power, projector.app_state, projector.poster = 0, 0x10, 11
    session_dir = tmp_path / "results" / "2026-01-01-000000"
    session_dir.mkdir(parents=True)
    old = at.Results(session_dir)
    source = {"app_state": 0x10, "poster": 5, "net_topology": 17, "software_version": "3.1.15"}
    old.data["pending_restores"] = [{"kind": "source", "value": source, "label": "source", "scope": "session"}]
    old.data["baseline"] = {"state": {"power": False}}
    old.save()
    original = at.SessionClient.set_power
    failed: list[bool] = []

    async def set_power(self: Any, on: bool) -> None:
        if not on and not failed:
            failed.append(True)
            raise BleakError("the link dropped")
        await original(self, on)

    monkeypatch.setattr(at.SessionClient, "set_power", set_power, raising=False)
    console = ready(ScriptedConsole())
    await run(console, "--restore-only")
    assert failed
    assert (projector.power, projector.poster) == (0, 5)
    assert json.loads((session_dir / "results.json").read_text())["pending_restores"] == []
    assert "NOT PUT BACK" not in console.text()


def test_by_hand_texts_say_on_or_off() -> None:
    assert at.manual_restore("power", False).startswith("switch the projector to standby with")
    assert at.manual_restore("power", True).startswith("switch the projector on with")
    assert at.manual_restore("auto_direction", True).startswith("switch auto picture direction on")


# --- review round 5 -----------------------------------------------------------------------------------------------


async def test_a_countdown_can_skip_the_step(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True
    before = snapshot(projector)

    def standby() -> str:
        projector.power = 0
        return ""

    console = ready(ScriptedConsole()).expect("Start?").expect("lightly once", standby)
    console.during_countdowns.append("s")
    await run(console, "--step", "notifications")
    assert snapshot(projector) == before  # switched on again
    assert results_of(tmp_path)["steps"]["notifications"]["status"] == "ended early (skipped)"


async def test_enter_ends_a_countdown_early(run: Callable[..., Awaitable[Any]], projector: FakeProjector) -> None:
    projector.bonded = True
    console = ready(ScriptedConsole()).expect("Start?").expect("lightly once").expect("again").expect("come back", "y")
    console.during_countdowns += ["", ""]
    session = await run(console, "--step", "notifications")
    ended = [e for e in session.results.data["steps"]["notifications"]["events"] if e["kind"] == "countdown-ended"]
    assert [e["key"] for e in ended] == ["", ""]


async def test_a_standby_that_does_not_show_is_not_counted_as_put_back(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projector.bonded = True
    projector.power = 0
    original = at.SessionClient.set_power

    async def set_power(self: Any, on: bool) -> None:
        if on:
            await original(self, on)  # acknowledged, but standby never comes

    monkeypatch.setattr(at.SessionClient, "set_power", set_power, raising=False)
    console = weather_answers(ready(ScriptedConsole()))
    await run(console, "--step", "weather")
    assert [p["kind"] for p in results_of(tmp_path)["pending_restores"]] == ["power"]
    assert "NOT PUT BACK: power: back to standby" in console.text()
    assert "switch the projector to standby with" in console.text()


def test_redactor_home_folder_and_escaped_names(tmp_path: Path) -> None:
    results = at.Results(tmp_path)
    results.data["baseline"] = {"state": {"controllers": [{"id": 1, "name": 'Alex "A"', "allowed": True}]}}
    redact = at.Redactor(results)
    text = redact.text(f'{Path.home()}/x.py name="Alex \\"A\\"" and Alex "A"')
    assert str(Path.home()) not in text
    assert "Alex" not in text


# --- review round 6 -----------------------------------------------------------------------------------------------


async def test_app_state_0_at_the_start_is_read_again(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """Right after Restart a real one answered app state 0 for a while: read again once it is up."""
    projector.bonded = True
    projector.app_state, projector.poster, projector.topology = 0x10, 5, 17
    projector.blank(asyncio.get_running_loop().time() + 0.05)  # 0 until then, then the poster again
    before = {**snapshot(projector), "app_state": 0x10, "topology": 17}
    console = ready(ScriptedConsole())
    console.expect("Start?").expect("Code 7", "n").expect("Code 11", "n").expect("Code 1 ", "n").expect("now show", "n")
    FakeClock.real = 0.01
    try:
        await run(console, "--step", "weather")
    finally:
        FakeClock.real = 0.0
    assert snapshot(projector) == before
    assert results_of(tmp_path)["baseline"]["state"]["app_state"] == 0x10


# --- review round 8 -----------------------------------------------------------------------------------------------


async def test_a_clock_write_the_tunnel_refuses_is_an_answer(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True

    class ReadOnlyTunnel(FakeTunnel):
        async def __call__(self, method: Any, body: str | None) -> Any:
            if method.name == "POST":
                self.requests.append((method.name, body))
                return at.web_tunnel.Response(404, b"", 0, 0)
            return await super().__call__(method, body)

    console = ready(ScriptedConsole()).expect("Start?").expect("Does Clock 1 show", "y").expect("90 minutes back")
    await run(console, "--step", "clock", tunnel=ReadOnlyTunnel())
    data = results_of(tmp_path)
    assert data["steps"]["clock"]["status"] == "done"
    assert data["pending_restores"] == []
    assert "did not work: TunnelError: HTTP 404" in console.text()
    assert "nothing to put back" in console.text()


async def test_restore_only_checks_against_the_sessions_start(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True
    projector.direction = 0  # changed since, and not noted as pending
    session_dir = tmp_path / "results" / "2026-01-01-000000"
    session_dir.mkdir(parents=True)
    old = at.Results(session_dir)
    old.data["pending_restores"] = [{"kind": "weather", "value": 5, "label": "weather", "scope": "step"}]
    old.data["baseline"] = {"state": {"power": True, "direction": 2}, "weather": 5}
    old.save()
    console = ready(ScriptedConsole())
    await run(console, "--restore-only")
    assert "DIFFERENT FROM THE START: direction: 2 at the start, 0 now" in console.text()
    assert "Continuing the session" not in console.text()


# --- review round 9 -----------------------------------------------------------------------------------------------


# --- the narrowed session -----------------------------------------------------------------------------------------


async def test_a_computer_not_registered_is_sent_to_lspx_cli_pair(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """The session never pairs: it says how to register, and sends nothing but the power read that found out."""
    projector.bonded = False
    console = ready(ScriptedConsole())
    await run(console)
    assert "tools/lspx_cli.py pair" in console.text()
    assert sent_ops(projector) == {"GET_POWER"}
    assert not projector.bond_ops
    assert projector.controllers == {0: "iPhone", MY_ID: "Home Assistant (dev)"}


async def test_a_link_lost_while_listening_is_said(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, fake_connection: list[FakeBleakClient], tmp_path: Path
) -> None:
    """Whether the link stays up through standby is not known: if it drops, "no notifications" proves nothing."""

    def press_and_drop() -> str:
        projector.power = 0
        fake_connection[-1].drop()
        return ""

    def press_again() -> str:
        projector.power = 0x10
        return ""

    console = ready(ScriptedConsole()).expect("Start?").expect("lightly once", press_and_drop)
    console.expect("again", press_again).expect("come back on", "y")
    await run(console, "--step", "notifications")
    events = results_of(tmp_path)["steps"]["notifications"]["events"]
    assert next(e for e in events if e["kind"] == "unasked-frames")["links_lost"] == 1
    assert "this does not show that the projector sends none" in console.text()


async def test_what_was_typed_before_a_countdown_does_not_end_it() -> None:
    console = at.TerminalConsole()
    console._loop = asyncio.get_running_loop()
    console._lines = asyncio.Queue()
    console._lines.put_nowait("")  # a second Enter, typed after the last answer
    console._loop.call_later(0.01, console._lines.put_nowait, "s")
    assert await console.waiting_line() == "s"


async def test_a_copy_of_the_last_reply_is_not_counted_as_unasked(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replies can come twice: the copy of the reply just before listening is not the projector's own news."""
    projector.duplicate_delay = 0.05
    monkeypatch.setattr(FakeClock, "real", 0.1)
    console = ready(ScriptedConsole()).expect("Start?").expect("lightly once").expect("again").expect("come back", "y")
    console.during_countdowns += ["", ""]
    await run(console, "--step", "notifications")
    events = results_of(tmp_path)["steps"]["notifications"]["events"]
    assert next(e for e in events if e["kind"] == "unasked-frames")["count"] == 0
    assert projector.copies_sent
    await asyncio.sleep(0.1)  # let the last copies arrive (into no link)


# --- the last review round ----------------------------------------------------------------------------------------


async def test_only_notifications_while_listening_are_counted(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, fake_connection: list[FakeBleakClient], tmp_path: Path
) -> None:
    def before_listening() -> str:
        fake_connection[-1].notify(bytes([1, 0x80, 0x12, 0x10]))  # the session's own doing, say
        return ""

    def press() -> str:
        fake_connection[-1].notify(bytes([1, 0x80, 0x00, 0x00]))
        return ""

    console = ready(ScriptedConsole()).expect("Start?", before_listening).expect("lightly once", press)
    console.expect("again").expect("come back on", "y")
    console.during_countdowns += ["", ""]
    await run(console, "--step", "notifications")
    events = results_of(tmp_path)["steps"]["notifications"]["events"]
    assert next(e for e in events if e["kind"] == "unasked-frames")["notifications"] == 1
    assert "1 of them read as change notifications" in console.text()


async def test_after_switching_on_the_power_settles_first(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Right after switching on, commands are briefly refused (0x03): nothing is chosen until the power reads on."""
    projector.power = 0
    before = snapshot(projector)
    handle = FakeProjector.handle

    def switching(self: FakeProjector, frame: bytes, encrypted: bool) -> bytes:
        reply = handle(self, frame, encrypted)
        if frame[:3] == bytes([0x08, 0x00, 0x10]):  # SET_POWER on: the next two commands are refused
            self.transition_replies = 2
        return reply

    monkeypatch.setattr(FakeProjector, "handle", switching)
    await run(weather_answers(ready(ScriptedConsole())), "--step", "weather")
    data = results_of(tmp_path)
    assert data["steps"]["weather"]["status"] == "done"
    assert any(e["kind"] == "switched-on" for e in data["steps"]["weather"]["events"])
    assert snapshot(projector) == before
    # Only the power is asked until it reads on: the refused answers go to it, not to the main board.
    ops = [(Op(op).name, payload) for op, payload in projector.log]
    on = ops.index(("SET_POWER", b"\x10"))
    assert [name for name, _ in ops[on + 1 : on + 4]] == ["GET_POWER"] * 3


async def test_the_clock_jumps_back_not_ahead(run: Callable[..., Awaitable[Any]], projector: FakeProjector) -> None:
    """A jump ahead could pass, and fire, one of the projector's own once-only timers."""
    tunnel = FakeTunnel(offset=0)
    console = ready(ScriptedConsole()).expect("Start?").expect("Does Clock 1 show", "y").expect("90 minutes back")
    console.expect("jump 90 minutes back", "y").expect("back to the time", "y")
    await run(console, "--step", "clock", tunnel=tunnel)
    first_post = next(json.loads(body)["time"] for method, body in tunnel.requests if method == "POST" and body)
    now = web_api.local_unix_seconds(datetime.now().astimezone())
    assert -90 * 60 - 5 <= first_post - now <= -90 * 60 + 5
    assert "no timer of the projector's can fire" in console.text()


# --- the last pass ----------------------------------------------------------------------------------------------


class NoHttp:
    """An HTTP session for the Wi-Fi reads; the functions that use it are patched."""

    async def __aenter__(self) -> NoHttp:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


async def test_over_wifi_the_clock_is_only_read(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projector.endpoint = (80, "192.168.1.50")
    projector.topology = 17
    sent: list[int] = []

    async def get_time(session: Any, host: str, *, port: int = 80) -> Any:
        return web_api.ProjectorTime(web_api.local_unix_seconds(datetime.now().astimezone()) + 600, "GMT+1")

    async def set_time(session: Any, host: str, seconds: int, *, port: int = 80) -> Any:
        sent.append(seconds)
        return web_api.ProjectorTime(seconds, "GMT+0")

    monkeypatch.setattr(web_api, "get_time", get_time)
    monkeypatch.setattr(web_api, "set_time", set_time)
    original = at.main

    async def main(*args: Any, **kwargs: Any) -> Any:
        return await original(*args, **{**kwargs, "http_session": NoHttp})

    monkeypatch.setattr(at, "main", main)
    console = ready(ScriptedConsole()).expect("Start?").expect("Does Clock 1 show", "y").expect("over tunnel", "n")
    await run(console, "--step", "clock")
    assert not sent
    assert "nothing is set that way" in console.text()
    assert "(+10 min), time zone 'GMT+1'" in console.text()
    share = next((tmp_path / "results").iterdir()) / "shareable.md"
    assert "192.168.1.50" not in share.read_text()


async def test_restore_only_checks_against_the_latest_runs_start(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """On at the first run, in standby at the next, which left something pending: standby is no difference."""
    projector.power = 0
    session_dir = tmp_path / "results" / "2026-01-01-000000"
    session_dir.mkdir(parents=True)
    old = at.Results(session_dir)
    old.data["pending_restores"] = [{"kind": "direction", "value": 2, "label": "direction", "scope": "step"}]
    old.data["baseline"] = {"state": {"power": True, "direction": 2}, "weather": 5}
    old.data["later_starts"] = [{"state": {"power": False, "direction": 2}, "weather": 5}]
    old.save()
    projector.direction = 1
    console = ready(ScriptedConsole())
    await run(console, "--restore-only")
    assert projector.direction == 2
    assert "Everything the session checks is as at the start." in console.text()


async def test_what_can_never_be_put_back_is_listed_at_the_end(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """An old session's clock change over Wi-Fi, which this version never makes or puts back: said at the end."""
    session_dir = tmp_path / "results" / "2026-01-01-000000"
    session_dir.mkdir(parents=True)
    old = at.Results(session_dir)
    clock = {"route": "wifi", "offset": 0, "host": "192.168.1.50"}
    old.data["pending_restores"] = [{"kind": "clock", "value": clock, "label": "clock over wifi", "scope": "step"}]
    old.data["baseline"] = {"state": {"power": True}, "weather": 5}
    old.save()
    console = ready(ScriptedConsole())
    await run(console, "--restore-only")
    assert "NOT PUT BACK, and the session cannot (by hand):" in console.text()
    assert "open Sony's app once" in console.text()
    data = json.loads((session_dir / "results.json").read_text())
    assert data["not_put_back"][0]["label"] == "clock over wifi"
    assert "Not put back, and the session cannot" in (session_dir / "shareable.md").read_text()


async def test_a_link_lost_while_reading_the_start_ends_cleanly(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def lost(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise BleakError("disconnected")

    monkeypatch.setattr(at.SessionClient, "read_state", lost, raising=False)
    console = ready(ScriptedConsole())
    await run(console)
    assert "Cannot reach the projector" in console.text()
    assert "Give the connection back" in console.text()


async def test_quitting_while_the_link_is_busy_makes_no_more_attempts(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, found: list[bool], fake_connection: list[Any]
) -> None:
    found += [False]
    console = ready(ScriptedConsole()).expect("Try again?", "q")
    await run(console)
    assert not fake_connection  # never connected: nothing to check at the end
    assert "Give the connection back" in console.text()


# --- the final change -----------------------------------------------------------------------------------------------


async def test_bluetooth_off_is_explained_and_can_be_tried_again(
    projector: FakeProjector, fake_connection: list[FakeBleakClient], tmp_path: Path
) -> None:
    tries: list[int] = []

    async def find_device() -> BLEDevice | None:
        tries.append(1)
        if len(tries) == 1:
            raise BleakError("Bluetooth device is turned off")
        return make_ble_device(ADDRESS, NAME)

    console = ready(ScriptedConsole()).expect("Try again?").expect("Start?", "q")
    await at.main(
        ["--results", str(tmp_path / "results"), "--step", "direction"],
        console=console,
        find_device=find_device,
        clock=FakeClock(),
        tunnel_request=FakeTunnel(),
    )
    assert not console.script
    assert "Bluetooth may be off on this computer" in console.text()
    assert "Privacy & Security" in console.text()
    assert len(tries) == 2


async def test_a_wifi_direct_topology_agreed_to_be_left_is_listed(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.app_state, projector.poster, projector.topology = 0x10, 5, 33  # Wi-Fi Direct, a poster
    console = ready(ScriptedConsole()).expect("Start?").expect("HDMI source at hand", "y")
    console.expect("Wi-Fi Direct, say", "y").expect("What happened", "d")
    await run(console, "--step", "hdmi")
    data = results_of(tmp_path)
    assert [c["label"] for c in data["not_put_back"]] == ["network topology 33"]
    assert (projector.app_state, projector.poster) == (0x10, 5)  # the poster is back; the topology is said


async def test_no_switch_on_restore_when_the_start_source_shows(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector
) -> None:
    """On HDMI at the start and again at the end: SetNetTopology 48 on HDMI was never seen, so it is not sent."""
    before = snapshot(projector)
    console = ready(ScriptedConsole()).expect("Start?").expect("Auto picture direction is now off", "n")
    console.expect("Direction 1", "u").expect("Direction 0", "h").expect("Direction 3", "v")
    console.expect("Start?").expect("HDMI source at hand", "y").expect("What happened", "d")
    await run(console, "--step", "direction,hdmi")
    assert snapshot(projector) == before
    hdmi_switches = [p for op, p in projector.log if op == Op.SET_NET_TOPOLOGY and p == b"\x30"]
    assert len(hdmi_switches) == 1  # the HDMI step's own, none on putting back


async def test_the_end_check_reads_the_clock_again(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """The jump is taken but the write back only answers: the end check says the clock is off."""

    class BackIgnored(FakeTunnel):
        posts = 0

        async def __call__(self, method: Any, body: str | None) -> Any:
            if method.name == "POST":
                self.posts += 1
                if self.posts > 1:
                    return at.web_tunnel.Response(200, b"", 0, 0)
            return await super().__call__(method, body)

    console = ready(ScriptedConsole()).expect("Start?").expect("Does Clock 1 show", "y").expect("90 minutes back")
    console.expect("jump 90 minutes", "y").expect("back to the time", "n")
    await run(console, "--step", "clock", tunnel=BackIgnored(offset=0))
    data = results_of(tmp_path)
    assert any(d.startswith("clock: -5") for d in data["differences"])
    assert data["not_put_back"][0]["label"] == "clock over tunnel"
