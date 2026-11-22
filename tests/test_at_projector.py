"""tools/at_projector.py, the guided session at the projector, against the fake projector.

Above all: whatever happens (every step run, Ctrl-C, a lost link), the projector ends as it started, nothing is sent
that the session must never send (volume, Restart, changes to "Smartphones added"), and the smartphone steps touch
only an entry a session made. Like tests/test_lspx_cli.py, these do not count towards the integration's coverage.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

import custom_components.sony_lspx_p1 as integration
from custom_components.sony_lspx_p1 import client as client_module
from custom_components.sony_lspx_p1 import web_api
from custom_components.sony_lspx_p1.client import make_ble_device
from custom_components.sony_lspx_p1.protocol import BondOp, NetTopology, Op, encode_bond_activation, encode_command

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

MY_ID = 0x1001  # the id the fake projector gives the first controller paired after "iPhone"


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

    def expect(self, fragment: str, reply: Reply = "") -> ScriptedConsole:
        self.script.append((fragment, reply))
        return self

    def say(self, text: str) -> None:
        self.said.append(text)

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
    """Not registered with this computer yet; on, showing HDMI, as tests/fake_projector.py starts."""
    fake = FakeProjector(bonded=False)
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
    }


def sent_ops(projector: FakeProjector) -> set[str]:
    return {Op(op).name for op, _ in projector.log}


def results_of(tmp_path: Path) -> dict[str, Any]:
    (directory,) = (tmp_path / "results").iterdir()
    return json.loads((directory / "results.json").read_text())


def ready(console: ScriptedConsole) -> ScriptedConsole:
    return console.expect("Ready")


def pairing(console: ScriptedConsole) -> ScriptedConsole:
    return ready(console).expect("Pair now?", "y")


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

    def block(bonded: bool) -> Callable[[], str]:
        def change() -> str:
            projector.bonded = bonded  # as if Home Assistant had blocked or allowed this computer
            return "o"

        return change

    def remove() -> str:
        del projector.controllers[MY_ID]
        projector.bonded = False
        return "o"

    console = pairing(ScriptedConsole())
    # Weather
    console.expect("Start?").expect("Code 7", "y").expect("Code 11", "y").expect("Code 1 ", "o more like fog")
    console.expect("code 3", "y").expect("code 4", "y").expect("code 9", "o snowflakes")
    # Clock
    console.expect("Start?").expect("Does Clock 1 show", "y").expect("90 minutes ahead over tunnel")
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
    # Detection
    console.expect("Run it?", "y").expect("Which one is on", "a").expect("Close Sony's app").expect("Part 2", "o")
    console.expect("switch itself on", "n").expect("standby by itself", "n")
    # Block and allow
    console.expect("Run it?", "y").expect("Who makes the change", "h").expect("What did Home Assistant", block(False))
    console.expect("as denied", "y").expect("press Enter").expect("What did Home Assistant", block(True))
    console.expect("press Enter")
    # Remove
    console.expect("Run it?", "y").expect("Who makes the change", "h").expect("What did Home Assistant", remove)
    console.expect("gone from Smartphones added", "y").expect("press Enter")

    session = await run(console, tunnel=tunnel)

    assert snapshot(projector) == before
    assert projector.controllers == {0: "iPhone"}  # the session's own entry is gone again
    assert abs(tunnel.offset - -300) <= 2  # the clock is back as it was
    assert [m for m, _ in tunnel.requests].count("POST") == 2
    assert "SET_VOLUME" not in sent_ops(projector)
    assert "REBOOT" not in sent_ops(projector)
    # The session itself never changes "Smartphones added" (Home Assistant or the app does, in the steps).
    assert {BondOp(op).name for op in projector.bond_ops} <= {"GET_MY_ID", "GET_PIN_CODE_FLAG"}
    # The detection step puts the byte back before the power, so that the detection bits cannot act meanwhile.
    ops = [Op(op).name for op, _ in projector.log]
    last = {name: len(ops) - 1 - ops[::-1].index(name) for name in ("SET_AUTO_STANDBY", "SET_POWER")}
    assert last["SET_AUTO_STANDBY"] < last["SET_POWER"]

    data = results_of(tmp_path)
    assert {k: v["status"] for k, v in data["steps"].items()} == dict.fromkeys(
        ("setup", *at.STEP_KEYS, "finish"), "done"
    )
    assert data["pending_restores"] == []
    assert data["differences"] == []
    assert data["finished"]
    assert data["created_entry"] == {
        "id": MY_ID,
        "name": at.TEST_ENTRY_NAME,
        "session": data["started"],
        "removed": True,
    }
    events = data["steps"]
    assert [e["code"] for e in events["weather"]["events"] if e["kind"] == "set-weather"] == [7, 11, 1, 3, 4, 9]
    assert [e["data"] for e in events["notifications"]["events"] if e["kind"] == "notification"] == ["00", "10"]
    assert any(e["kind"] == "hdmi-status" for e in events["hdmi"]["events"])
    refused = [e for e in events["block"]["events"] if e["kind"] == "read" and "(blocked)" in e["what"]]
    assert [e["what"] for e in refused][:2] == ["power (blocked)", "this controller's id (blocked)"]
    assert "NotPairedError" in refused[0]["error"]
    assert session.entry_removed()
    assert not session.client.is_connected

    # The raw frames are kept, and the shareable copy leaves out the address, the projector's and the phone's names.
    directory = session.results.directory
    frames = [json.loads(line) for line in (directory / "frames.jsonl").read_text().splitlines()]
    assert {"command", "bonding", "smartphones", "bonding-name"} <= {f["char"] for f in frames}
    assert any(f["what"] == "SET_WEATHER (0x0069)" for f in frames)
    share = (directory / "shareable.md").read_text()
    assert "## Weather poster pictures (#2)" in share
    assert "more like fog" in share
    for private in (ADDRESS, NAME, "iPhone"):
        assert private not in share
    assert at.TEST_ENTRY_NAME in share
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
    assert data["finished"]


# --- what the session must never do -----------------------------------------------------------------------------


def test_refuses_what_it_must_never_send() -> None:
    client = at.SessionClient(make_ble_device(ADDRESS, NAME), lambda *_: None)
    refused = [
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_VOLUME, b"\x28")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_XLOUD, b"\x00")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.REBOOT)),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_AUTO_STANDBY, b"\x02")),  # not all three bytes
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_NET_TOPOLOGY, b"\x20")),  # Wi-Fi Direct
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.DELETE_CONTROLLER, 0x10, 0x01])),
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.BLOCK_CONTROLLER, 0x10, 0x01])),
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.SET_PIN_CODE_FLAG, 1])),
        (fake_projector.CHAR_BOND_CONTROL, encode_bond_activation()),  # only while pairing
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
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_AUTO_STANDBY, b"\x02\x01\x00")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.GET_VOLUME)),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_NET_TOPOLOGY, b"\x30")),
        (fake_projector.CHAR_COMMAND, encode_command(Op.SET_NET_TOPOLOGY, b"\x11")),
        (fake_projector.CHAR_BOND_CONTROL, bytes([BondOp.GET_MY_ID])),
        (at.TUNNEL_PARAM, web_api.TIME_PATH.encode()),
        (at.TUNNEL_PARAM, b'{"time":1}'),
        (at.TUNNEL_CP, bytes.fromhex("000000500011")),
    ]
    for char, data in allowed:
        client.check(char, data)
    client.pairing = True
    client.check(fake_projector.CHAR_BOND_CONTROL, encode_bond_activation())
    client.check(fake_projector.CHAR_BONDING_NAME, b"LSPX test session")


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


async def test_smartphone_steps_leave_an_entry_made_elsewhere_alone(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """This computer was registered before (by tools/lspx_cli.py pair, say): not an entry a session made."""
    projector.bonded = True
    projector.controllers[MY_ID] = "Home Assistant (dev)"
    projector.my_id = MY_ID
    console = ready(ScriptedConsole()).expect("Run it?", "y").expect("Run it?", "y")
    await run(console, "--step", "block,remove")
    assert "leave that entry alone" in console.text()
    assert "was not made by a session" in console.text()
    data = results_of(tmp_path)
    assert data["steps"]["block"]["status"] == "ended early (no entry made by a session)"
    assert data["steps"]["remove"]["status"] == "ended early (no entry made by a session)"
    assert projector.controllers == {0: "iPhone", MY_ID: "Home Assistant (dev)"}
    assert not projector.blocked


async def test_smartphone_steps_check_the_projector_agrees(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    """The entry a session made must still be the one the projector says this computer is."""
    console = pairing(ScriptedConsole()).expect("Run it?", "y")

    def elsewhere() -> str:
        projector.my_id = 0  # the projector now says this computer is "iPhone"
        return "y"

    console.script[-1] = ("Run it?", elsewhere)
    await run(console, "--step", "block")
    assert "they must be the same, so the step does nothing" in console.text()
    assert (
        results_of(tmp_path)["steps"]["block"]["status"]
        == "ended early (this computer is not the entry the session made)"
    )
    assert not projector.blocked


async def test_opt_in_steps_need_a_yes(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    projector.bonded = True
    before = snapshot(projector)
    console = ready(ScriptedConsole()).expect("Run it?").expect("Run it?", "n").expect("Type one of", "")
    await run(console, "--step", "detection,remove")
    assert snapshot(projector) == before
    steps = results_of(tmp_path)["steps"]
    assert steps["detection"]["status"] == "skipped"
    assert steps["remove"]["status"] == "skipped"
    assert "harder to undo" in console.text()


# --- steps on their own --------------------------------------------------------------------------------------------


async def test_weather_refused(run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path) -> None:
    """A NAK to SetWeather (UNKNOWN_COMMAND, say) is the answer #2 asks for, recorded as such."""
    projector.bonded = True
    projector.rejections[Op.SET_WEATHER] = 0x03
    console = (
        ready(ScriptedConsole()).expect("Start?").expect("Code 7", "n").expect("Code 11", "n").expect("Code 1 ", "n")
    )
    console.expect("now show", "n")
    await run(console, "--step", "weather")
    events = results_of(tmp_path)["steps"]["weather"]["events"]
    assert next(e["rejected"] for e in events if e["kind"] == "set-weather").startswith("CommandRejected: ")
    assert "So SetWeather does not change the picture here" in console.text()


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


async def test_clock_over_wifi(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a home Wi-Fi network both routes are tried, and each puts the clock back."""
    projector.bonded = True
    projector.endpoint = (80, "192.168.1.50")
    projector.topology = 17
    clock = {"offset": 600}

    async def get_time(session: Any, host: str, *, port: int = 80) -> Any:
        now = web_api.local_unix_seconds(datetime.now().astimezone())
        return web_api.ProjectorTime(now + clock["offset"], "GMT+0")

    async def set_time(session: Any, host: str, seconds: int, *, port: int = 80) -> Any:
        clock["offset"] = seconds - web_api.local_unix_seconds(datetime.now().astimezone())
        return web_api.ProjectorTime(seconds, "GMT+0")

    monkeypatch.setattr(web_api, "get_time", get_time)
    monkeypatch.setattr(web_api, "set_time", set_time)

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    original = at.main

    async def main(*args: Any, **kwargs: Any) -> Any:
        return await original(*args, **{**kwargs, "http_session": Session})

    monkeypatch.setattr(at, "main", main)
    console = ready(ScriptedConsole()).expect("Start?")
    console.expect("Does Clock 1 show", "y").expect("over tunnel", "n")
    console.expect("Does Clock 1 show", "y").expect("over wifi").expect("jump", "y").expect("back to the time", "y")
    await run(console, "--step", "clock")
    assert abs(clock["offset"] - 600) <= 2
    assert "192.168.1.50" in console.text()
    share = next((tmp_path / "results").iterdir()) / "shareable.md"
    assert "192.168.1.50" not in share.read_text()


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
        f"1A2B3C4D-0000-1111-2222-333344445555 {NAME} AA:BB:CC:DD:EE:FF Alex's iPhone Home Assistant"
        " 192.168.1.20 192.168.8.100 0.0.0.0 3.1.15"
    )
    assert text == (
        "<projector address> LSPXP1_xxxx <address> <smartphone 1> Home Assistant <home address> <192.168.8.x>"
        " 0.0.0.0 3.1.15"
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
    assert "8. remove: " in text
    assert "[opt-in]" in text


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
    assert data["finished"]


# --- the entry the session made -------------------------------------------------------------------------------------


async def test_a_continued_session_that_pairs_again_owns_the_new_entry(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    await run(pairing(ScriptedConsole()).expect("Start?", "q"), "--step", "weather,block")
    assert results_of(tmp_path)["created_entry"]["id"] == MY_ID
    del projector.controllers[MY_ID]  # deleted in Sony's app meanwhile
    projector.controllers[0x2000] = "Pixel"  # and a phone added: the next entry gets another id
    projector.bonded = False

    console = pairing(ScriptedConsole()).expect("Start?", "s").expect("Run it?", "y")
    console.expect("Who makes the change", "s")
    await run(console, "--step", "weather,block")
    entry = results_of(tmp_path)["created_entry"]
    assert entry["id"] == 0x2000 + 0x1001
    assert "not using it" not in console.text()


async def test_an_entry_with_the_same_id_but_another_name_is_not_the_sessions(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, tmp_path: Path
) -> None:
    def renamed() -> str:
        projector.controllers[MY_ID] = "Home Assistant (dev)"  # registered anew by tools/lspx_cli.py, same id
        return "y"

    console = pairing(ScriptedConsole()).expect("Run it?", renamed)
    await run(console, "--step", "block")
    assert "they must be the same, so the step does nothing" in console.text()
    status = results_of(tmp_path)["steps"]["block"]["status"]
    assert status == "ended early (this computer is not the entry the session made)"
    assert not projector.blocked


async def test_a_pairing_cut_short_is_still_the_sessions(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ctrl-C after the projector added the entry but before the session noted it: the next run adopts it."""
    original = client_module.LSPXClient.pair

    async def pair_then_ctrl_c(self: Any, *args: Any, **kwargs: Any) -> None:
        await original(self, *args, **kwargs)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(at.SessionClient, "pair", pair_then_ctrl_c, raising=False)
    await run(pairing(ScriptedConsole()), "--step", "weather")
    assert results_of(tmp_path)["created_entry"] is None
    assert results_of(tmp_path)["pairing_started"]["name"] == at.TEST_ENTRY_NAME

    console = ready(ScriptedConsole()).expect("Start?", "q")
    await run(console, "--step", "weather")
    assert results_of(tmp_path)["created_entry"]["id"] == MY_ID
    assert f'this computer is "{at.TEST_ENTRY_NAME}", id {MY_ID}' in console.text()


async def test_a_blocked_computer_whose_link_drops(
    run: Callable[..., Awaitable[Any]], projector: FakeProjector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One of #41's possibilities: the projector drops a blocked controller instead of answering 0xA0."""
    connect = client_module.establish_connection

    def block() -> str:
        async def dropped(*args: Any, **kwargs: Any) -> Any:
            raise BleakError("disconnected")

        monkeypatch.setattr(client_module, "establish_connection", dropped)
        return "o"

    def allow() -> str:
        monkeypatch.setattr(client_module, "establish_connection", connect)
        return "o"

    console = pairing(ScriptedConsole()).expect("Run it?", "y").expect("Who makes the change", "h")
    console.expect("What did Home Assistant", block).expect("as denied", "y").expect("press Enter")
    console.expect("Go on?", "d").expect("What did Home Assistant", allow).expect("press Enter")
    await run(console, "--step", "block")
    events = results_of(tmp_path)["steps"]["block"]["events"]
    assert any(e["kind"] == "connect-failed" for e in events)
    probes = [e for e in events if e["kind"] == "read" and "(blocked)" in e["what"]]
    assert len(probes) == 4
    assert all("BleakError" in e["error"] for e in probes)
    assert results_of(tmp_path)["steps"]["block"]["status"] == "done"
    assert results_of(tmp_path)["pending_restores"] == []


ORIGINAL_RESTORE = at.Session._restore
