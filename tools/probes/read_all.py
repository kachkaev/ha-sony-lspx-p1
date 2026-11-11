# Read-only probe: sends GET opcodes to ApplicationCommandControlPoint and decodes notifications. No SET commands.
import asyncio
import struct

from bleak import BleakClient, BleakScanner

CP = "ee002916-5ab8-446b-2a0f-e1c55a6d871f"
GETS = [
    ("SocWifiMacAddress", 1),
    ("SocStatus", 3),
    ("SocSoftwareVersion", 4),
    ("SocMode", 33),
    ("SocNetTopology", 49),
    ("SocAppState", 50),
    ("AppEndpointInfo", 100),
    ("StaStatus", 101),
    ("Power", 2049),
    ("BatteryLevel", 2081),
    ("AudioVolume", 513),
    ("PrjBrightness", 353),
    ("PrjAutoBrightness", 289),
    ("PrjFocus", 321),
    ("PrjFocusLimits", 322),
    ("PrjFocusAssist", 257),
    ("PrjKeystone", 337),
    ("PrjPictureDirection", 369),
    ("PrjAutoPicDirection", 305),
    ("PrjParamRange", 385),
    ("PictureMode", 1281),
    ("PictureBrightness", 1329),
    ("PictureContrast", 1345),
    ("PictureParamRange", 1297),
]
q = asyncio.Queue()


def on_notify(_, data: bytearray):
    q.put_nowait(bytes(data))


def show(b):
    t = b.decode("utf-8", "replace") if b and all(32 <= x < 127 or x == 0 for x in b) else ""
    return b.hex() + (f"  {t.strip(chr(0))!r}" if t.strip("\x00") else "")


async def main():
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or "").startswith("LSPXP1"), timeout=20
    )
    if not dev:
        print("LSPXP1 not found")
        return
    async with BleakClient(dev, timeout=30) as c:
        print("connected", dev.address)
        await c.start_notify(CP, on_notify)
        for name, op in GETS:
            while not q.empty():
                q.get_nowait()
            try:
                await c.write_gatt_char(CP, struct.pack(">H", op), response=True)
                r = await asyncio.wait_for(q.get(), 5)
                res, cmd = r[0], struct.unpack(">H", r[1:3])[0] if len(r) >= 3 else -1
                tag = {1: "CMD", 2: "ACK", 3: "NAK"}.get(res, res)
                print(f"{name:22s} op={op:5d} -> {tag} cmd={cmd} data={show(r[3:])}")
            except Exception as e:
                print(f"{name:22s} op={op:5d} !! {type(e).__name__}: {e}")


asyncio.run(main())
