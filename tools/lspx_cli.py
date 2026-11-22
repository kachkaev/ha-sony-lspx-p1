"""Command-line access to the projector using the integration's own client.

Usage (needs Bluetooth permission for the terminal):
    python tools/lspx_cli.py scan
    python tools/lspx_cli.py pair [--name "My Mac"]
    python tools/lspx_cli.py state
    python tools/lspx_cli.py set volume 40
    python tools/lspx_cli.py power on|off [--watch 40]
    python tools/lspx_cli.py poster "Clock 1"|<theme id> [--watch 20]
    python tools/lspx_cli.py hdmi [--watch 20]
    python tools/lspx_cli.py weather [<code>]   # the Weather poster's code (GetWeather), or send one (SetWeather)
    python tools/lspx_cli.py watch-hdmi [--seconds 120]   # print the HDMI status whenever it changes
    python tools/lspx_cli.py listen [--seconds 120]   # stay connected and print the projector's notifications
    python tools/lspx_cli.py reboot [--watch 180]   # restart, then time how long until it answers again
    python tools/lspx_cli.py controllers   # "Smartphones added", marking this controller's own entry
    python tools/lspx_cli.py remove|block|allow <id>   # change one entry (never this controller's own)
    python tools/lspx_cli.py endpoint   # where the projector's HTTP API is on its Wi-Fi, and whether HTTP may go there
    python tools/lspx_cli.py get-time [--host <ip>] [--port 80]   # the projector's clock, over Wi-Fi
    python tools/lspx_cli.py set-time [--shift <minutes>] [--host <ip>] [--port 80]   # set it (experimental)
    python tools/lspx_cli.py get-settings [--host <ip>] [--port 80]   # the settings over Wi-Fi, read only
    python tools/lspx_cli.py probe [--out <file>]   # every read-only command in turn, into one transcript file

The commands over Wi-Fi send nothing while the projector's Wi-Fi is its own network, as the integration decides
(HttpEndpoint.home_host); add --allow-own-network to try anyway, from a computer on the projector's own network.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import sys
import time
import traceback
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))

import aiohttp
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from sony_lspx_p1 import web_api
from sony_lspx_p1.client import LSPXClient
from sony_lspx_p1.protocol import (
    DIRECT_NETWORK,
    LOCAL_NAME_PREFIX,
    OWN_NETWORK_TOPOLOGIES,
    POSTER_NAMES,
    STATE_READS,
    WEATHER_CODES,
    AppState,
    BondOp,
    Controller,
    HdmiStatus,
    HttpEndpoint,
    NetTopology,
    Op,
    decode_http_endpoint,
    decode_power,
    decode_u8,
)

CONTROLLER_COMMANDS = {
    "remove": 'remove an entry from "Smartphones added" (not yet tried on a real projector)',
    "block": "block an entry: it stays in the list but cannot control the projector (not yet tried)",
    "allow": "allow a blocked entry again (not yet tried)",
}

SETTERS = {
    "volume": "set_volume",
    "xloud": "set_xloud",
    "brightness": "set_brightness",
    "auto_brightness": "set_auto_brightness",
    "focus": "set_focus",
    "focus_assist": "set_focus_assist",
    "keystone": "set_keystone",
    "direction": "set_direction",
    "auto_direction": "set_auto_direction",
    "picture_mode": "set_picture_mode",
}


async def find() -> BLEDevice:
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or "").startswith(LOCAL_NAME_PREFIX), timeout=20
    )
    if dev is None:
        sys.exit("projector not found")
    return dev


def wall_clock(seconds: int | None) -> str:
    """A time as the projector holds it: wall-clock time as Unix seconds."""
    if seconds is None:
        return "(not in the reply)"
    return f"{datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)} ({seconds})"


def describe_topology(topology: int) -> str:
    return f"{topology}" + (f" ({NetTopology(topology).name})" if topology in NetTopology else "")


def http_refusal(endpoint: HttpEndpoint, topology: int | None) -> str | None:
    """Why the integration would send no HTTP request to this endpoint (HttpEndpoint.home_host), or None."""
    if endpoint.home_host(topology) is not None:
        return None
    if endpoint.host is None:
        return "the projector reports no address on a Wi-Fi network"
    if topology in OWN_NETWORK_TOPOLOGIES:
        assert topology is not None
        return f"the projector's Wi-Fi is its own network (topology {describe_topology(topology)}), not the home one"
    if endpoint.direct:
        return f"{endpoint.host} is on {DIRECT_NETWORK}, the projector's own network (and some home routers' address)"
    return f"the integration's rule (HttpEndpoint.home_host) refuses {endpoint.host}"


async def web_host(client: LSPXClient | None, args: argparse.Namespace) -> str:
    """The projector's address on its Wi-Fi: --host, or GetHttpEndpoint over Bluetooth.

    Exits unless the integration would send HTTP there too (http_refusal), or --allow-own-network says to anyway.
    """
    if args.host:
        host = str(args.host)
        # Only the address can be checked: the topology is not read without Bluetooth. A name is taken as it is.
        endpoint = HttpEndpoint(args.port, host)
        reason = http_refusal(endpoint, None) if endpoint.direct else None
    else:
        assert client is not None
        endpoint = await client.get_http_endpoint()
        print(f"endpoint: port {endpoint.port}, address {endpoint.address!r} (the app always uses port 80)")
        if endpoint.host is None:
            sys.exit(http_refusal(endpoint, None))
        host = endpoint.host
        try:
            topology: int | None = decode_u8(await client.command(Op.GET_NET_TOPOLOGY))
        except Exception as err:
            # As the integration without a topology from its poll: the address alone decides.
            print(f"network topology: {type(err).__name__}: {err}; checking the address only")
            topology = None
        reason = http_refusal(endpoint, topology)
    if reason is not None:
        if not args.allow_own_network:
            sys.exit(f"sending nothing over HTTP: {reason}; add --allow-own-network to try anyway")
        print(f"--allow-own-network: sending to {host} anyway, although {reason}")
    return host


# Makes the HTTP session for the commands over Wi-Fi; tests hand in one bound to a fake web API.
HttpSession = Callable[[], aiohttp.ClientSession]


async def web_time(args: argparse.Namespace, client: LSPXClient | None, http_session: HttpSession) -> None:
    host = await web_host(client, args)
    url = web_api.base_url(host, args.port) + web_api.TIME_PATH
    async with http_session() as session:

        async def show(label: str) -> None:
            try:
                reply = await web_api.get_time(session, host, port=args.port)
            except web_api.WebApiError as err:
                print(f"{label}: GET {url} failed: {err}")
                return
            print(f"{label}: {wall_clock(reply.time)}, timezone {reply.timezone!r}")

        await show("projector clock")
        if args.cmd == "get-time":
            return
        now = datetime.now().astimezone()
        local = web_api.local_unix_seconds(now) + round(args.shift * 60)
        print(f"local time here: {now.isoformat()}; sending {wall_clock(local)} (shifted by {args.shift} min)")
        t0 = time.monotonic()
        try:
            reply = await web_api.set_time(session, host, local, port=args.port)
        except web_api.WebApiError as err:
            sys.exit(f"POST {url} failed after {time.monotonic() - t0:.1f}s: {err}")
        print(
            f"POST {url}: accepted in {time.monotonic() - t0:.1f}s; reply {wall_clock(reply.time)}, {reply.timezone!r}"
        )
        await show("projector clock now")
        print("Check the clock poster (Clock 1-4) on the projector.")


# Read by get-settings: what the integration's settings over Wi-Fi use (web_api.py).
SETTINGS_PATHS = (web_api.TIMERS_PATH, web_api.POSTER_RESUME_PATH, web_api.POSTER_DURATION_PATH)


async def web_settings(args: argparse.Namespace, client: LSPXClient | None, http_session: HttpSession) -> None:
    """Print the raw replies of the settings over Wi-Fi (GET only), then how the integration reads them."""
    host = await web_host(client, args)
    timeout = aiohttp.ClientTimeout(total=web_api.REQUEST_TIMEOUT)
    async with http_session() as session:
        for path in SETTINGS_PATHS:
            url = web_api.base_url(host, args.port) + path
            try:
                async with session.get(url, headers=web_api.HEADERS, timeout=timeout) as response:
                    print(f"GET {url}: HTTP {response.status}\n  {await response.text()}")
            except (aiohttp.ClientError, TimeoutError) as err:
                print(f"GET {url} failed: {web_api.describe(err)}")
        try:
            timers = await web_api.get_timers(session, host, port=args.port)
        except web_api.WebApiError as err:
            print(f"timers: {err}")
        else:
            for timer in timers:
                print(f"  timer {timer.id}: {timer.tag} {timer.state} at {wall_clock(timer.time)}")
            for tag in (web_api.TIMER_ON, web_api.TIMER_OFF):
                at = web_api.timer_time(timers, tag)
                print(f"{tag}: {'off' if at is None else f'on, daily at {at:%H:%M}'}")


def controller_line(c: Controller) -> str:
    """One entry of "Smartphones added", as the controllers command prints it."""
    return f"{c.id:5d}  {'allowed' if c.allowed else 'blocked'}  {c.name}"


def parse_value(raw: str) -> bool | int:
    if raw.lower() in ("on", "true", "yes"):
        return True
    if raw.lower() in ("off", "false", "no"):
        return False
    try:
        return int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is neither a number nor on/off") from None


def parse_theme(raw: str) -> int:
    """A poster by the app's name for it (any case), or a theme id."""
    by_name = {name.lower(): int(poster) for poster, name in POSTER_NAMES.items()}
    if raw.lower() in by_name:
        return by_name[raw.lower()]
    try:
        return int(raw)
    except ValueError:
        names = ", ".join(repr(name) for name in POSTER_NAMES.values())
        raise argparse.ArgumentTypeError(f"{raw!r} is neither a theme id nor one of {names}") from None


HOST_HELP = (
    "the projector's IP address; read over Bluetooth if not given. Without Bluetooth only the address is checked:"
    " a 192.168.8.x one is refused, a host name is not checked"
)
ALLOW_OWN_NETWORK_HELP = (
    "send even while the projector's Wi-Fi is its own network (Wi-Fi Direct, wireless HDMI, or a 192.168.8.x"
    " address), which the integration never does: for a computer on that network; elsewhere 192.168.8.1 may be"
    " another device, such as the router"
)

# Run by probe, in this order. Each one only reads: nothing here changes what the projector does or holds.
PROBE_COMMANDS = (("state",), ("controllers",), ("weather",), ("endpoint",), ("get-time",), ("get-settings",))


class Tee:
    """Writes to several streams at once."""

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


async def probe(out: Path | None, find_device: Callable[[], Awaitable[BLEDevice]], http_session: HttpSession) -> None:
    """Run PROBE_COMMANDS one after another, printing each and writing everything to one transcript file."""
    started = datetime.now().astimezone()
    out = out or Path(f"lspx-probe-{started:%Y%m%d-%H%M%S}.txt")
    dev = await find_device()

    async def found() -> BLEDevice:
        return dev

    with out.open("w", encoding="utf-8") as file, contextlib.redirect_stdout(Tee(sys.stdout, file)):
        print(f"# lspx_cli.py probe of {dev.address} ({dev.name}), {started.isoformat(timespec='seconds')}")
        for argv in PROBE_COMMANDS:
            print(f"\n$ lspx_cli.py {' '.join(argv)}", flush=True)
            try:
                await main(list(argv), find_device=found, http_session=http_session)
            # One failing command must not stop the others.
            except SystemExit as err:
                print(f"failed: SystemExit: {err}", flush=True)
            except Exception as err:
                print(f"failed: {type(err).__name__}: {err}\n{traceback.format_exc()}", end="", flush=True)
    print(f"transcript written to {out}")


async def main(
    argv: Sequence[str] | None = None,
    *,
    find_device: Callable[[], Awaitable[BLEDevice]] = find,
    http_session: HttpSession = aiohttp.ClientSession,
) -> None:
    """Run one command; ``argv`` defaults to the command line. Tests hand in the device lookup and HTTP session."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    pp = sub.add_parser("pair")
    pp.add_argument("--name", default="Home Assistant (dev)")
    sub.add_parser("state")
    sp = sub.add_parser("set")
    sp.add_argument(
        "name",
        choices=[*sorted(SETTERS), "picture_brightness", "picture_contrast", "picture_saturation", "picture_sharpness"],
    )
    sp.add_argument("value", type=parse_value, help="a number, or on/off")
    pw = sub.add_parser("power")
    pw.add_argument("value", choices=["on", "off"])
    pw.add_argument("--watch", type=float, default=0, help="seconds to poll the power state afterwards")
    rp = sub.add_parser("restrict", help='"Restrict adding smartphones": show, or turn on/off')
    rp.add_argument("value", nargs="?", choices=["on", "off"])
    sub.add_parser("controllers", help='the "Smartphones added" list')
    for name, help_text in CONTROLLER_COMMANDS.items():
        cp = sub.add_parser(name, help=help_text)
        cp.add_argument("id", type=int, help="the entry's id, as the controllers command prints it")
    po = sub.add_parser("poster", help="show a poster: an app name such as 'Clock 1', or a theme id")
    po.add_argument("theme", type=parse_theme)
    po.add_argument("--watch", type=float, default=20, help="seconds to poll what the projector shows")
    wh = sub.add_parser("watch-hdmi", help="read the HDMI status every second and print it when it changes")
    wh.add_argument("--seconds", type=float, default=120)
    li = sub.add_parser(
        "listen", help="stay connected, sending nothing, and print every notification (press the power button now)"
    )
    li.add_argument("--seconds", type=float, default=120)
    hd = sub.add_parser("hdmi", help="switch to the wireless HDMI unit")
    hd.add_argument("--watch", type=float, default=20, help="seconds to poll what the projector shows")
    we = sub.add_parser(
        "weather",
        help="read the Weather poster's code, or send one (1-11, as the app numbers its pictures; not yet tried)",
    )
    we.add_argument("code", nargs="?", type=int)
    rb = sub.add_parser("reboot", help="restart the projector (not yet tried on a real one)")
    rb.add_argument("--watch", type=float, default=180, help="seconds to wait for the projector to answer again")
    sub.add_parser(
        "endpoint",
        help="where the projector's HTTP API is on its Wi-Fi network (GetHttpEndpoint), and whether HTTP may go there",
    )
    for name, help_text in (
        ("get-time", "read the projector's clock over Wi-Fi (GET /v1/settings/time)"),
        ("set-time", "set the projector's clock over Wi-Fi to this computer's local time (experimental)"),
    ):
        tp = sub.add_parser(name, help=help_text)
        tp.add_argument("--host", help=HOST_HELP)
        tp.add_argument("--port", type=int, default=web_api.HTTP_PORT)
        tp.add_argument("--allow-own-network", action="store_true", help=ALLOW_OWN_NETWORK_HELP)
        if name == "set-time":
            tp.add_argument("--shift", type=float, default=0, help="minutes to add, to see the clock jump")
    gs = sub.add_parser(
        "get-settings", help="read the settings over Wi-Fi (timers, initial mode, slideshow interval), read only"
    )
    gs.add_argument("--host", help=HOST_HELP)
    gs.add_argument("--port", type=int, default=web_api.HTTP_PORT)
    gs.add_argument("--allow-own-network", action="store_true", help=ALLOW_OWN_NETWORK_HELP)
    pr = sub.add_parser(
        "probe", help="run every read-only command in turn (" + ", ".join(c[0] for c in PROBE_COMMANDS) + ")"
    )
    pr.add_argument("--out", type=Path, help="the transcript file (default: lspx-probe-<date>-<time>.txt here)")
    args = p.parse_args(argv)

    if args.cmd == "probe":
        await probe(args.out, find_device, http_session)
        return

    if args.cmd in ("get-time", "set-time") and args.host:
        await web_time(args, None, http_session)  # no Bluetooth needed
        return
    if args.cmd == "get-settings" and args.host:
        await web_settings(args, None, http_session)
        return
    dev = await find_device()
    if args.cmd == "scan":
        print(dev.address, dev.name)
        return
    client = LSPXClient(dev, idle_disconnect=False)
    try:
        if args.cmd == "pair":
            await client.pair(args.name)
            print("paired; controllers:")
            for c in await client.read_controllers():
                print(controller_line(c))
        elif args.cmd == "state":
            t = time.monotonic()
            state = await client.read_state()
            print(json.dumps(dataclasses.asdict(state), indent=1, default=str))
            print(f"read in {time.monotonic() - t:.1f}s")
        elif args.cmd == "set":
            value = args.value
            if args.name.startswith("picture_") and args.name != "picture_mode":
                print("->", await client.set_picture_value(args.name, value))
            else:
                print("->", await getattr(client, SETTERS[args.name])(value))
        elif args.cmd == "restrict":
            if args.value:
                print("->", await client.set_restrict_new_controllers(args.value == "on"))
            else:
                print(await client.get_restrict_new_controllers())
        elif args.cmd == "controllers":
            try:
                my_id: int | None = await client.get_my_controller_id()
            except Exception as err:
                print(f"this controller's id: {type(err).__name__}: {err}")
                my_id = None
            for c in await client.read_controllers():
                mine = "  (this controller)" if c.id == my_id else ""
                print(controller_line(c) + mine)
        elif args.cmd in CONTROLLER_COMMANDS:
            op = {
                "remove": BondOp.DELETE_CONTROLLER,
                "block": BondOp.BLOCK_CONTROLLER,
                "allow": BondOp.ALLOW_CONTROLLER,
            }
            for c in await client.manage_controller(op[args.cmd], args.id):
                print(controller_line(c))
        elif args.cmd == "watch-hdmi":
            t0 = time.monotonic()
            last = None
            while time.monotonic() - t0 < args.seconds:
                try:
                    raw = (await client.command(Op.GET_HDMI_STATUS)).hex()
                    app = AppState(STATE_READS["app_state"][1](await client.command(Op.GET_APP_STATE))).name
                except Exception as err:
                    raw, app = f"error: {err}", ""
                if (raw, app) != last:
                    last = (raw, app)
                    decoded = ""
                    if len(raw) >= 4 and not raw.startswith("error"):
                        enabled, status = int(raw[:2], 16), int(raw[2:4], 16)
                        name = HdmiStatus(status).name if status in HdmiStatus._value2member_map_ else status
                        decoded = f"enabled={enabled} status={name}"
                    print(f"{time.monotonic() - t0:6.1f}s  {raw}  {decoded}  app={app}", flush=True)
                await asyncio.sleep(1)
        elif args.cmd == "listen":
            # The client logs each change notification as it arrives, and any other frame (one framed differently,
            # say) when the closing read drains it.
            logging.basicConfig(format="%(relativeCreated)8.0f ms  %(message)s")
            logging.getLogger("sony_lspx_p1.client").setLevel(logging.DEBUG)
            client.set_notification_callback(lambda n: print(f"  -> {n.describe()}", flush=True))
            print("power:", decode_power(await client.command(Op.GET_POWER)))
            print(f"listening for {args.seconds:.0f} s: press the power button (the app cannot connect)", flush=True)
            await asyncio.sleep(args.seconds)
            print("power:", decode_power(await client.command(Op.GET_POWER)))
        elif args.cmd in ("poster", "hdmi"):
            t0 = time.monotonic()
            if args.cmd == "hdmi":
                await client.show_hdmi()
            else:
                version = (await client.read_state()).software_version
                await client.show_poster(args.theme, software_version=version)
            print(f"{time.monotonic() - t0:5.1f}s  {args.cmd}: accepted")
            while time.monotonic() - t0 < args.watch:
                await asyncio.sleep(2)
                shown = {}
                for attr in ("app_state", "poster", "net_topology"):
                    op, decoder = STATE_READS[attr]
                    try:
                        shown[attr] = decoder(await client.command(op))
                    except Exception as err:
                        shown[attr] = f"error: {err}"
                print(f"{time.monotonic() - t0:5.1f}s  {shown}")
        elif args.cmd == "weather":
            # Show the Weather poster first (poster Weather) to see whether its picture follows.
            async def read_weather() -> str:
                try:
                    return str(await client.get_weather())
                except Exception as err:
                    return f"{type(err).__name__}: {err}"

            shown = {}
            for attr in ("power", "app_state", "poster"):
                op, decoder = STATE_READS[attr]
                try:
                    shown[attr] = decoder(await client.command(op))
                except Exception as err:
                    shown[attr] = f"error: {err}"
            print(f"showing {shown}; weather code before: {await read_weather()}")
            if args.code is not None:
                if args.code not in WEATHER_CODES:
                    print(f"note: {args.code} is not one of the app's codes (1-11), sending anyway")
                t0 = time.monotonic()
                await client.set_weather(args.code)
                print(f"{time.monotonic() - t0:5.1f}s  set weather {args.code}: ACK")
                print(f"weather code after: {await read_weather()}")
        elif args.cmd == "endpoint":
            raw = await client.command(Op.GET_HTTP_ENDPOINT)
            endpoint = decode_http_endpoint(raw)
            topology = decode_u8(await client.command(Op.GET_NET_TOPOLOGY))
            print(f"raw {raw.hex()}: port {endpoint.port}, address {endpoint.address!r}, usable host {endpoint.host}")
            print(f"network topology {describe_topology(topology)}")
            reason = http_refusal(endpoint, topology)
            print(
                f"HTTP: the integration would send to {endpoint.host}"
                if reason is None
                else f"HTTP: the integration would send nothing ({reason})"
            )
        elif args.cmd in ("get-time", "set-time"):
            await web_time(args, client, http_session)
        elif args.cmd == "get-settings":
            await web_settings(args, client, http_session)
        elif args.cmd == "reboot":
            t0 = time.monotonic()
            print(f"{0:5.1f}s  reboot: {await client.reboot()}")
            while time.monotonic() - t0 < args.watch:
                await asyncio.sleep(2)
                try:
                    on = decode_power(await client.command(Op.GET_POWER))
                except Exception as err:
                    print(f"{time.monotonic() - t0:5.1f}s  power read: {type(err).__name__}: {err}")
                    continue
                print(f"{time.monotonic() - t0:5.1f}s  answering again, power={'on' if on else 'standby'}")
                break
        elif args.cmd == "power":
            t0 = time.monotonic()
            await client.set_power(args.value == "on")
            print(f"{0:5.1f}s  set power {args.value}: ACK")
            while time.monotonic() - t0 < args.watch:
                await asyncio.sleep(1)
                try:
                    on = decode_power(await client.command(Op.GET_POWER))
                    try:
                        vol = (await client.command(Op.GET_VOLUME))[0]
                        extra = f"volume={vol}"
                    except Exception as err:
                        extra = f"volume: {type(err).__name__}: {err}"
                    print(f"{time.monotonic() - t0:5.1f}s  power={'on' if on else 'standby'}  {extra}")
                except Exception as err:
                    print(f"{time.monotonic() - t0:5.1f}s  power read: {type(err).__name__}: {err}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
