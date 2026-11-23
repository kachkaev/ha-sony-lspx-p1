"""A guided sitting at the projector: the checks that need someone looking at it, one at a time.

Usage (from a terminal that may use Bluetooth, near the projector):
    .venv/bin/python tools/at_projector.py              # the whole session, or the rest of the last one
    .venv/bin/python tools/at_projector.py --list       # the steps: what each settles, how long it takes
    .venv/bin/python tools/at_projector.py --step weather,direction   # only these steps
    .venv/bin/python tools/at_projector.py --new        # start a new session instead of continuing the last
    .venv/bin/python tools/at_projector.py --restore-only   # put back what an interrupted session left changed

Each step says what it is about to do, does it over Bluetooth with the integration's own client, asks what you see
(type the letter of an answer, optionally followed by a note: "o more like fog"), and writes the answers, the raw
frames and the timings to .at-projector/<date-time>/ (git-ignored), with a redacted shareable.md to paste into the
issues. It puts back every setting it changes at the end of each step (the source and the power at the end of the
session), also on the first Ctrl-C (a second one quits at once and leaves the rest for --restore-only), and refuses
to send anything else, such as volume, Restart or any change to "Smartphones added", which it only ever reads.

The computer must be registered with the projector once, beforehand: tools/lspx_cli.py pair. The projector serves
one Bluetooth client at a time: disable the integration's entry in Home Assistant and close Sony's app first (the
session says how, and how to give the connection back).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import logging
import platform
import re
import sys
import threading
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(REPO / "custom_components"))
sys.path.insert(0, str(TOOLS))

import aiohttp
import lspx_cli
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from probes import web_tunnel
from sony_lspx_p1 import web_api
from sony_lspx_p1.client import (
    SWITCH_REPLY_TIMEOUT,
    LSPXClient,
    NotPairedError,
    ProjectorBusyError,
    describe_error,
    describe_op,
)
from sony_lspx_p1.protocol import (
    CHAR_BOND_CONTROL,
    CHAR_BONDED_LIST,
    CHAR_BONDING_NAME,
    CHAR_COMMAND,
    CHAR_SECURITY_PROBE,
    LOCAL_NAME_PREFIX,
    POSTER_NAMES,
    STATE_READS,
    STATIC_READS,
    WEATHER_CODES,
    AppState,
    BondCommandFailed,
    BondOp,
    CommandRejected,
    Direction,
    ErrorCode,
    HdmiStatus,
    NetTopology,
    Notification,
    Op,
    Poster,
    ProtocolError,
    decode_bool,
    decode_hdmi_status,
    decode_http_endpoint,
    decode_power,
    decode_string,
    decode_u8,
    decode_u16,
    payload_u8,
)

DEFAULT_RESULTS = REPO / ".at-projector"
SCAN_TIMEOUT = 15.0
# How long a start-up may take before the session stops waiting: about 45 s after a long standby, about 50 s after
# Restart (docs/protocol.md), and the integration's own limit is 120 s.
START_UP_LIMIT = 150.0
START_UP_PROBE_INTERVAL = 3.0
STANDBY_SETTLE = 15.0  # seconds to wait for the power to read standby after switching to it
RESTORE_ATTEMPTS = 3
RESTORE_RETRY_DELAY = 5.0
HDMI_WATCH_LIMIT = 360.0  # seconds; the HDMI step asks no more after this (the next answer ends it)

# --- What the session may send ----------------------------------------------------------------------------------
# Everything else is refused before it leaves this computer (SessionClient): volume and every other setting, Restart,
# anything to do with Wi-Fi or firmware, and every change to "Smartphones added" or its restriction.

ALLOWED_COMMANDS = frozenset(
    {op for op, _ in STATE_READS.values()}
    | {op for op, _ in STATIC_READS.values()}
    | {
        Op.GET_WEATHER,
        Op.GET_HTTP_ENDPOINT,
        Op.SET_POWER,  # to switch on for the checks, and back
        Op.SET_POSTER,
        Op.STOP_APP,
        Op.SET_NET_TOPOLOGY,  # these three are how a source is chosen
        Op.SET_WEATHER,
        Op.SET_DIRECTION,
        Op.SET_AUTO_DIRECTION,
    }
)
# The only network topologies the session may set: home (with or without poster) and wireless HDMI.
SAFE_TOPOLOGY_VALUES = (int(NetTopology.HOME), int(NetTopology.HOME_POSTER), int(NetTopology.WIRELESS_HDMI))
SAFE_TOPOLOGIES = frozenset(bytes([t]) for t in SAFE_TOPOLOGY_VALUES)
ALLOWED_BOND_OPS = frozenset({BondOp.GET_PIN_CODE_FLAG})  # the restriction flag, read with the state; nothing else
# Control point ops of the Bluetooth tunnel: GET and POST (docs/protocol.md, "Bluetooth tunnel"); no DELETE.
ALLOWED_TUNNEL_OPS = frozenset({0x40, 0x41, 0x42, 0x43, 0x50, 0x52, 0x53})
TUNNEL_CP = web_tunnel.CP
TUNNEL_PARAM = web_tunnel.PARAM

CHAR_NAMES = {
    CHAR_COMMAND: "command",
    CHAR_BOND_CONTROL: "bonding",
    CHAR_BONDED_LIST: "smartphones",
    CHAR_BONDING_NAME: "bonding-name",
    CHAR_SECURITY_PROBE: "security-probe",
    TUNNEL_CP: "tunnel-control",
    TUNNEL_PARAM: "tunnel-param",
}

# The app's picture for each Weather poster code (docs/protocol.md, "Weather"; from its AccuWeather mapping).
WEATHER_PICTURES = {
    1: "sunny / clear",
    2: "partly sunny",
    3: "hazy",
    4: "mostly cloudy",
    5: "cloudy",
    6: "dreary / fog",
    7: "showers",
    8: "rain / sleet",
    9: "flurries",
    10: "thunderstorms",
    11: "snow",
}
DIRECTION_NAMES = {
    Direction.MIRROR_HORIZONTAL: "mirror horizontal",
    Direction.ROTATE_180: "rotate 180°",
    Direction.NORMAL: "normal",
    Direction.MIRROR_VERTICAL: "mirror vertical",
}
CLOCK_1 = Poster.DIGITAL_CLOCK_2  # the app's "Clock 1", the factory default

FREE_THE_LINK = """\
The projector serves one Bluetooth client at a time. To free its connection:
  - Home Assistant: Settings -> Devices & services -> Sony LSPX-P1 Projector; on the projector's entry,
    the three dots -> Disable. Home Assistant lets go of the connection within a few seconds.
  - Sony's app: close it fully on the phone (swipe it away), or switch the phone's Bluetooth off.
  - Not fully off: a long press of the power button switches it off completely, and then it cannot be reached.
To give the connection back afterwards: the same page in Home Assistant, the disabled entry -> Enable."""

BUSY = (
    """\
Cannot reach the projector. Either another client holds its one connection (Home Assistant, Sony's app, another
computer), it is fully switched off (a long press of its power button), or it is out of range.

"""
    + FREE_THE_LINK
)


# --- Steps --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    issues: tuple[int, ...]
    minutes: str
    settles: str


STEPS = (
    Step(
        "weather",
        "Weather poster pictures",
        (2,),
        "3-4",
        "Whether SetWeather changes the Weather poster's picture, and what codes 3, 4 and 9 look like.",
    ),
    Step(
        "clock",
        "The clock over the Bluetooth tunnel (and Wi-Fi)",
        (1,),
        "2-3",
        "Whether the Bluetooth tunnel answers GET /v1/settings/time and sets the clock (Clock 1 jumps 90 minutes,"
        " then goes back); the Wi-Fi address the projector reports.",
    ),
    Step(
        "direction",
        "Picture direction",
        (),
        "2",
        "Which way each picture direction setting turns or mirrors the picture (docs/protocol.md lists the app's"
        " names; how the picture turns was never seen).",
    ),
    Step(
        "notifications",
        "Change notifications from the power button",
        (9,),
        "2-3",
        "Whether the projector notifies a connected controller of a change made at the projector itself.",
    ),
    Step(
        "hdmi",
        "HDMI status while a source is plugged in and out",
        (14,),
        "4-6",
        "Whether the HDMI status follows a source being plugged into the wireless HDMI unit. Needs an HDMI source.",
    ),
)
STEP_KEYS = tuple(step.key for step in STEPS)


class SkipStep(Exception):
    """End the current step (typed s, or the step cannot run); its changes are put back."""


class QuitSession(Exception):
    """End the session (typed q, or the input closed); everything is put back."""


class UnsafeCommand(Exception):
    """The session tried to send something it never should."""


class CannotRestore(Exception):
    """A change the session can never put back itself (it says how to by hand, and forgets it)."""


@dataclass(frozen=True)
class Answer:
    key: str
    label: str
    note: str


class Console(Protocol):
    """How the session talks to the person at the projector; tests hand in a scripted one."""

    def say(self, text: str) -> None: ...
    async def line(self, prompt: str) -> str: ...


class TerminalConsole:
    """Reads lines in a thread of its own, so a question never blocks the event loop and Ctrl-C still works."""

    def __init__(self) -> None:
        self._lines: asyncio.Queue[str | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def say(self, text: str) -> None:
        print(text, flush=True)

    def _start(self) -> asyncio.Queue[str | None]:
        if self._lines is None:
            self._lines = asyncio.Queue()
            self._loop = asyncio.get_running_loop()
            threading.Thread(target=self._read, daemon=True).start()
        return self._lines

    def _read(self) -> None:
        assert self._loop is not None and self._lines is not None
        while True:
            text = sys.stdin.readline()
            self._loop.call_soon_threadsafe(self._lines.put_nowait, text.rstrip("\n") if text else None)
            if not text:
                return

    async def waiting_line(self) -> str:
        """A line typed during a countdown (no prompt); what was typed before it started does not count."""
        lines = self._start()
        while not lines.empty():
            if lines.get_nowait() is None:
                raise EOFError
        text = await lines.get()
        if text is None:
            raise EOFError
        return text

    async def line(self, prompt: str) -> str:
        lines = self._start()
        while not lines.empty():  # typed ahead, while nothing was asked: not an answer to this
            if lines.get_nowait() is None:
                raise EOFError
        print(prompt, end="", flush=True)
        text = await lines.get()
        if text is None:
            raise EOFError
        return text


class Clock:
    """Time for the session's own waits; tests hand in one that passes at once."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --- The client, recording every frame and refusing what the session must never send --------------------------


class SessionClient(LSPXClient):
    """The integration's client, holding the link for the whole session, with every GATT write, read and
    notification passed to ``on_frame`` and every write checked against what the session may send."""

    def __init__(self, ble_device: BLEDevice, on_frame: Callable[[str, str, bytes], None]) -> None:
        super().__init__(ble_device, idle_disconnect=False)
        self._on_frame = on_frame

    async def _establish(self, *, use_services_cache: bool = True) -> Any:
        client = await super()._establish(use_services_cache=use_services_cache)
        self._instrument(client)
        return client

    def _instrument(self, client: Any) -> None:
        write, read, start_notify = client.write_gatt_char, client.read_gatt_char, client.start_notify

        async def write_gatt_char(char: str, data: bytes, *args: Any, **kwargs: Any) -> Any:
            self.check(char, bytes(data))
            self._on_frame("->", char, bytes(data))
            return await write(char, data, *args, **kwargs)

        async def read_gatt_char(char: str, *args: Any, **kwargs: Any) -> Any:
            data = await read(char, *args, **kwargs)
            self._on_frame("<- read", char, bytes(data))
            return data

        async def start_notify_recorded(char: str, callback: Callable[..., None], *args: Any, **kwargs: Any) -> Any:
            def recorded(sender: Any, data: bytearray) -> None:
                self._on_frame("<-", char, bytes(data))
                callback(sender, data)

            return await start_notify(char, recorded, *args, **kwargs)

        client.write_gatt_char = write_gatt_char
        client.read_gatt_char = read_gatt_char
        client.start_notify = start_notify_recorded

    def check(self, char: str, data: bytes) -> None:
        """Raise UnsafeCommand unless the session may write ``data`` to ``char``."""
        char = char.lower()
        if char == CHAR_COMMAND:
            op = int.from_bytes(data[:2], "big")
            if op not in ALLOWED_COMMANDS:
                raise UnsafeCommand(f"refusing to send {describe_op(op)}: the session never changes it")
            if op == Op.SET_NET_TOPOLOGY and data[2:] not in SAFE_TOPOLOGIES:
                # Only home (with or without poster) and wireless HDMI, never Wi-Fi Direct or anything else.
                raise UnsafeCommand(f"refusing SET_NET_TOPOLOGY {data[2:].hex()}")
            return
        if char == CHAR_BOND_CONTROL:
            # Two reads, nothing else: no pairing (the bonding window), and no change to "Smartphones added".
            if data[:1] and data[0] in ALLOWED_BOND_OPS and len(data) == 1:
                return
            raise UnsafeCommand(f"refusing bonding operation {data.hex()}: the session only reads the list")
        if char == TUNNEL_CP and len(data) >= 4 and int.from_bytes(data[2:4], "big") in ALLOWED_TUNNEL_OPS:
            return
        if char == TUNNEL_PARAM and tunnel_param_allowed(data):
            return
        raise UnsafeCommand(f"refusing to write {data.hex()} to {CHAR_NAMES.get(char, char)}")

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[Any]:
        """The secured Bluetooth link, held for one tunnel request (no command can run meanwhile)."""
        async with self._lock:
            client = await self._connect()
            await self._secure(client)
            try:
                yield client
            except BaseException:
                await self._drop()  # a link in an unknown state
                raise


def tunnel_param_allowed(data: bytes) -> bool:
    """Only the clock's path, and a body with nothing but the time (and the Wi-Fi request's time zone)."""
    if data == web_api.TIME_PATH.encode():
        return True
    try:
        body = json.loads(data)
    except ValueError:
        return False
    return isinstance(body, dict) and "time" in body and set(body) <= {"time", "timezone"}


# --- Results ------------------------------------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)


class Results:
    """One session's results directory: results.json, frames.jsonl, client.log and shareable.md."""

    def __init__(self, directory: Path, data: dict[str, Any] | None = None) -> None:
        self.directory = directory
        self.data: dict[str, Any] = data or {
            "format": 1,
            "tool": "tools/at_projector.py",
            "started": datetime.now().astimezone().isoformat(timespec="seconds"),
            "computer": {"platform": sys.platform, "python": platform.python_version()},
            "projector": {},
            "baseline": None,
            "steps": {},
            "pending_restores": [],
            "finished": False,
        }

    @classmethod
    def new(cls, root: Path) -> Results:
        directory = root / datetime.now().strftime("%Y-%m-%d-%H%M%S")
        suffix = 1
        while directory.exists():
            suffix += 1
            directory = root / f"{datetime.now():%Y-%m-%d-%H%M%S}-{suffix}"
        directory.mkdir(parents=True)
        return cls(directory)

    @classmethod
    def latest(cls, root: Path) -> Results | None:
        if not root.is_dir():
            return None
        for directory in sorted((d for d in root.iterdir() if (d / "results.json").is_file()), reverse=True):
            try:
                return cls(directory, json.loads((directory / "results.json").read_text(encoding="utf-8")))
            except ValueError:
                continue
        return None

    @property
    def frames_path(self) -> Path:
        return self.directory / "frames.jsonl"

    def step(self, key: str) -> dict[str, Any]:
        return self.data["steps"].setdefault(key, {"status": "not run", "events": []})

    def save(self, *, shareable_copy: bool = True) -> None:
        """Write results.json (at once, after every event) and, unless told not to, shareable.md."""
        self.data["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        path = self.directory / "results.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_jsonable(self.data), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        if shareable_copy:
            (self.directory / "shareable.md").write_text(shareable(self), encoding="utf-8")

    def frames(self) -> list[dict[str, Any]]:
        if not self.frames_path.is_file():
            return []
        return [json.loads(line) for line in self.frames_path.read_text(encoding="utf-8").splitlines() if line]


# --- Redaction for the shareable copy ---------------------------------------------------------------------------

MAC = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:_-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
PROJECTOR_NAME = re.compile(rf"\b{LOCAL_NAME_PREFIX}_[0-9A-Fa-f]{{4}}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Names that say nothing about anyone: kept, as they tell Home Assistant's entries and the session's apart.
GENERIC_NAMES = re.compile(r"^Home Assistant( \(dev\))?$")


def _hidden_ip(text: str) -> str:
    """What the shareable copy shows for an address: whether it is on the projector's own network, no more."""
    own = ipaddress.IPv4Address(text) in lspx_cli.DIRECT_NETWORK
    return "<192.168.8.x>" if own else "<home address>"


def _keep_ip(text: str) -> bool:
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return True  # not an address (a version number, say)
    return address.is_unspecified  # 192.168.8.x too may be a home router's (README, Clock)


class Redactor:
    """Takes out Bluetooth addresses, the projector's name, smartphone names and home-network addresses."""

    def __init__(self, results: Results) -> None:
        data = results.data
        self.address = str(data.get("projector", {}).get("address") or "")
        names: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("name"), str) and "allowed" in value:
                    names.add(value["name"])
                for v in value.values():
                    collect(v)
            elif isinstance(value, list):
                for v in value:
                    collect(v)

        collect(data)
        self.names = sorted((n for n in names if n and not GENERIC_NAMES.match(n)), key=len, reverse=True)

    def text(self, text: str) -> str:
        if self.address:
            text = re.sub(re.escape(self.address), "<projector address>", text, flags=re.IGNORECASE)
        text = MAC.sub("<address>", text)
        text = PROJECTOR_NAME.sub(f"{LOCAL_NAME_PREFIX}_xxxx", text)
        for i, name in enumerate(self.names, 1):
            for form in {name, json.dumps(name, ensure_ascii=False)[1:-1]}:  # as typed, and as events print it
                text = re.sub(rf"(?<!\w){re.escape(form)}(?!\w)", f"<smartphone {i}>", text)
        text = text.replace(str(Path.home()), "~")  # in traces: the user's name
        return IPV4.sub(lambda m: m.group(0) if _keep_ip(m.group(0)) else _hidden_ip(m.group(0)), text)

    def frame(self, frame: dict[str, Any]) -> str:
        data = bytes.fromhex(frame["hex"])
        char = frame["char"]
        shown = data.hex()
        if char in ("smartphones", "bonding-name"):
            shown = f"<{len(data)} bytes: smartphone names>"
        elif char == "command" and frame["dir"] == "<-" and data[1:3] == Op.GET_HTTP_ENDPOINT.to_bytes(2, "big"):
            address = data[5:].decode("ascii", "replace").strip("\x00 ")
            if address and not _keep_ip(address):
                shown = f"{data[:5].hex()} <address: {len(data) - 5} bytes>"
        return f"{frame['t']:9.3f}  {frame['step']:<13} {frame['dir']:<7} {char:<14} {shown}  {frame.get('what', '')}"


def describe_event(event: dict[str, Any]) -> str:
    fields = {k: v for k, v in event.items() if k not in ("t", "at", "kind")}
    if event["kind"] == "answer":
        note = f' — "{fields["note"]}"' if fields.get("note") else ""
        return f"Q: {fields['question']} -> **{fields['label']}**{note}"
    if event["kind"] == "say":
        return f"_{fields['text']}_"
    return f"{event['kind']}: " + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in fields.items())


def shareable(results: Results) -> str:
    """The results as Markdown, without addresses or names, to paste into an issue."""
    redact = Redactor(results)
    data = results.data
    frames = results.frames()
    lines = [
        "# At the projector: results",
        "",
        f"`tools/at_projector.py`, session of {data['started']}"
        + (f", last updated {data['updated']}" if data.get("updated") else "")
        + f", {data['computer']['platform']}, Python {data['computer']['python']}.",
        "Bluetooth addresses, the projector's name, smartphone names and home-network addresses are left out;"
        " notes are as typed.",
        "",
    ]
    baseline = data.get("baseline") or {}
    state = baseline.get("state") or {}
    if state:
        versions = ", ".join(
            f"{k} {state.get(k)}"
            for k in ("software_version", "syscon_version", "light_engine_version")
            if state.get(k)
        )
        lines += [f"Firmware: {versions or 'not read'}.", ""]
    if data.get("pending_restores"):
        lines += ["**Not put back yet:** " + "; ".join(p["label"] for p in data["pending_restores"]), ""]
    if data.get("differences"):
        lines += ["**Different from the start of the session:** " + "; ".join(data["differences"]), ""]
    for key in ("setup", *STEP_KEYS, "finish"):
        step_data = data["steps"].get(key)
        if not step_data:
            continue
        step = next((s for s in STEPS if s.key == key), None)
        title = step.title if step else key.capitalize()
        issues = " ".join(f"#{n}" for n in step.issues) if step and step.issues else ""
        lines += [f"## {title}" + (f" ({issues})" if issues else ""), "", f"Status: {step_data['status']}.", ""]
        lines += [f"- `{e['t']:8.1f} s` {describe_event(e)}" for e in step_data["events"]]
        step_frames = [f for f in frames if f["step"] == key]
        if step_frames:
            lines += [
                "",
                f"<details><summary>{len(step_frames)} Bluetooth frames</summary>",
                "",
                "```text",
                *(redact.frame(f) for f in step_frames),
                "```",
                "",
                "</details>",
            ]
        lines.append("")
    return redact.text("\n".join(lines))


# --- The session ----------------------------------------------------------------------------------------------------

# What a read, a probe or a restore may fail with, without failing the step: recorded as the answer instead.
READ_ERRORS = (
    CommandRejected,
    ProjectorBusyError,
    NotPairedError,
    BondCommandFailed,
    ProtocolError,
    BleakError,
    TimeoutError,
    web_api.WebApiError,
    web_tunnel.TunnelError,
)

FindDevice = Callable[[], Awaitable[BLEDevice | None]]
HttpSession = Callable[[], aiohttp.ClientSession]
TunnelRequest = Callable[[web_tunnel.Method, str | None], Awaitable[web_tunnel.Response]]


async def scan() -> BLEDevice | None:
    return await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or "").startswith(LOCAL_NAME_PREFIX), timeout=SCAN_TIMEOUT
    )


@dataclass(eq=False)  # one change is one object: two alike are still two (removed by identity)
class Change:
    """Something the session changed, and how to put it back (see Session._restore)."""

    kind: str
    value: Any
    label: str
    scope: str  # "step": put back at the end of the step; "session": at the end of the session

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": _jsonable(self.value), "label": self.label, "scope": self.scope}


# What to do by hand when the session could not put something back.
MANUAL_RESTORE = {
    "power": "switch the projector {value} with its power button (or in Home Assistant)",
    "source": "choose the source it showed before ({value}) in Home Assistant or Sony's app",
    "weather": "run tools/lspx_cli.py weather {value}; or leave it to Home Assistant, which sends its own code"
    " while the Weather poster shows if a weather entity is chosen (Sony's app never sends one)",
    "direction": "set the picture direction back to {value} (tools/lspx_cli.py set direction {value}, or Home"
    " Assistant's picture direction)",
    "auto_direction": "switch auto picture direction {value} (tools/lspx_cli.py set auto_direction {value})",
    "clock": "open Sony's app once: it sets the clock when it starts",
}


def manual_restore(kind: str, value: Any) -> str:
    """How to put back by hand what the session could not."""
    if kind == "source" and isinstance(value, dict):
        value = describe_source(value) + (
            f", network topology {value['net_topology']}" if value.get("net_topology") in (16, 17) else ""
        )
    if kind == "power":
        value = "on" if value else "to standby"
    elif kind == "auto_direction":
        value = "on" if value else "off"
    return MANUAL_RESTORE.get(kind, "put it back by hand").format(value=value)


@dataclass
class Session:
    results: Results
    console: Console
    find_device: FindDevice = scan
    http_session: HttpSession = aiohttp.ClientSession
    clock: Clock = field(default_factory=Clock)
    steps: Sequence[str] = STEP_KEYS
    client: SessionClient | None = None
    changes: list[Change] = field(default_factory=list)
    current: str = "setup"
    tunnel_seq: int = 0
    interrupts: int = 0  # Ctrl-C (task cancellations) so far
    listening: bool = False  # nothing is asked of the projector: any frame on the command characteristic is its own
    unasked_frames: int = 0
    weather_before: int | None = None
    start: dict[str, Any] | None = None  # what this run started from (read_start)
    no_more_restores: bool = False
    source_switched: bool = False  # this run has changed the source: what shows is its own doing
    weather_acknowledged: bool = False
    _t0: float = 0.0
    _frames: Any = None
    _log_handler: logging.Handler | None = None
    tunnel_request: TunnelRequest | None = None  # tests hand in a fake tunnel

    # --- recording ----------------------------------------------------------------------------------------------

    def now(self) -> float:
        return round(self.clock.monotonic() - self._t0, 3)

    def event(self, kind: str, **fields: Any) -> None:
        entry = {"t": self.now(), "at": datetime.now().astimezone().isoformat(timespec="seconds"), "kind": kind}
        self.results.step(self.current)["events"].append(entry | _jsonable(fields))
        self.results.save(shareable_copy=False)

    def say(self, text: str) -> None:
        self.console.say(text)

    def note(self, text: str) -> None:
        """Say ``text`` and record it with the step."""
        self.console.say(text)
        self.event("say", text=text)

    def on_frame(self, direction: str, char: str, data: bytes) -> None:
        char = char.lower()
        what = ""
        if char == CHAR_COMMAND and direction == "->" and len(data) >= 2:
            what = describe_op(int.from_bytes(data[:2], "big"))
        elif char == CHAR_COMMAND and direction == "<-" and len(data) >= 3:
            result, op = data[0], int.from_bytes(data[1:3], "big")
            label = {2: "ACK", 3: "NAK"}.get(result, f"result {result}")
            what = f"{label} {describe_op(op)}"
            if result == 3 and len(data) > 3:
                code = data[3]
                what += f" error 0x{code:02x}" + (
                    f" ({ErrorCode(code).name})" if code in ErrorCode._value2member_map_ else ""
                )
        frame = {"t": self.now(), "step": self.current, "dir": direction, "char": CHAR_NAMES.get(char, char)}
        frame |= {"hex": data.hex(), "what": what}
        if self.listening and char == CHAR_COMMAND and direction == "<-":
            self.unasked_frames += 1
        if self._frames is not None:
            self._frames.write(json.dumps(frame) + "\n")
            self._frames.flush()

    def on_notification(self, notification: Notification) -> None:
        self.say(f"The projector sent a change notification: {notification.describe()}")
        self.event("notification", op=notification.op, result=notification.result, data=notification.data.hex())

    # --- asking -----------------------------------------------------------------------------------------------------

    async def ask(self, question: str, choices: dict[str, str], *, default: str | None = None) -> Answer:
        """Ask until one of ``choices`` (or Enter, for ``default``) is typed; a note may follow the letter.

        s ends the step and q the session (both put back what they changed), so neither is ever a choice.
        """
        assert not {"s", "q"} & set(choices)
        menu = "  ".join(f"[{k or 'Enter'}] {v}" for k, v in choices.items())
        self.say(f"\n{question}\n  {menu}   (s: skip this step, q: quit; a note may follow the letter)")
        while True:
            try:
                text = (await self.console.line("> ")).strip()
            except EOFError:
                raise QuitSession("the input closed") from None
            key, _, note = text.partition(" ")
            if not text and default is not None:
                key, note = default, ""
            if key in ("s", "q") and key not in choices:
                self.event("answer", question=question, key=key, label="skip" if key == "s" else "quit", note=note)
                raise SkipStep("skipped") if key == "s" else QuitSession("quit")
            if key in choices:
                answer = Answer(key, choices[key], note.strip())
                self.event("answer", question=question, key=key, label=answer.label, note=answer.note)
                return answer
            self.say(f"Type one of: {', '.join(k or 'Enter' for k in choices)} (or s, q).")

    async def enter(self, text: str) -> Answer:
        """Wait for Enter (after ``text``), with an optional note."""
        return await self.ask(text, {"": "done"}, default="")

    async def countdown(self, seconds: float, what: str) -> None:
        """Wait, saying how long is left every 15 s. Sends nothing meanwhile. Enter ends the wait early, s the step
        and q the session (each putting back what it changed)."""
        self.say(f"{what}: {seconds:.0f} s (Enter: stop waiting, s: skip this step, q: quit)")
        waiting = getattr(self.console, "waiting_line", None)
        typed = asyncio.get_running_loop().create_task(waiting()) if waiting else None
        end = self.clock.monotonic() + seconds
        try:
            while (left := end - self.clock.monotonic()) > 0:
                nap = asyncio.get_running_loop().create_task(self.clock.sleep(min(left, 15.0)))
                await asyncio.wait({nap, *([typed] if typed else [])}, return_when=asyncio.FIRST_COMPLETED)
                if typed is not None and typed.done():
                    nap.cancel()
                    try:
                        key = (typed.result() or "").strip().partition(" ")[0]
                    except EOFError:
                        raise QuitSession("the input closed") from None
                    self.event("countdown-ended", key=key, left=round(left))
                    if key == "s":
                        raise SkipStep("skipped")
                    if key == "q":
                        raise QuitSession("quit")
                    return
                if (left := end - self.clock.monotonic()) > 1:
                    self.say(f"  ... {left:.0f} s")
        finally:
            if typed is not None and not typed.done():
                typed.cancel()

    # --- talking to the projector -----------------------------------------------------------------------------------

    @property
    def c(self) -> SessionClient:
        assert self.client is not None
        return self.client

    async def read(self, label: str, call: Callable[[], Awaitable[Any]]) -> tuple[Any, str | None]:
        """Run one read; record its value or how it failed (value None and the error)."""
        began = self.clock.monotonic()
        try:
            value = await call()
        except READ_ERRORS as err:
            error = describe_error(err)
        else:
            self.event("read", what=label, value=value, seconds=round(self.clock.monotonic() - began, 2))
            return value, None
        self.event("read", what=label, error=error, seconds=round(self.clock.monotonic() - began, 2))
        return None, error

    async def connect(self) -> None:
        """Find the projector and reach it, saying how to free the link while another client holds it.

        Raises NotPairedError when the projector refuses this computer (not registered: tools/lspx_cli.py pair).
        """
        while True:
            if self.client is not None and self.client.is_connected:
                # Holding the link already: a connected projector need not be found by a scan.
                device = self.client._ble_device
            else:
                device = await self.find_device()
            if device is None:
                self.event("not-found", seconds=SCAN_TIMEOUT)
            else:
                if self.client is None:
                    self.client = SessionClient(device, self.on_frame)
                    self.client.set_notification_callback(self.on_notification)
                    self.results.data["projector"] = {"address": device.address, "name": device.name}
                else:
                    self.client.set_ble_device(device)
                began = self.clock.monotonic()
                try:
                    await self.c.command(Op.GET_POWER)
                except (CommandRejected, ProjectorBusyError) as err:
                    # It answered: the link works, whatever the power read said.
                    self.event("connected", seconds=round(self.clock.monotonic() - began, 1), power=describe_error(err))
                    return
                except (BleakError, TimeoutError) as err:
                    self.event(
                        "connect-failed", error=describe_error(err), seconds=round(self.clock.monotonic() - began, 1)
                    )
                else:
                    self.event("connected", seconds=round(self.clock.monotonic() - began, 1))
                    return
            self.say("\n" + BUSY)
            await self.ask("Try again?", {"": "try again"}, default="")

    async def wait_until_up(self) -> float | None:
        """Wait for the main board after a power-on: until GetAppState answers something other than 0 or a refusal.

        Returns the seconds it took, or None if START_UP_LIMIT passed first.
        """
        began = self.clock.monotonic()
        said = began
        while (elapsed := self.clock.monotonic() - began) < START_UP_LIMIT:
            try:
                app = decode_u8(await self.c.command(Op.GET_APP_STATE))
            except (CommandRejected, ProjectorBusyError) as err:
                app, error = None, describe_error(err)
            else:
                error = None
            if app:
                self.event("main-board-up", seconds=round(elapsed, 1), app_state=app)
                return elapsed
            if self.clock.monotonic() - said >= 15:
                said = self.clock.monotonic()
                self.say(f"  still starting up after {elapsed:.0f} s ({error or f'app state {app}'})")
            await self.clock.sleep(START_UP_PROBE_INTERVAL)
        self.event("main-board-not-up", seconds=START_UP_LIMIT)
        return None

    def change(self, kind: str, value: Any, label: str, *, scope: str = "step") -> None:
        """Note, before making a change, how to put it back; saved at once, so a later run can do it."""
        self.changes.append(Change(kind, value, label, scope))
        self._save_pending()

    def _save_pending(self) -> None:
        self.results.data["pending_restores"] = [c.as_dict() for c in self.changes]
        self.results.save()

    def has_change(self, kind: str) -> bool:
        return any(c.kind == kind for c in self.changes)

    async def restore(self, *, scope: str | None = None) -> None:
        """Put back what was changed (all of it, or one scope), last change first; keep what fails as pending."""
        for change in reversed([c for c in self.changes if scope is None or c.scope == scope]):
            for attempt in range(1, RESTORE_ATTEMPTS + 1):
                try:
                    await self._restore(change)
                except (UnsafeCommand, CannotRestore) as err:
                    # Can never be done by the session: say so once, and do not keep it for later runs.
                    self.changes.remove(change)
                    self._save_pending()
                    manual = manual_restore(change.kind, change.value)
                    self.note(f"Cannot put back {change.label}: {err}. By hand: {manual}.")
                    break
                except Exception as err:
                    self.event("restore-failed", what=change.label, attempt=attempt, error=describe_error(err))
                    if attempt < RESTORE_ATTEMPTS:
                        self.say(f"  putting back {change.label} failed ({describe_error(err)}); trying again")
                        await self.clock.sleep(RESTORE_RETRY_DELAY)
                    continue
                self.changes.remove(change)
                self.event("restored", what=change.label)
                self._save_pending()
                break
        for change in self.changes:
            if scope is None or change.scope == scope:
                manual = manual_restore(change.kind, change.value)
                self.note(f"NOT PUT BACK: {change.label}. By hand: {manual}. Or run this tool with --restore-only.")

    async def _restore(self, change: Change) -> None:
        kind, value = change.kind, change.value
        if kind == "power":
            await self.c.set_power(bool(value))
            if value:
                await self.wait_until_up()
            else:
                await self.settle_in_standby()
        elif kind == "source":
            async with self.main_board():
                await self.restore_source(value)
        elif kind == "weather":
            async with self.main_board():
                try:
                    await self.c.set_weather(int(value))
                except CommandRejected as err:
                    if err.code != ErrorCode.INVALID_ARGUMENT:
                        raise  # tried again (UNKNOWN_COMMAND also comes and goes around power changes)
                    raise CannotRestore(f"the projector refuses SetWeather {value} ({err})") from err
        elif kind == "direction":
            await self.c.set_direction(int(value), disable_auto=False)
        elif kind == "auto_direction":
            await self.c.set_auto_direction(bool(value))
        elif kind == "clock":
            host = value.get("host")
            async with self.main_board():
                if value["route"] == "wifi":
                    host = await self.wifi_host()
                    if host != value.get("host"):
                        # The address may be another device's by now: send nothing there.
                        raise CannotRestore(f"the projector's Wi-Fi address is now {host}, not {value.get('host')}")
                await self.set_clock(
                    value["route"], web_api.local_unix_seconds(datetime.now().astimezone()) + value["offset"], host
                )
        else:
            raise CannotRestore(f"nothing known about {kind!r}")

    async def restore_source(self, value: dict[str, Any]) -> None:
        """Show the source from the start again (on: see main_board)."""
        if not restorable(value):
            raise CannotRestore(f"the session cannot choose {describe_source(value)}")
        app = await self._u8(Op.GET_APP_STATE)
        # The poster theme (GetPoster) is read on HDMI too: put the one from the start back first, while on a poster
        # (a switch between posters takes a second).
        if (
            value.get("app_state") == AppState.HDMI
            and value.get("poster")
            and app == AppState.POSTER
            and decode_u16(await self.c.command(Op.GET_POSTER)) != value["poster"]
        ):
            await self.show_source({**value, "app_state": AppState.POSTER})
        await self.show_source(value)
        topology = value.get("net_topology")
        if value.get("app_state") == AppState.POSTER and topology in (NetTopology.HOME, NetTopology.HOME_POSTER):
            # Leaving HDMI sets the topology the app sets (17 from firmware 3.0.0 on); put back the one read then.
            now = await self._u8(Op.GET_NET_TOPOLOGY)
            if now != topology:
                await self.c.command(Op.SET_NET_TOPOLOGY, payload_u8(topology), reply_timeout=SWITCH_REPLY_TIMEOUT)
                self.event("topology-restored", before=now, to=topology, read_back=await self._u8(Op.GET_NET_TOPOLOGY))

    @contextlib.asynccontextmanager
    async def main_board(self) -> AsyncIterator[None]:
        """For putting back something the main board holds: in standby it does not answer (and keeps what it shows
        for the next power-on), so switch on first, and back to standby afterwards unless a noted power change will
        set the power anyway."""
        if await self._power():
            yield
            return
        self.note(
            "  In standby the projector cannot take this: switching on to put it back, then back to standby (after a"
            " long standby, switching on takes 45-100 s)."
        )
        # Noted first, like every change: if this is cut short, the power is still put back (or left for later).
        back = None
        if not self.has_change("power"):
            back = Change("power", False, "power: back to standby", "session")
            self.changes.insert(0, back)  # put back after everything else
            self._save_pending()
        try:
            await self.c.set_power(True)
            await self.wait_until_up()
            yield
        finally:
            # Only the change noted here (by identity: another one may look the same).
            if back is not None and any(c is back for c in self.changes):
                for attempt in range(1, RESTORE_ATTEMPTS + 1):
                    try:
                        await self._restore(back)
                    except Exception as err:
                        self.event("restore-failed", what=back.label, attempt=attempt, error=describe_error(err))
                        if attempt < RESTORE_ATTEMPTS:
                            await self.clock.sleep(RESTORE_RETRY_DELAY)
                        continue
                    self.changes = [c for c in self.changes if c is not back]
                    self._save_pending()
                    break

    async def settle_in_standby(self) -> None:
        """After switching to standby, wait until the power reads so (reads right after switching are unreliable)."""
        began = self.clock.monotonic()
        while self.clock.monotonic() - began < STANDBY_SETTLE:
            with contextlib.suppress(*READ_ERRORS):
                if not await self._power():
                    self.event("in-standby", seconds=round(self.clock.monotonic() - began, 1))
                    return
            await self.clock.sleep(1.5)
        self.event("not-in-standby-yet", seconds=STANDBY_SETTLE)
        raise RuntimeError(f"the power did not read standby within {STANDBY_SETTLE:.0f} s")

    async def wifi_host(self) -> str | None:
        """The projector's address on its Wi-Fi, if the integration would send HTTP there (as it decides)."""
        endpoint = decode_http_endpoint(await self.c.command(Op.GET_HTTP_ENDPOINT))
        topology = await self._u8(Op.GET_NET_TOPOLOGY)
        return endpoint.host if lspx_cli.http_refusal(endpoint, topology) is None else None

    # --- what the projector shows -------------------------------------------------------------------------------

    def start_data(self) -> dict[str, Any]:
        """What this run started from (see read_start); before it is read, the session's baseline."""
        return self.start or self.results.data.get("baseline") or {}

    def baseline_state(self) -> dict[str, Any]:
        return self.start_data().get("state") or {}

    def baseline_source(self) -> dict[str, Any]:
        """What it showed at the start, with what is needed to choose it again (on a later run too)."""
        state = self.baseline_state()
        return {
            "app_state": state.get("app_state"),
            "poster": state.get("poster"),
            "net_topology": state.get("net_topology"),
            "software_version": state.get("software_version"),
        }

    async def show_source(self, source: dict[str, Any]) -> None:
        if source.get("app_state") == AppState.HDMI:
            await self.c.show_hdmi()
        elif source.get("app_state") == AppState.POSTER and source.get("poster"):
            version = source.get("software_version") or self.baseline_state().get("software_version")
            await self.c.show_poster(int(source["poster"]), software_version=version)
        else:
            raise CannotRestore(f"the session cannot choose {describe_source(source)}")

    async def ensure_on(self) -> None:
        """Switch on if in standby (back to standby at the end if it was then), and wait for the main board."""
        if not await self._power():
            # Back to how it was at the start of the session: in standby, unless it was on then (and someone has
            # switched it to standby since, with its power button in the notifications step, say).
            if not self.has_change("power") and self.baseline_state().get("power") is not True:
                self.change("power", False, "power: back to standby", scope="session")
            self.note("Switching the projector on. After more than about 30 minutes of standby this takes 45-100 s.")
            await self.c.set_power(True)
        took = await self.wait_until_up()  # at once when it is up already
        if took is None:
            self.note("It did not say it was up within 150 s; going on anyway.")
        elif took > 5:
            self.note(f"It is up, after {took:.0f} s.")
        await self.fill_baseline()

    async def fill_baseline(self) -> None:
        """Read what the main board did not answer at the start (asleep in a long standby), before changing any of it.

        Switched on, it shows what it showed before standby (docs/protocol.md), so this is the source to put back.
        """
        state = self.baseline_state()
        if not state or self.source_switched:
            return  # once the session has switched, what shows is its own doing, not the start
        reads = {
            "app_state": lambda: self._u8(Op.GET_APP_STATE),
            "poster": lambda: self._u16(Op.GET_POSTER),
            "net_topology": lambda: self._u8(Op.GET_NET_TOPOLOGY),
            "software_version": lambda: self._string(Op.GET_SOC_SOFTWARE_VERSION),
        }
        # App state 0 is "not yet" too, as the integration counts it (seen for a while after Restart).
        missing = [attr for attr in reads if state.get(attr) is None or (attr == "app_state" and state[attr] == 0)]
        if not missing:
            return
        for attr in missing:
            value, _ = await self.read(f"{attr} (not answered at the start)", reads[attr])
            state[attr] = value
        self.results.save()
        self.note(f"At the start it was set to show {describe_source(self.baseline_source())}.")

    async def _u16(self, op: Op) -> int:
        return decode_u16(await self.c.command(op))

    async def _string(self, op: Op) -> str:
        return decode_string(await self.c.command(op))

    async def note_source_change(self) -> None:
        """Before the first change of source: note that the one from the start comes back at the end (or, when it
        cannot, ask first)."""
        if self.has_change("source"):
            return
        start = self.baseline_source()
        if restorable(start):
            self.change("source", start, f"source: back to {describe_source(start)}", scope="session")
        elif not self.source_switched:
            answer = await self.ask(
                f"At the start it showed {describe_source(start)}, which the session cannot choose again: after"
                " this step it shows a poster (or HDMI). Go on?",
                {"y": "go on", "": "no: skip this step"},
                default="",
            )
            if answer.key != "y":
                raise SkipStep("the source from the start could not be put back")
        self.source_switched = True

    async def ensure_source(self, source: dict[str, Any], label: str) -> None:
        """Show ``source`` (app_state, poster), noting first that the one at the start comes back at the end."""
        await self.ensure_on()
        app = await self._u8(Op.GET_APP_STATE)
        poster = decode_u16(await self.c.command(Op.GET_POSTER)) if app == AppState.POSTER else None
        if app == source["app_state"] and (app != AppState.POSTER or poster == source.get("poster")):
            return
        start = self.baseline_source()
        through_hdmi = AppState.HDMI in (app, source["app_state"])
        if through_hdmi and start.get("net_topology") not in (None, *SAFE_TOPOLOGY_VALUES):
            # Leaving HDMI sets a home topology; the session never sets Wi-Fi Direct (guest) or anything else back.
            await self.ask(
                f"At the start the network topology was {start['net_topology']} (Wi-Fi Direct, say), which the session"
                " cannot set again once it has gone through HDMI (switch Wi-Fi Direct on again in Sony's app"
                " afterwards). Go on?",
                {"y": "go on"},
            )
        await self.note_source_change()
        took = "about 17 s" if through_hdmi else "a few seconds"
        self.note(f"Showing {label} ({took}).")
        began = self.clock.monotonic()
        if (
            source["app_state"] == AppState.HDMI
            and app == AppState.POSTER
            and start.get("poster") not in (None, poster)
        ):
            # The poster theme (GetPoster) is read on HDMI too: make it the one from the start before leaving posters.
            await self.show_source({**start, "app_state": AppState.POSTER})
        await self.show_source(source)
        self.event("source-switched", to=label, seconds=round(self.clock.monotonic() - began, 1))

    async def ensure_poster(self, theme: Poster) -> None:
        await self.ensure_source(
            {"app_state": AppState.POSTER, "poster": int(theme)}, f"the {POSTER_NAMES[theme]} poster"
        )

    # --- the session ------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        self._t0 = self.clock.monotonic() - float(self.results.data.get("elapsed", 0))
        self._frames = self.results.frames_path.open("a", encoding="utf-8")
        self._log_handler = logging.FileHandler(self.results.directory / "client.log", encoding="utf-8")
        self._log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger = logging.getLogger(LSPXClient.__module__)
        logger.addHandler(self._log_handler)
        level, logger.level = logger.level, logging.DEBUG
        try:
            try:
                await self.setup()
                for key in self.steps:
                    await self.run_step(next(s for s in STEPS if s.key == key))
            except (QuitSession, SkipStep) as err:  # s outside a step ends the session too
                self.say(f"Ending the session ({err}).")
            except asyncio.CancelledError:
                self.interrupted()
            # Put back what is left. Ctrl-C (a cancel) while doing so starts it again, from what is still left; a
            # second interrupt of the session ends it without Bluetooth, with what is left saved for --restore-only.
            # (When run from a terminal, asyncio turns a second Ctrl-C into KeyboardInterrupt, which quits at once.)
            while True:
                try:
                    await self.finish(bluetooth=self.interrupts < 2)
                    break
                except asyncio.CancelledError:
                    self.interrupted()
        finally:
            logger.removeHandler(self._log_handler)
            logger.setLevel(level)
            self._log_handler.close()
            self.results.data["elapsed"] = self.now()
            self.results.save()
            self._frames.close()
            if self.client is not None:
                with contextlib.suppress(Exception):
                    await self.client.disconnect()

    def interrupted(self) -> None:
        """A Ctrl-C (the task was cancelled): count it, and let what follows wait (and time out) as usual."""
        task = asyncio.current_task()
        if task is None or not task.cancelling():
            return  # the same interrupt, passed on from a step
        while task.uncancel():
            pass
        self.interrupts += 1
        self.current = "finish"
        if self.interrupts == 1:
            self.note("Interrupted: putting back what the session changed (Ctrl-C again to quit at once).")
        else:
            self.note("Interrupted again: stopping without putting back the rest.")

    async def setup(self) -> None:
        self.current = "setup"
        self.say(intro(self.steps))
        await self.ask("Ready, with the integration's entry disabled and Sony's app closed?", {"": "start"}, default="")
        try:
            await self.connect()
        except NotPairedError as err:
            # The session never pairs (that adds an entry to "Smartphones added"): registering is a separate step.
            self.say(
                "\nThe projector does not accept this computer: it is not registered with it (or its entry was"
                " blocked or deleted in Sony's app). Register it once with:\n"
                "    .venv/bin/python tools/lspx_cli.py pair\n"
                '("Restrict adding smartphones" must be off in Sony\'s app; close the app first), then run this again.'
            )
            raise QuitSession("this computer is not registered with the projector") from err
        if self.results.data.get("pending_restores"):
            await self.restore_pending()
        if not self.steps:
            # --restore-only: nothing more to change, so the check at the end is against the session's own start.
            self.results.step("setup")["status"] = "done"
            self.results.save()
            return
        first = self.results.data.get("baseline") is None
        if not first:
            self.note("Continuing the session: reading the projector again, as this run puts back what it finds now.")
        await self.read_start(first=first)
        self.results.step("setup")["status"] = "done"
        self.results.save()

    async def restore_pending(self) -> None:
        pending = self.results.data["pending_restores"]
        self.note("Putting back what an earlier run left changed: " + "; ".join(p["label"] for p in pending))
        self.changes = [Change(**p) for p in pending]
        await self.restore()
        if self.changes:
            answer = await self.ask(
                "Put these back by hand as said above?",
                {"y": "done: forget them", "": "not now: keep them noted and quit"},
                default="",
            )
            if answer.key != "y":
                self.no_more_restores = True  # as chosen: not tried again at the end
                raise QuitSession("not everything could be put back")
            self.changes = []
            self._save_pending()

    async def read_start(self, *, first: bool) -> None:
        """What this run starts from, and so puts back: the session's baseline on its first run, read again on each
        later one (the projector may have been used in between)."""
        self.note("Reading everything first (about 10 s), to put it back at the end.")
        state, error = await self.read("state", self.c.read_state)
        if state is None:
            # Switched on a moment ago, say: GetPower is refused while it starts. Wait for it once.
            self.note(f"The projector could not be read ({error}); waiting for it to start up, then trying again.")
            await self.wait_until_up()
            state, error = await self.read("state", self.c.read_state)
        if state is None:
            raise QuitSession(f"the projector could not be read: {error}")
        weather, _ = await self.read("weather code", self.c.get_weather)
        start = {"state": _jsonable(state), "weather": weather}
        if first:
            self.results.data["baseline"] = start
        else:
            self.results.data.setdefault("later_starts", []).append(start)
        self.start = start
        self.results.save()
        self.note(f"Power {'on' if state.power else 'standby'}, showing {describe_source(self.baseline_source())}.")
        if state.app_state not in (None, 0, AppState.POSTER, AppState.HDMI):
            await self.ask(
                f"It shows {describe_source(self.baseline_source())}, which the session cannot bring back at the end"
                " (only posters and HDMI). Go on anyway?",
                {"y": "go on"},
            )

    async def run_step(self, step: Step) -> None:
        self.current = step.key
        record = self.results.step(step.key)
        if record["status"] == "done":
            self.say(f"{step.title}: done already in this session (--new starts a new one).")
            return
        issues = f" (#{', #'.join(map(str, step.issues))})" if step.issues else ""
        self.say(f"\n=== {step.title}{issues}, about {step.minutes} min ===\n{step.settles}")
        started = False
        try:
            await self.ask("Start?", {"": "start"}, default="")
            started = True
            record["status"] = "running"
            self.event("started")
            await getattr(self, f"step_{step.key}")()
            record["status"] = "done"
        except SkipStep as err:
            record["status"] = f"ended early ({err})" if started else "skipped"
        except QuitSession:
            record["status"] = "ended early (quit)" if started else record["status"]
            await self.end_step(record, ending=True)
            raise
        except asyncio.CancelledError:
            record["status"] = "interrupted"
            self.interrupted()
            await self.end_step(record, ending=True)
            raise
        except Exception as err:
            # Anything unexpected ends this step only; the frames and the trace show what happened.
            record["status"] = "failed"
            self.event("failed", error=describe_error(err), trace=traceback.format_exc(limit=6))
            self.note(f"The step failed: {describe_error(err)}")
        # Not in a finally: on KeyboardInterrupt (a second Ctrl-C from a terminal) nothing more is sent.
        await self.end_step(record)

    async def end_step(self, record: dict[str, Any], *, ending: bool = False) -> None:
        """Put back what the step changed; unless the session is ending anyway, ask before going on if that failed."""
        self.event("ended", status=record["status"])
        await self.restore(scope="step")
        self.results.save()
        if any(c.scope == "step" for c in self.changes) and not ending:
            # Not put back: the next steps would change more on top of it.
            await self.ask(
                "This step's changes are not all put back (see above). Go on with the next step?",
                {"": "no: end the session, putting back what it can", "g": "go on"},
                default="",
            )
            if self.results.step(self.current)["events"][-1].get("key") != "g":
                raise QuitSession("a step's changes are not all put back")

    async def finish(self, *, bluetooth: bool = True) -> None:
        self.current = "finish"
        record = self.results.step("finish")
        if self.client is not None and bluetooth and not self.no_more_restores:
            await self.restore()
            await self.compare_with_baseline()
        if self.steps:  # --restore-only leaves the session as it was: still to be continued, if it was
            # Every step of this run, and every step an earlier run of the session got to, is done or skipped.
            steps = self.results.data["steps"]
            recorded = [k for k in STEP_KEYS if steps.get(k, {}).get("status") not in (None, "not run")]
            self.results.data["finished"] = (
                all(self.results.step(k)["status"] in ("done", "skipped") for k in {*self.steps, *recorded})
                and not self.results.data["pending_restores"]
            )
        record["status"] = "done"
        self.results.save()
        lines = [
            "",
            "=== Done ===",
            f"Results: {self.results.directory}",
            f"  paste into the issues: {self.results.directory / 'shareable.md'}",
        ]
        if pending := self.results.data.get("pending_restores"):
            lines.append("NOT PUT BACK (run this tool with --restore-only, or by hand):")
            lines += [f"  - {c['label']}: {manual_restore(c['kind'], c['value'])}" for c in pending]
        lines.append("Give the connection back: in Home Assistant, enable the integration's entry again.")
        self.say("\n".join(lines))

    async def compare_with_baseline(self) -> None:
        start = self.baseline_state()
        if not start:
            return
        state, _ = await self.read("state", self.c.read_state)
        if state is None:
            self.note("Could not read the projector at the end, so not checked against the start.")
            return
        weather, _ = await self.read("weather code", self.c.get_weather)
        now = _jsonable(state)
        keys = [
            "power",
            "app_state",
            "poster",
            "net_topology",
            "direction",
            "auto_direction",
            "auto_standby",
            "restrict_new_controllers",
        ]
        differences = [
            f"{k}: {start[k]} at the start, {now[k]} now"
            for k in keys
            if start.get(k) is not None and now.get(k) is not None and start[k] != now[k]
        ]
        start_weather = self.start_data().get("weather")
        if start_weather is not None and weather is not None and weather != start_weather:
            differences.append(f"weather code: {start_weather} at the start, {weather} now")
        listed = lambda s: sorted((c["id"], c["allowed"]) for c in s.get("controllers") or [])  # noqa: E731
        if start.get("controllers") is not None and now.get("controllers") is not None and listed(start) != listed(now):
            differences.append("Smartphones added differs from the start (ids and allowed)")
        self.results.data["final"] = {"state": now, "weather": weather}
        self.results.data["differences"] = differences
        self.note(
            "Everything the session checks is as at the start."
            if not differences
            else "DIFFERENT FROM THE START: " + "; ".join(differences)
        )

    # --- the steps --------------------------------------------------------------------------------------------------

    async def step_weather(self) -> None:
        await self.ensure_poster(Poster.WEATHER)
        before, error = await self.read("weather code before", self.c.get_weather)
        if before not in WEATHER_CODES:
            # None (not read), or a number that is none of the app's codes (0, say), which SetWeather may refuse.
            why = error or f"it holds {before}, none of the app's codes 1-11"
            await self.ask(f"The code it holds cannot be put back exactly ({why}). Go on?", {"y": "go on"})
        self.weather_before, self.weather_acknowledged = before, False  # noted before each send (send_weather)
        codes = [c for c in (7, 11, 1, 2) if c != before][:3]  # never the one it holds: each must be a change
        self.say("Watch the Weather poster's picture. Each code is sent once; the projector is asked what it holds.")
        changed = False
        for code in codes:
            await self.send_weather(code)
            answer = await self.ask(
                f"Did the picture change? Code {code} is the app's picture for {WEATHER_PICTURES[code]}.",
                {"y": "yes, to that", "o": "yes, to something else", "n": "no change", "u": "not sure"},
            )
            changed |= answer.key in ("y", "o")
        if not changed:
            self.note("Showing Clock 1 and the Weather poster again, in case the picture is drawn only when shown.")
            await self.note_source_change()
            await self.show_source({**self.baseline_source(), "app_state": AppState.POSTER, "poster": int(CLOCK_1)})
            await self.show_source(
                {**self.baseline_source(), "app_state": AppState.POSTER, "poster": int(Poster.WEATHER)}
            )
            answer = await self.ask(
                f"Does it now show {WEATHER_PICTURES[codes[-1]]} (code {codes[-1]})?",
                {"y": "yes", "o": "another picture", "n": "the same as before"},
            )
            changed = answer.key in ("y", "o")
        if not changed:
            self.note("So SetWeather does not change the picture here; codes 3, 4 and 9 are not tried.")
            return
        for code in (3, 4, 9):
            await self.send_weather(code)
            await self.ask(
                f"What does code {code} show? (The app's picture for {WEATHER_PICTURES[code]}.)",
                {"y": "that", "o": "something else (say what)", "n": "no change"},
            )

    async def send_weather(self, code: int) -> None:
        before = self.weather_before
        if before in WEATHER_CODES and not self.has_change("weather"):
            self.change("weather", before, f"weather code: back to {before}")
        began = self.clock.monotonic()
        try:
            await self.c.set_weather(code)
        except CommandRejected as err:
            self.event("set-weather", code=code, rejected=describe_error(err))
            self.note(f"SetWeather {code} was refused: {describe_error(err)}")
            held, _ = await self.read("weather code after", self.c.get_weather)
            if not self.weather_acknowledged and held is not None and held == self.weather_before:
                # Nothing it took, and it still reads the code it held: nothing to put back.
                self.changes = [c for c in self.changes if c.kind != "weather"]
                self._save_pending()
            return
        self.weather_acknowledged = True
        self.event("set-weather", code=code, acknowledged_after=round(self.clock.monotonic() - began, 2))
        await self.read("weather code after", self.c.get_weather)

    async def step_clock(self) -> None:
        await self.ensure_poster(CLOCK_1)
        routes: list[tuple[str, str | None]] = [("tunnel", None)]
        # Decoded, so that the address is text, which the shareable copy leaves out (the raw frame is in the frames).
        endpoint, _ = await self.read("HTTP endpoint", self.c.get_http_endpoint)
        topology, _ = await self.read("network topology", lambda: self._u8(Op.GET_NET_TOPOLOGY))
        if endpoint is not None:
            reason = lspx_cli.http_refusal(endpoint, topology)
            self.event("wifi", port=endpoint.port, address=endpoint.address, usable=reason is None, refused=reason)
            if reason is None and endpoint.host:
                routes.append(("wifi", endpoint.host))
                self.note(f"The projector reports a Wi-Fi address the integration would use ({endpoint.host}).")
            else:
                self.note(f"Over Wi-Fi the integration would send nothing: {reason}.")
        self.say("Look at Clock 1 while the session asks the projector's clock.")
        for route, host in routes:
            if self.has_change("clock"):
                self.note("The clock is not back as it was, so the other route is not tried.")
                break
            await self.clock_route(route, host)

    async def _u8(self, op: Op) -> int:
        return decode_u8(await self.c.command(op))

    async def clock_route(self, route: str, host: str | None) -> None:
        name = "the Bluetooth tunnel (never tried before)" if route == "tunnel" else f"Wi-Fi ({host})"
        self.note(f"Asking the time over {name}.")
        reading = await self.get_clock(route, host)
        if reading is None:
            return
        offset = reading - web_api.local_unix_seconds(datetime.now().astimezone())
        shown = datetime.fromtimestamp(reading, UTC).strftime("%H:%M")
        self.note(f"Over {route} the projector's clock reads {shown}, {offset / 60:+.0f} min from this computer's.")
        await self.ask(f"Does Clock 1 show {shown}?", {"y": "yes", "n": "no (say what it shows)"})
        answer = await self.ask(
            f"Set it 90 minutes ahead over {route}, then back as it was?", {"": "yes", "n": "no"}, default=""
        )
        if answer.key == "n":
            return
        self.change("clock", {"route": route, "offset": offset, "host": host}, f"clock over {route}: back as it was")
        try:
            await self.set_clock(
                route, web_api.local_unix_seconds(datetime.now().astimezone()) + offset + 90 * 60, host
            )
        except READ_ERRORS as err:
            # Setting the clock this way was never tried: a refusal is an answer. Unchanged, nothing to put back.
            self.event("clock-set-refused", route=route, error=describe_error(err))
            self.note(f"Setting the clock over {route} did not work: {describe_error(err)}.")
            reading = await self.get_clock(route, host)
            expected = web_api.local_unix_seconds(datetime.now().astimezone()) + offset
            if reading is not None and abs(reading - expected) <= 60:
                self.changes = [c for c in self.changes if c.kind != "clock"]
                self._save_pending()
                self.note("It reads as before, so there is nothing to put back.")
            return
        await self.ask("Did Clock 1 jump 90 minutes ahead?", {"y": "yes", "n": "no", "o": "something else"})
        await self.get_clock(route, host)
        await self.restore(scope="step")
        expected = web_api.local_unix_seconds(datetime.now().astimezone()) + offset
        reading = await self.get_clock(route, host)
        answer = await self.ask("Is Clock 1 back to the time it showed before?", {"y": "yes", "n": "no"})
        off = None if reading is None else reading - expected
        self.event("clock-after", expected=expected, read=reading, off_seconds=off)
        if answer.key == "n" or off is None or abs(off) > 60:
            self.note(
                f"The clock may not be back as it was (read {'nothing' if off is None else f'{off:+d} s off'})."
                f" By hand: {manual_restore('clock', None)}."
            )

    async def get_clock(self, route: str, host: str | None) -> int | None:
        if route == "wifi":
            assert host is not None
            async with self.http_session() as session:
                reply, _ = await self.read("clock over Wi-Fi", lambda: web_api.get_time(session, host))
            return reply.time if reply is not None else None
        response, _ = await self.read_tunnel(web_tunnel.Method.GET, None)
        if response is None:
            return None
        try:
            result = json.loads(response.text()).get("result") or {}
            return int(result["time"])
        except (ValueError, AttributeError, KeyError, TypeError):
            self.note("The tunnel answered, but not with a time.")
            return None

    async def set_clock(self, route: str, seconds: int, host: str | None) -> None:
        if route == "wifi":
            assert host is not None
            async with self.http_session() as session:
                reply = await web_api.set_time(session, host, seconds)
            self.event("clock-set", route=route, sent=seconds, reply=reply)
            return
        # The app's own body through the tunnel (its setTime posts the time alone, docs/protocol.md).
        response = await self.tunnel(web_tunnel.Method.POST, json.dumps({"time": seconds}, separators=(",", ":")))
        self.event("clock-set", route=route, sent=seconds, http_status=response.http_status, body=response.text())
        if response.http_status not in (0, 200):
            raise web_tunnel.TunnelError(f"HTTP {response.http_status}")

    async def read_tunnel(self, method: web_tunnel.Method, body: str | None) -> tuple[Any, str | None]:
        try:
            response = await self.tunnel(method, body)
        except (web_tunnel.TunnelError, BleakError, TimeoutError, CommandRejected) as err:
            self.event("tunnel", method=method.name, error=describe_error(err))
            self.note(f"The tunnel did not answer as written up: {describe_error(err)} (the frames are recorded).")
            return None, describe_error(err)
        self.event(
            "tunnel",
            method=method.name,
            http_status=response.http_status,
            body=response.text(),
            retries=response.retries,
        )
        return response, None

    async def tunnel(self, method: web_tunnel.Method, body: str | None) -> web_tunnel.Response:
        if body is not None and not tunnel_param_allowed(body.encode()):
            raise UnsafeCommand(f"refusing to post {body!r}")
        if self.tunnel_request is not None:
            return await self.tunnel_request(method, body)
        async with self.c.exclusive() as client:
            service = client.services.get_service(web_tunnel.SERVICE)
            if service is None:
                raise web_tunnel.TunnelError(f"no service {web_tunnel.SERVICE}")
            cp_response = all(
                "write-without-response" not in char.properties
                for char in service.characteristics
                if char.uuid.lower() == TUNNEL_CP
            )
            link = web_tunnel.BleakLink(
                client, lambda line: self.event("tunnel-step", text=line), cp_response=cp_response
            )
            try:
                async with asyncio.timeout(web_tunnel.REQUEST_TIMEOUT):
                    response = await web_tunnel.request(
                        link,
                        method,
                        web_api.TIME_PATH,
                        body,
                        seq=self.tunnel_seq,
                        log=lambda line: self.event("tunnel-step", text=line),
                    )
            finally:
                with contextlib.suppress(BleakError, TimeoutError):
                    await client.stop_notify(TUNNEL_CP)
        self.tunnel_seq = response.next_seq
        return response

    async def step_direction(self) -> None:
        await self.ensure_poster(CLOCK_1)
        direction = await self._u8(Op.GET_DIRECTION)
        auto = decode_bool(await self.c.command(Op.GET_AUTO_DIRECTION))
        self.event("direction-before", direction=direction, auto=auto)
        self.change("auto_direction", auto, f"auto picture direction: back {'on' if auto else 'off'}")
        self.change("direction", direction, f"picture direction: back to {direction}")
        if auto:
            await self.c.set_auto_direction(False)
            await self.ask(
                "Auto picture direction is now off. Did the picture change?", {"n": "no", "y": "yes (say how)"}
            )
        self.say(
            f"Look at Clock 1's digits. The picture direction was {direction} ({DIRECTION_NAMES.get(direction, '?')})"
            "; each setting below is compared with how the picture looked then."
        )
        for value in (Direction.ROTATE_180, Direction.MIRROR_HORIZONTAL, Direction.MIRROR_VERTICAL, Direction.NORMAL):
            if value == direction:
                continue
            read_back = await self.c.set_direction(int(value), disable_auto=False)
            self.event("direction-set", value=int(value), read_back=read_back)
            await self.ask(
                f'Direction {int(value)} (the app\'s "{DIRECTION_NAMES[value]}"): compared with before, the picture is',
                {
                    "u": "upside down (turned 180°)",
                    "h": "mirrored left to right",
                    "v": "mirrored top to bottom",
                    "n": "unchanged",
                    "o": "something else",
                },
            )

    async def step_notifications(self) -> None:
        await self.ensure_on()
        # You switch it to standby and on again: if that is cut short, the session switches it on.
        self.change("power", True, "power: back on")
        notifications = lambda: sum(e["kind"] == "notification" for e in self.results.step("notifications")["events"])  # noqa: E731
        self.say(
            "The session now stays connected and sends nothing, and prints any notification the projector sends."
            " You press the projector's power button twice: lightly (a long press switches it fully off, and the"
            " session could not reach it again)."
        )
        pressed = {"": "pressed", "n": "nothing happened"}
        # Counted apart from the notifications the client recognises (opcode 0x8000 and up), which are only how the
        # app's code suggests they look: anything the projector sends unasked is in the frames.
        # Whether the link stays up through standby is not known: if it drops, nothing was listening meanwhile.
        if not self.c.is_connected:
            await self.c.command(Op.GET_POWER)  # listen on a link that is up
        # Replies can come twice (docs/protocol.md): let a copy of the last one pass before counting what comes.
        await self.clock.sleep(1.0)
        lost_before = self.c.stats.links_lost
        self.listening, self.unasked_frames = True, 0
        try:
            await self.ask(
                "Press the power button lightly once (to standby), then press Enter here.", pressed, default=""
            )
            await self.countdown(20, "Wait about 20 s in standby")
            await self.ask("Press the power button again (to switch on), then press Enter here.", pressed, default="")
            await self.countdown(120, "Listening while it starts up")
        finally:
            self.listening = False
        lost = self.c.stats.links_lost - lost_before
        self.event("unasked-frames", count=self.unasked_frames, links_lost=lost, last_lost=self.c.stats.last_link_lost)
        await self.read("power", self._power)
        await self.read("app state", lambda: self._u8(Op.GET_APP_STATE))
        await self.ask("Did it go to standby and come back on?", {"y": "yes", "n": "no (say what happened)"})
        self.note(
            f"While nothing was asked, {self.unasked_frames} frame(s) came on the command characteristic;"
            f" {notifications()} of them read as change notifications (all are in frames.jsonl)."
        )
        if lost:
            self.note(
                f"But the link dropped {lost} time(s) while listening (last: {self.c.stats.last_link_lost}), so"
                " nothing was listening for part of it: this does not show that the projector sends none."
            )

    async def _power(self) -> bool:
        return decode_power(await self.c.command(Op.GET_POWER))

    async def step_hdmi(self) -> None:
        answer = await self.ask(
            "Is an HDMI source at hand to plug into the wireless HDMI unit (a laptop, or a phone with an adapter)?",
            {"y": "yes", "n": "no"},
        )
        if answer.key == "n":
            raise SkipStep("no HDMI source")
        await self.ensure_source({"app_state": AppState.HDMI}, "HDMI")
        self.say(
            "The session reads the HDMI status every second and prints it when it changes. Plug the source in and"
            " out a few times, leaving each state about 20 s, and after each change type what you did or saw."
        )
        watcher = asyncio.get_running_loop().create_task(self.watch_hdmi())
        began = self.clock.monotonic()
        try:
            while self.clock.monotonic() - began < HDMI_WATCH_LIMIT:
                answer = await self.ask(
                    "What happened?",
                    {"p": "plugged in", "u": "unplugged", "v": "picture appeared", "g": "picture went", "d": "done"},
                )
                if answer.key == "d":
                    break
        finally:
            watcher.cancel()
            await asyncio.wait([watcher])  # not awaited directly: a Ctrl-C meanwhile must reach this task

    async def watch_hdmi(self) -> None:
        last = None
        while True:
            try:
                raw = await self.c.command(Op.GET_HDMI_STATUS)
                app = await self._u8(Op.GET_APP_STATE)
                enabled, status = decode_hdmi_status(raw)
                name = HdmiStatus(status).name if status in HdmiStatus._value2member_map_ else str(status)
                text = f"{raw.hex()} enabled={int(enabled)} status={name} app={app}"
            except READ_ERRORS as err:
                text = f"error: {describe_error(err)}"
            if text != last:
                last = text
                self.say(f"  [{self.now():7.1f} s] HDMI status {text}")
                self.event("hdmi-status", value=text)
            await self.clock.sleep(1)


def restorable(source: dict[str, Any]) -> bool:
    """Whether the session can choose ``source`` again (HDMI or a poster)."""
    app = source.get("app_state")
    return app == AppState.HDMI or (app == AppState.POSTER and bool(source.get("poster")))


def describe_source(source: dict[str, Any]) -> str:
    app = source.get("app_state")
    if app == AppState.POSTER:
        poster = source.get("poster")
        return (
            f"the {POSTER_NAMES.get(poster, f'theme {poster}')} poster"
            if poster in POSTER_NAMES
            else f"poster {poster}"
        )
    if app in AppState._value2member_map_:
        return AppState(app).name.lower()
    return f"app state {app}"


def intro(steps: Sequence[str]) -> str:
    plan = "\n".join(
        f"  {i}. {s.title}, ~{s.minutes} min" for i, s in enumerate((s for s in STEPS if s.key in steps), 1)
    )
    return (
        "At the projector: checks that need someone looking at it.\n\n"
        f"{FREE_THE_LINK}\n\nThe steps:\n{plan}\n\n"
        "Each step puts back what it changed; Ctrl-C does too (a second Ctrl-C quits at once, then run with"
        " --restore-only). The session never changes the volume, and only reads Smartphones added. Answers: type"
        " the letter, optionally a note after it, then Enter."
    )


def list_steps() -> str:
    out = []
    for i, step in enumerate(STEPS, 1):
        issues = f" (#{', #'.join(map(str, step.issues))})" if step.issues else ""
        out.append(f"{i}. {step.key}: {step.title}{issues}, ~{step.minutes} min")
        out.append(f"   {step.settles}")
    return "\n".join(out)


def parse_steps(text: str) -> list[str]:
    keys = [k.strip() for k in text.split(",") if k.strip()]
    unknown = [k for k in keys if k not in STEP_KEYS]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown step {', '.join(unknown)}; the steps: {', '.join(STEP_KEYS)}")
    return [k for k in STEP_KEYS if k in keys]  # always in the session's order


async def main(
    argv: Sequence[str] | None = None,
    *,
    console: Console | None = None,
    find_device: FindDevice = scan,
    http_session: HttpSession = aiohttp.ClientSession,
    clock: Clock | None = None,
    tunnel_request: TunnelRequest | None = None,
) -> Session | None:
    p = argparse.ArgumentParser(description="A guided session at the projector.")
    p.add_argument("--list", action="store_true", help="show the steps and exit")
    p.add_argument("--step", type=parse_steps, help="only these steps, comma-separated: " + ", ".join(STEP_KEYS))
    p.add_argument("--new", action="store_true", help="start a new session instead of continuing the last one")
    p.add_argument("--restore-only", action="store_true", help="put back what an earlier run left changed, and exit")
    p.add_argument("--results", type=Path, default=DEFAULT_RESULTS, help="where results go (default .at-projector/)")
    args = p.parse_args(argv)
    console = console or TerminalConsole()
    if args.list:
        console.say(list_steps())
        return None
    latest = Results.latest(args.results)
    if args.restore_only:
        if latest is None or not latest.data.get("pending_restores"):
            console.say("Nothing to put back.")
            return None
        session = Session(latest, console, find_device, http_session, clock or Clock(), steps=())
        await session.run()
        return session
    results = None
    if latest is not None and not args.new and not latest.data.get("finished"):
        results = latest
        console.say(f"Continuing the session in {latest.directory} (--new starts another).")
    if results is None:
        results = Results.new(args.results)
        if latest is not None:
            if latest.data.get("pending_restores"):
                # Put back first, before the new session reads what it starts from.
                results.data["pending_restores"] = latest.data["pending_restores"]
                results.save()  # first: an interrupt between the two saves must not lose them
                latest.data["pending_restores"] = []
                latest.save()
            results.save()
    steps = args.step or list(STEP_KEYS)
    session = Session(results, console, find_device, http_session, clock or Clock(), steps=steps)
    session.tunnel_request = tunnel_request
    await session.run()
    return session


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # A second Ctrl-C: asyncio stops at once. What was not put back yet is in the results, for --restore-only.
        print("\nStopped at once. Run tools/at_projector.py --restore-only to put back what is left (if anything).")
