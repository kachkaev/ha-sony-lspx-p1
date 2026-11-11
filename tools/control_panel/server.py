"""Mini control server for the Sony LSPX-P1 over Bluetooth LE.

Keeps one BLE connection open, sends one command at a time, and serves a small web UI.
Run from a terminal that has Bluetooth permission:  python server.py  ->  http://127.0.0.1:8765
The Mac must already be paired with the projector (see the pairing probe).
"""

import asyncio
import logging
import struct
import time
from pathlib import Path

from aiohttp import web
from bleak import BleakClient, BleakScanner

CP = "ee002916-5ab8-446b-2a0f-e1c55a6d871f"  # ApplicationCommandControlPoint
NAME_PREFIX = "LSPXP1"
HERE = Path(__file__).parent

log = logging.getLogger("lspx")


# Read commands: name -> (get opcode, decoder)
def u8(d):
    return d[0]


def s8(d):
    return struct.unpack(">b", d[:1])[0]


def s16(d):
    return struct.unpack(">h", d[:2])[0]


def flag(d):
    return bool(d[0])


READS = {
    "power": (2049, lambda d: d[0] == 0x10),
    "battery": (2081, lambda d: {"level": d[0], "charge_state": d[1] if len(d) > 1 else None}),
    "volume": (513, u8),
    "xloud": (529, flag),
    "brightness": (353, u8),
    "auto_brightness": (289, lambda d: {"by_ac": bool(d[0]), "by_illumination": bool(d[1])}),
    "focus": (321, lambda d: s16(d[1:3]) if len(d) > 2 else s16(d)),
    "focus_limits": (
        322,
        lambda d: {"near": s16(d[0:2]), "inch25": s16(d[2:4]), "origin": s16(d[4:6]), "far": s16(d[6:8])},
    ),
    "focus_assist": (257, flag),
    "keystone": (337, lambda d: s8(d[1:2]) if len(d) > 1 else 0),
    "direction": (369, u8),
    "auto_direction": (305, flag),
    "prj_range": (385, lambda d: {"brightness": [d[0], d[1]], "keystone": [s8(d[2:3]), s8(d[3:4])]}),
    "picture_mode": (1281, u8),
    "picture_range": (
        1297,
        lambda d: {
            "saturation": [d[0], d[1]],
            "brightness": [d[2], d[3]],
            "contrast": [d[4], d[5]],
            "sharpness": [d[6], d[7]],
            "rgb": [d[8], d[9]],
        },
    ),
    "picture_brightness": (1329, u8),
    "picture_contrast": (1345, u8),
    "picture_saturation": (1313, u8),
    "picture_sharpness": (1361, u8),
    "app_state": (50, u8),
    "soc_version": (4, lambda d: d.split(b"\x00")[0].decode(errors="replace")),
}

# Write commands: name -> payload builder (value -> bytes after opcode), set opcode
WRITES = {
    "power": (2048, lambda v: bytes([0x10 if v else 0x00])),
    "volume": (512, lambda v: bytes([int(v)])),
    "xloud": (528, lambda v: bytes([1 if v else 0])),
    "brightness": (352, lambda v: bytes([int(v)])),
    "auto_brightness": (288, lambda v: bytes([1 if v else 0, 1 if v else 0])),
    "focus": (320, lambda v: b"\x00" + struct.pack(">h", int(v))),
    "focus_assist": (256, lambda v: bytes([1 if v else 0])),
    "keystone": (336, lambda v: b"\x01" + struct.pack(">b", int(v))),
    "direction": (368, lambda v: bytes([int(v)])),
    "auto_direction": (304, lambda v: bytes([1 if v else 0])),
    "picture_mode": (1280, lambda v: bytes([int(v)])),
    "picture_brightness": (1328, lambda v: bytes([int(v)])),
    "picture_contrast": (1344, lambda v: bytes([int(v)])),
    "picture_saturation": (1312, lambda v: bytes([int(v)])),
    "picture_sharpness": (1360, lambda v: bytes([int(v)])),
}


class Projector:
    def __init__(self):
        self.client: BleakClient | None = None
        self.lock = asyncio.Lock()
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.history: list[dict] = []

    def _record(self, **entry):
        entry["t"] = time.strftime("%H:%M:%S")
        self.history.append(entry)
        del self.history[:-50]
        log.info("%s", entry)

    async def _ensure(self):
        if self.client and self.client.is_connected:
            return
        dev = await BleakScanner.find_device_by_filter(
            lambda d, a: (a.local_name or d.name or "").startswith(NAME_PREFIX), timeout=20
        )
        if not dev:
            raise RuntimeError("projector not found over Bluetooth")
        self.client = BleakClient(dev, timeout=30, disconnected_callback=lambda _: log.warning("disconnected"))
        await self.client.connect()
        await self.client.start_notify(CP, lambda _, d: self.queue.put_nowait(bytes(d)))
        self._record(event="connected", address=dev.address)

    async def command(self, op: int, payload: bytes = b"") -> bytes:
        async with self.lock:
            await self._ensure()
            while not self.queue.empty():
                self.queue.get_nowait()
            await self.client.write_gatt_char(CP, struct.pack(">H", op) + payload, response=True)
            r = await asyncio.wait_for(self.queue.get(), 8)
            result, echoed, data = r[0], struct.unpack(">H", r[1:3])[0], r[3:]
            if echoed != op:
                raise RuntimeError(f"reply for op {echoed}, expected {op}")
            if result != 2:
                raise RuntimeError(f"NAK op={op} error=0x{data[0] if data else 0:02x}")
            return data

    async def read(self, name: str):
        op, decode = READS[name]
        return decode(await self.command(op))

    async def write(self, name: str, value):
        op, build = WRITES[name]
        payload = build(value)
        try:
            await self.command(op, payload)
            self._record(set=name, value=value, ok=True)
        except Exception as e:
            self._record(set=name, value=value, ok=False, error=str(e))
            raise


projector = Projector()


async def state(_request):
    out, errors = {}, {}
    for name in READS:
        try:
            out[name] = await projector.read(name)
        except Exception as e:
            errors[name] = str(e)
    return web.json_response({"state": out, "errors": errors, "history": projector.history[-15:]})


async def set_value(request):
    body = await request.json()
    name, value = body["name"], body["value"]
    if name not in WRITES:
        return web.json_response({"error": f"unknown control {name}"}, status=400)
    try:
        await projector.write(name, value)
        current = await projector.read(name) if name in READS else None
        return web.json_response({"ok": True, "name": name, "value": current})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=502)


async def index(_request):
    return web.FileResponse(HERE / "index.html")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/api/state", state), web.post("/api/set", set_value)])
    web.run_app(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
