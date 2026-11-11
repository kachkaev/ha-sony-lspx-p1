import asyncio

from bleak import BleakScanner


async def main():
    found = await BleakScanner.discover(timeout=12, return_adv=True)
    rows = []
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        uu = [u for u in adv.service_uuids]
        sony = (
            any(u.lower().startswith("ee00") for u in uu) or "lspx" in name.lower() or 0x012D in adv.manufacturer_data
        )
        rows.append(
            (
                not sony,
                -(adv.rssi or -999),
                addr,
                name,
                adv.rssi,
                uu,
                {hex(k): v.hex() for k, v in adv.manufacturer_data.items()},
            )
        )
    rows.sort()
    print(f"{len(rows)} devices")
    for r in rows[:25]:
        print(("SONY? " if not r[0] else "      "), r[2], repr(r[3]), r[4], r[5], r[6])


asyncio.run(main())
