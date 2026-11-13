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

The integration uses `0x13`/`0x14` and reads `BondedDeviceList`; `0x01`–`0x03` and `0x06` back the remove/block/allow smartphone actions and are not yet tried on a projector; `0x07` is untested.

### Removing, blocking and allowing a controller

From the app (`BlePairingIfs.deleteController`/`setEnableController`/`getMyId`, the settings screens in `z/e.java` and `z/d.java`); not yet tried on a projector.

- **Bytes:** `01 [id:u16]` delete, `02 [id:u16]` allow, `03 [id:u16]` block, where `id` is big-endian, as in `BondedDeviceList` (`CharacteristicInfoCache` gives all three the u16 value type; `CharacteristicConverter.convertIntToUint16`). For id `0xB31D`, delete is `01 B3 1D`. `06` has no argument and answers `[1][06][id:u16]`. The app reads any reply data of `0x01`–`0x03` as a u16 too, but does not use it.
- **The app's own entry:** the Smartphones added list (`z/e.java`) reads `0x06` and the list each time it opens, and greys out the entry whose id is its own so it cannot be opened. The entry's screen (`z/d.java`) offers Allow / Deny (`0x02`/`0x03`) and Delete smartphone; Delete reads `0x06` again and sends `0x01` only if the id differs. Only when setup is abandoned does the app delete its own entry.
- **Afterwards:** the app goes back to the list, which reads `0x06` and `BondedDeviceList` again. A failed delete is shown as "This smartphone has already been deleted."
- **Ids** look random (`0x6531`, `0xB31D` seen); `0x06` answering `0` is treated by the app as "not registered".

The integration does the same: it reads `0x06` and the list first, refuses to remove or block its own entry (and anything at all if its own id is not in the list), sends the operation, and reads the list back.

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
| `0x0031` | `0x0030` | Network topology | u8: `16` home, `17` home + poster, `18` home + content viewer, `32`–`34` the same as guest, `48` wireless HDMI |
| | `0x0033` | Stop the current app | no payload |
| `0x0067` | `0x0066` | Poster | u16 theme, see below |
| `0x006A` | `0x0069` | Weather poster's weather | u16, presumably a poster code 1–11; not yet tried, see below |
| `0x0004` | | Main board firmware | NUL-padded ASCII |
| `0x0831` | | System controller firmware | NUL-padded ASCII |
| `0x0841` | | Light engine firmware ("LD" in the app) | NUL-padded ASCII |
| `0x0005` | | Wireless HDMI unit firmware | NUL-padded ASCII |
| `0x0070` | | Wireless HDMI status | `[enabled][status]`: status `0` disconnected, `1` source unplugged, `2` no signal, `3` connecting, `4` connected, `5` changing resolution, `6` changing source, `16` updating |
| `0x0844` | | Usage time | `[hours:u16][minutes][seconds]` the projector has been on |
| `0x0811` | `0x0810` | Auto on/standby | `[phone nearby][inactivity][timer]`, each a bit set: `1` switch to standby, `2` switch on. Set all three bytes. |
| `0x0064` | | HTTP endpoint | `[port:u16][IPv4 ASCII]` |
| `0x0065` | | Wi-Fi station status | `[event flags:u16][SSID…]` |
| | `0x0F00` | Reboot | no payload; from the app, not yet confirmed on a projector (see below) |

When an "auto" setting is on, the projector keeps overriding the matching manual value; switch the auto setting off before setting the value.

### Poster and HDMI

From the official app's Bluetooth-only code path; confirmed on a projector with firmware 3.1.15.

- **HDMI:** set the network topology to `48`.
- **Poster:** if the current app is not the poster (`16`), stop it (`0x0033`) and wait about 3 seconds; then set the poster. If the topology was `48`, set it back to `17` (firmware 3.0.0 and later) or `16`.

The projector may answer these commands, and reads sent while a switch is still settling, only once it is done: often within a second, sometimes after more than 8 seconds (seen going to and from HDMI, and between posters). The app waits up to 60 seconds; so does the integration for these steps.

Poster themes, with the app's names (its "Change poster" screen):

| Theme | App name | Theme | App name |
| --- | --- | --- | --- |
| `11` | Weather | `3` | A day |
| `10` | Clock 1 (digital, factory default) | `4` | Journey |
| `9` | Clock 2 (digital) | `5` | Night dreams |
| `7` | Clock 3 (analog) | `6` | Window |
| `8` | Clock 4 (analog) | `13`/`14` | My slideshow 1/2 (photos sent from the app) |

Clocks and weather depend on data the app sends over Wi-Fi (the time, the weather code), so without the app they show whatever the projector last received.

### Weather

The app's Bluetooth SDK has SetWeather (`0x0069`) and GetWeather (`0x006A`), the `SocWeather` characteristic, but the app never calls them; not yet tried on a projector.

- **Value:** u16 in both directions (`CharacteristicInfoCache` type `18`, no range given). `BlePosterIfs.setWeather(int)` passes the number through unchanged, and the app's message handler only hands it on (`POSTER_WEATHER_SET`, nothing sends it). So the SDK says nothing about what the number means; the only weather number the app gives the projector anywhere is its poster code below, so that is what the integration sends. For code 7: `00 69 00 07`.
- **What the app does instead:** at most once an hour it fetches the current conditions from AccuWeather, turns the icon number into a poster code (`poster/weather/c.convertWeatherCodeToPosterCode`) and, through the HTTP poster API, makes the poster content tagged `weather_00NN` (or `weather00NN`) the Weather poster group's contents (`PosterCoordinator.applyWeather`). If the Weather poster is showing, it switches to the Blank theme for 300 ms and back, so that the poster is redrawn.

| Code | AccuWeather icons | Picture | Code | AccuWeather icons | Picture |
| --- | --- | --- | --- | --- | --- |
| 1 | 1, 2, 33, anything else | sunny / clear | 7 | 12, 13, 14 | showers |
| 2 | 3, 4, 34 | partly sunny | 8 | 18, 24, 25, 26, 29, 39, 40 | rain, ice, sleet, rain and snow |
| 3 | 5, 35, 36 | hazy | 9 | 19, 20, 21, 43 | flurries |
| 4 | 6, 30 | mostly cloudy, hot | 10 | 15, 16, 17, 41, 42 | thunderstorms |
| 5 | 7, 31, 38 | cloudy, cold | 11 | 22, 23, 44 | snow |
| 6 | 8, 11, 32, 37 | dreary, fog, windy | | | |

The integration (only when a weather entity is chosen in its options) maps Home Assistant's conditions: `sunny`, `clear-night` → 1; `partlycloudy` → 2; `cloudy` → 5; `fog`, `windy`, `windy-variant` → 6; `rainy` → 7; `pouring`, `hail`, `snowy-rainy` → 8; `lightning`, `lightning-rainy` → 10; `snowy` → 11; anything else (`exceptional`, unknown) sends nothing. It sends the code only while the Weather poster is shown (what SetWeather does while HDMI or another poster shows is unknown; it might switch to the poster): when the poster appears, which always sends the code again, and when the condition changes, waiting for a reply as long as for a poster switch (60 s), in case the projector answers only once the poster is redrawn. Diagnostics read GetWeather; polls do not.
If SetWeather turns out not to change the picture, the HTTP route is left ([#1](https://github.com/kachkaev/ha-sony-lspx-p1/issues/1)).

### Reboot

From the app, not yet confirmed on a projector.

- **Bytes:** `0F 00`, nothing else. `CharacteristicInfoCache` defines Reboot as `APP_COMMAND_NO_VAL`, so the value `1` that `BleDeviceIfs.reboot()` passes is dropped when the command is built, as for Stop the current app (`0x0033`).
- **Reply:** the app waits for the usual notification (up to 60 s) but, for Reboot, factory reset and initialise only, counts the link dropping before it as success (`BleControl.writeSync`): the projector may restart before it answers. A NAK is a failure.
- **Use in the app:** only right after entering or leaving demo mode, which is hidden on the About screen (tap a version number quickly more than ten times); nothing is read or waited for afterwards. The normal screens never send it.

The integration sends it the same way, also counts no reply within 8 seconds as success, and then disconnects and polls again a few times.

## Known quirks

- The projector's internal link between the Bluetooth chip and the main board can get stuck, after which every command times out (`0xC0`) and the official app stops working too. The recessed RESET button next to the DC IN jack recovers it.
- Right after switching power, reads fail for 1–3 seconds (`0x03`/`0xC0`); poll again a few seconds later.
- Many stacks keep a single connection to the projector; disconnect when idle so the smartphone app can still connect.
