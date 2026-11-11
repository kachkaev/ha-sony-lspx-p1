# Registers this Mac as a controller (Sony's documented app flow), then repeats the read-only GET probe.
import asyncio
import struct

from bleak import BleakClient, BleakScanner

CP = "ee002916-5ab8-446b-2a0f-e1c55a6d871f"  # ApplicationCommandControlPoint
BOND = "ee002834-5ab8-446b-2a0f-e1c55a6d871f"  # ManageBondingControlPoint
NAME = "ee002228-5ab8-446b-2a0f-e1c55a6d871f"  # BondingDeviceName
ENC = "ee002193-5ab8-446b-2a0f-e1c55a6d871f"  # needs encryption -> triggers OS pairing
LIST = "ee0023c8-5ab8-446b-2a0f-e1c55a6d871f"  # BondedDeviceList
GETS = [
    ("SocStatus", 3),
    ("SocSoftwareVersion", 4),
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
qcp, qbond = asyncio.Queue(), asyncio.Queue()


def show(b):
    t = b.decode("utf-8", "replace") if b and all(32 <= x < 127 or x == 0 for x in b) else ""
    return b.hex() + (f"  {t.strip(chr(0))!r}" if t.strip("\x00") else "")


async def gets(c):
    for name, op in GETS:
        while not qcp.empty():
            qcp.get_nowait()
        await c.write_gatt_char(CP, struct.pack(">H", op), response=True)
        try:
            r = await asyncio.wait_for(qcp.get(), 5)
            tag = {2: "ACK", 3: "NAK"}.get(r[0], r[0])
            print(f"  {name:20s} op={op:5d} -> {tag} data={show(r[3:])}")
        except TimeoutError:
            print(f"  {name:20s} op={op:5d} -> timeout")


async def main():
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or "").startswith("LSPXP1"), timeout=20
    )
    if not dev:
        print("LSPXP1 not found")
        return
    async with BleakClient(dev, timeout=30) as c:
        print("connected", dev.address)
        await c.start_notify(CP, lambda _, d: qcp.put_nowait(bytes(d)))
        await c.start_notify(BOND, lambda _, d: qbond.put_nowait(bytes(d)))
        await c.write_gatt_char(BOND, bytes([0x10]) + b"T0hrku", response=True)
        try:
            print("activate bonding ->", show(await asyncio.wait_for(qbond.get(), 5)))
        except TimeoutError:
            print("activate bonding -> no notify")
        await c.write_gatt_char(NAME, b"Home Assistant probe", response=True)
        print("name written")
        try:
            await c.pair()
        except Exception as e:
            print("pair():", type(e).__name__, e)
        for attempt in range(3):
            try:
                v = await c.read_gatt_char(ENC)
                print("encrypted read OK:", show(v))
                break
            except Exception as e:
                print(f"encrypted read attempt {attempt + 1}:", type(e).__name__, e)
                await asyncio.sleep(5)
        print("bonded list:", show(await c.read_gatt_char(LIST)))
        print("GET probe after pairing:")
        await gets(c)


asyncio.run(main())
