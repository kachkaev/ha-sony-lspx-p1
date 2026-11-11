# Read-only GATT dump of the LSPX-P1: connects, lists services/characteristics, reads readable ones. No writes.
import asyncio

from bleak import BleakClient, BleakScanner

NAMES = {
    "ee002c46": "WebAPIpassthroughParam",
    "ee00270d": "WebAPIpassthroughControlPoint",
    "ee0023c8": "BondedDeviceList",
    "ee002228": "BondingDeviceName",
    "ee00272f": "BleFirmwareRevisionString",
    "ee00202a": "TxPowerControl",
    "ee0025ea": "AdvertiseIntervalControl",
    "ee00216e": "AutoSleepProximityTimeMinute",
    "ee002916": "ApplicationCommandControlPoint",
    "ee002feb": "ApplicationCommandHugeControlPoint",
    "ee00213c": "ApplicationCommandHugeParam",
    "ee002834": "ManageBondingControlPoint",
}


async def main():
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or "").startswith("LSPXP1"), timeout=20
    )
    if not dev:
        print("LSPXP1 not found")
        return
    print("found", dev.address, dev.name)
    async with BleakClient(dev, timeout=30) as c:
        print("connected; mtu", c.mtu_size)
        for s in c.services:
            print(f"\nSERVICE {s.uuid} {s.description}")
            for ch in s.characteristics:
                nm = NAMES.get(ch.uuid[:8].lower(), ch.description)
                line = f"  CHAR {ch.uuid} [{','.join(ch.properties)}] {nm}"
                if "read" in ch.properties:
                    try:
                        v = await asyncio.wait_for(c.read_gatt_char(ch), 10)
                        txt = v.decode("utf-8", "replace") if v and all(32 <= b < 127 for b in v) else ""
                        line += f"\n      = {v.hex()} {txt!r}" if txt else f"\n      = {v.hex()}"
                    except Exception as e:
                        line += f"\n      ! read failed: {type(e).__name__}: {e}"
                print(line)
                for d in ch.descriptors:
                    print(f"      desc {d.uuid} {d.description}")


asyncio.run(main())
