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
    python tools/lspx_cli.py reboot [--watch 180]   # restart, then time how long until it answers again
    python tools/lspx_cli.py controllers   # "Smartphones added", marking this controller's own entry
    python tools/lspx_cli.py remove|block|allow <id>   # change one entry (never this controller's own)
    python tools/lspx_cli.py endpoint   # where the projector's HTTP API is on its Wi-Fi (GetHttpEndpoint)
    python tools/lspx_cli.py get-time [--host <ip>] [--port 80]   # the projector's clock, over Wi-Fi
    python tools/lspx_cli.py set-time [--shift <minutes>] [--host <ip>] [--port 80]   # set it (experimental)
    python tools/lspx_cli.py get-settings [--host <ip>] [--port 80]   # the settings over Wi-Fi, read only
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))

import aiohttp
from bleak import BleakScanner
from sony_lspx_p1 import web_api
from sony_lspx_p1.client import LSPXClient, LSPXError
from sony_lspx_p1.protocol import (
    LOCAL_NAME_PREFIX,
    POSTER_NAMES,
    STATE_READS,
    WEATHER_CODES,
    AppState,
    BondOp,
    HdmiStatus,
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


async def find():
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


async def web_host(client: LSPXClient | None, args: argparse.Namespace) -> str:
    """The projector's address on its Wi-Fi: --host, or GetHttpEndpoint over Bluetooth."""
    if args.host:
        return str(args.host)
    assert client is not None
    endpoint = await client.get_http_endpoint()
    print(f"endpoint: port {endpoint.port}, address {endpoint.address!r} (the app always uses port 80)")
    if endpoint.host is None:
        sys.exit("the projector reports no address on a Wi-Fi network")
    return endpoint.host


async def web_time(args: argparse.Namespace, client: LSPXClient | None) -> None:
    host = await web_host(client, args)
    url = web_api.base_url(host, args.port) + web_api.TIME_PATH
    async with aiohttp.ClientSession() as session:

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


async def web_settings(args: argparse.Namespace, client: LSPXClient | None) -> None:
    """Print the raw replies of the settings over Wi-Fi (GET only), then how the integration reads them."""
    host = await web_host(client, args)
    timeout = aiohttp.ClientTimeout(total=web_api.REQUEST_TIMEOUT)
    async with aiohttp.ClientSession() as session:
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


def parse_value(raw: str):
    if raw.lower() in ("on", "true", "yes"):
        return True
    if raw.lower() in ("off", "false", "no"):
        return False
    return int(raw)


async def main() -> None:
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
    sp.add_argument("value")
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
    po.add_argument("theme")
    po.add_argument("--watch", type=float, default=20, help="seconds to poll what the projector shows")
    wh = sub.add_parser("watch-hdmi", help="read the HDMI status every second and print it when it changes")
    wh.add_argument("--seconds", type=float, default=120)
    hd = sub.add_parser("hdmi", help="switch to the wireless HDMI unit")
    hd.add_argument("--watch", type=float, default=20, help="seconds to poll what the projector shows")
    we = sub.add_parser(
        "weather",
        help="read the Weather poster's code, or send one (1-11, as the app numbers its pictures; not yet tried)",
    )
    we.add_argument("code", nargs="?", type=int)
    rb = sub.add_parser("reboot", help="restart the projector (not yet tried on a real one)")
    rb.add_argument("--watch", type=float, default=180, help="seconds to wait for the projector to answer again")
    sub.add_parser("endpoint", help="where the projector's HTTP API is on its Wi-Fi network (GetHttpEndpoint)")
    for name, help_text in (
        ("get-time", "read the projector's clock over Wi-Fi (GET /v1/settings/time)"),
        ("set-time", "set the projector's clock over Wi-Fi to this computer's local time (experimental)"),
    ):
        tp = sub.add_parser(name, help=help_text)
        tp.add_argument("--host", help="the projector's IP address; read over Bluetooth if not given")
        tp.add_argument("--port", type=int, default=web_api.HTTP_PORT)
        if name == "set-time":
            tp.add_argument("--shift", type=float, default=0, help="minutes to add, to see the clock jump")
    gs = sub.add_parser(
        "get-settings", help="read the settings over Wi-Fi (timers, initial mode, slideshow interval), read only"
    )
    gs.add_argument("--host", help="the projector's IP address; read over Bluetooth if not given")
    gs.add_argument("--port", type=int, default=web_api.HTTP_PORT)
    args = p.parse_args()

    if args.cmd in ("get-time", "set-time") and args.host:
        await web_time(args, None)  # no Bluetooth needed
        return
    if args.cmd == "get-settings" and args.host:
        await web_settings(args, None)
        return
    dev = await find()
    if args.cmd == "scan":
        print(dev.address, dev.name)
        return
    client = LSPXClient(dev, idle_disconnect=False)
    try:
        if args.cmd == "pair":
            await client.pair(args.name)
            print("paired; controllers:", await client.bonded_controllers())
        elif args.cmd == "state":
            t = time.monotonic()
            state = await client.read_state()
            print(json.dumps(dataclasses.asdict(state), indent=1, default=str))
            print(f"read in {time.monotonic() - t:.1f}s")
        elif args.cmd == "set":
            value = parse_value(args.value)
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
                print(f"{c.id:5d}  {'allowed' if c.allowed else 'blocked'}  {c.name}{mine}")
        elif args.cmd in CONTROLLER_COMMANDS:
            op = {
                "remove": BondOp.DELETE_CONTROLLER,
                "block": BondOp.BLOCK_CONTROLLER,
                "allow": BondOp.ALLOW_CONTROLLER,
            }
            for c in await client.manage_controller(op[args.cmd], args.id):
                print(f"{c.id:5d}  {'allowed' if c.allowed else 'blocked'}  {c.name}")
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
        elif args.cmd in ("poster", "hdmi"):
            t0 = time.monotonic()
            if args.cmd == "hdmi":
                await client.show_hdmi()
            else:
                by_name = {name.lower(): int(poster) for poster, name in POSTER_NAMES.items()}
                theme = by_name.get(args.theme.lower()) or int(args.theme)
                version = (await client.read_state()).software_version
                await client.show_poster(theme, software_version=version)
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
            print(
                f"network topology {topology}" + (f" ({NetTopology(topology).name})" if topology in NetTopology else "")
            )
        elif args.cmd in ("get-time", "set-time"):
            await web_time(args, client)
        elif args.cmd == "get-settings":
            await web_settings(args, client)
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
                    except LSPXError as err:
                        extra = f"volume: {err}"
                    print(f"{time.monotonic() - t0:5.1f}s  power={'on' if on else 'standby'}  {extra}")
                except Exception as err:
                    print(f"{time.monotonic() - t0:5.1f}s  power read: {type(err).__name__}: {err}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
