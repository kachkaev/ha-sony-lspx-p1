"""Command-line access to the projector using the integration's own client.

Usage (needs Bluetooth permission for the terminal):
    python tools/lspx_cli.py scan
    python tools/lspx_cli.py pair [--name "My Mac"]
    python tools/lspx_cli.py state
    python tools/lspx_cli.py set volume 40
    python tools/lspx_cli.py power on|off [--watch 40]
    python tools/lspx_cli.py poster "Clock 1"|<theme id> [--watch 20]
    python tools/lspx_cli.py hdmi [--watch 20]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))

from bleak import BleakScanner
from sony_lspx_p1.client import LSPXClient, LSPXError
from sony_lspx_p1.protocol import LOCAL_NAME_PREFIX, POSTER_NAMES, STATE_READS, Op, decode_power

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
    po = sub.add_parser("poster", help="show a poster: an app name such as 'Clock 1', or a theme id")
    po.add_argument("theme")
    po.add_argument("--watch", type=float, default=20, help="seconds to poll what the projector shows")
    hd = sub.add_parser("hdmi", help="switch to the wireless HDMI unit")
    hd.add_argument("--watch", type=float, default=20, help="seconds to poll what the projector shows")
    args = p.parse_args()

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
            for c in await client.read_controllers():
                print(f"{c.id:5d}  {'allowed' if c.allowed else 'blocked'}  {c.name}")
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
