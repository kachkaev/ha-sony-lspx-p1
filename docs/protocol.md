# Sony LSPX-P1 Bluetooth LE protocol

Reverse-engineered from the official "Portable Ultra Short Throw Pro" Android app (`jp.co.sony.lspx001`, v3.2.11) and verified against a real projector (main board firmware 3.1.15, Bluetooth firmware 0.8.0).
Everything the app can adjust in its control panel is reachable over Bluetooth LE.
The projector also has an HTTP/JSON API on port 80 (`/v1/settings/...`) when it is on Wi-Fi; this integration does not use it.

## Discovery

The projector advertises as `LSPXP1_xxxx` (the last four hex digits of its Wi-Fi MAC address) with the 16-bit service UUID `0x00EE` and Sony manufacturer data (company ID `0x012D`) starting with `ee 00 5a b8 44 6b 2a 0f e1 c5 5a 6d 87 1f`.

## GATT layout

All custom UUIDs share the base `EE00xxxx-5AB8-446B-2A0F-E1C55A6D871F`.

| Service | Characteristic | Properties | Purpose |
| --- | --- | --- | --- |
| `EE0015BF` | `EE002916` ApplicationCommandControlPoint | write, notify | All projector commands (below) |
|  | `EE002FEB` / `EE00213C` | write/notify, read/write | Large payloads (Wi-Fi setup, posters) |
| `EE001B93` | `EE002834` ManageBondingControlPoint | write, notify | Bonding management |
|  | `EE002228` BondingDeviceName | read, write | Name to register the next controller under |
|  | `EE0023C8` BondedDeviceList | read | Registered controllers |
|  | `EE00272F` BleFirmwareRevisionString | read | e.g. `0.8.0` |
|  | `EE002193` | read, write | Requires an encrypted link; reading it triggers OS pairing |
| `EE001950` | `EE00270D` / `EE002C46` | write/notify, read/write | HTTP API pass-through |

## Pairing

Commands are rejected with error `0xA0` (access denied) unless the link is encrypted with a bond the projector knows.
To register a new controller (what the app does once per phone):

1. Subscribe to `EE002834` and write `10 54 30 68 72 6B 75` (operation `0x10` ActivateBondingMode + ASCII key `T0hrku`). The projector notifies `01 10` (success).
2. Write the controller's display name (UTF-8) to `EE002228`.
3. Perform OS-level Bluetooth pairing ("Just Works"). On macOS, reading `EE002193` triggers it; BlueZ and ESPHome proxies support an explicit pair call.

The controller then appears in the app under Settings → Network → Smartphones added (up to 8).
If "Restrict adding smartphones" is on, the projector shows a PIN on screen instead.

`BondedDeviceList` is a sequence of `[id:u16][flag:u8][name…]\n` records; flag `0` means allowed.

### Bonding operations

Other operations on `EE002834` (from the app's `ManageBondOperationList`), for a controller that is already bonded.
Write `[op:u8][args…]`; the reply is a notification `[result:u8][op:u8][data…]`, result `1` success, `2` failure, `4` invalid parameter.

| Op | Name | Arguments | Reply data |
| --- | --- | --- | --- |
| `0x01` | Delete controller | `[id:u16]` | |
| `0x02` / `0x03` | Allow / block controller | `[id:u16]` | |
| `0x06` | ID of this controller | | `[id:u16]` |
| `0x07` | Number of controllers | | `[n:u8]` |
| `0x13` | Get "Restrict adding smartphones" | | `[flag:u8]`, non-zero = on |
| `0x14` | Set "Restrict adding smartphones" | `[flag:u8]` | |

The integration uses `0x13`/`0x14` and reads `BondedDeviceList`; the others are documented for completeness and untested.

## Command framing

Write to `EE002916`: `[opcode:u16 big-endian][payload…]`.
The reply arrives as a notification on the same characteristic: `[result:u8][opcode:u16][data…]`.

- result `2` = ACK, data is the value (for a get) or the echoed payload (for a set);
- result `3` = NAK, `data[0]` is the error code.

| Error | Meaning |
| --- | --- |
| `0x01` | checksum |
| `0x02` | invalid argument |
| `0x03` | unknown command (also briefly while switching between standby and on) |
| `0xA0` | access denied: not bonded / link not encrypted |
| `0xC0` | timeout: the Bluetooth chip got no answer from the main board |
| `0xD0` | module error |

## Commands

Values are big-endian. "sint" means two's complement.

| Get | Set | Name | Value |
| --- | --- | --- | --- |
| `0x0801` | `0x0800` | Power | `0x10` on, `0x00` standby |
| `0x0821` | | Battery | `[level %][charge state: 0 on battery, 1 charging, 2 full]` |
| `0x0201` | `0x0200` | Volume | u8 0–100 |
| `0x0211` | `0x0210` | xLOUD | u8 0/1 |
| `0x0161` | `0x0160` | Brightness | u8, range from `0x0181` (40–100) |
| `0x0121` | `0x0120` | Auto brightness | `[by AC adapter][by illumination sensor]`, the app sets both |
| `0x0141` | `0x0140` | Focus | get: sint16; set: `00` + sint16 |
| `0x0142` | | Focus limits | `[near:s16][25-inch position:s16][origin:s16][far:s16]`, e.g. 149 … −383 |
| `0x0101` | `0x0100` | Focus assist (auto focus) | u8 0/1 |
| `0x0151` | `0x0150` | Keystone | `01` + sint8, range from `0x0181` (−30–30) |
| `0x0111` | `0x0110` | Auto keystone | not supported by firmware 3.1.15 (`0x03`) |
| `0x0171` | `0x0170` | Picture direction | `0` mirror horizontal, `1` rotate 180°, `2` normal, `3` mirror vertical |
| `0x0131` | `0x0130` | Auto picture direction | u8 0/1 |
| `0x0181` | | Projection ranges | `[bright min][bright max][keystone min:s8][keystone max:s8]` |
| `0x0501` | `0x0500` | Picture mode | `0`–`2` Preset 1–3, `16`/`17` Custom 1/2 |
| `0x0511` | | Picture ranges | min/max pairs: saturation, brightness, contrast, sharpness, RGB |
| `0x0521` | `0x0520` | Picture saturation | u8 (only effective in Custom modes) |
| `0x0531` | `0x0530` | Picture brightness | u8 |
| `0x0541` | `0x0540` | Picture contrast | u8 |
| `0x0551` | `0x0550` | Picture sharpness | u8 |
| `0x0032` | | Current app | `16` poster, `32` content viewer, `48` mirroring, `64` DLNA, `80` YouTube, `96` HDMI |
| `0x0004` | | Main board firmware | NUL-padded ASCII |
| `0x0064` | | HTTP endpoint | `[port:u16][IPv4 ASCII]` |
| `0x0065` | | Wi-Fi station status | `[event flags:u16][SSID…]` |

When an "auto" setting is on, the projector keeps overriding the matching manual value; switch the auto setting off before setting the value.

## Known quirks

- The projector's internal link between the Bluetooth chip and the main board can get stuck, after which every command times out (`0xC0`) and the official app stops working too. The recessed RESET button next to the DC IN jack recovers it.
- Right after switching power, reads fail for 1–3 seconds (`0x03`/`0xC0`); poll again a few seconds later.
- Many stacks keep a single connection to the projector; disconnect when idle so the smartphone app can still connect.
