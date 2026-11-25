# Sony LSPX-P1 Bluetooth LE protocol

Reverse-engineered from the official "Portable Ultra Short Throw Pro" Android app (`jp.co.sony.lspx001`, v3.2.11) and verified against a real projector (main board firmware 3.1.15, Bluetooth firmware 0.8.0).
Everything the app can adjust in its control panel is reachable over Bluetooth LE.
The projector also has an HTTP/JSON API on port 80 (`/v1/settings/...`) when it is on Wi-Fi; this integration uses it to set the clock (through the [Bluetooth tunnel](#bluetooth-tunnel), over Wi-Fi only when that fails) and, for entities that are disabled by default, for the timers and two poster settings ([below](#http-api-over-wi-fi)).

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
| `EE001950` | `EE00270D` / `EE002C46` | write/notify, read/write | HTTP API pass-through ([tunnel](#bluetooth-tunnel)) |

## Pairing

Commands are rejected with error `0xA0` (access denied) unless the link is encrypted with a bond the projector knows.
To register a new controller (what the app does once per phone):

1. Subscribe to `EE002834` and write `10 54 30 68 72 6B 75` (operation `0x10` ActivateBondingMode + ASCII key `T0hrku`). The projector notifies `01 10` (success).
2. Write the controller's display name (UTF-8) to `EE002228`.
3. Perform OS-level Bluetooth pairing ("Just Works"). On macOS, reading `EE002193` triggers it; BlueZ and ESPHome proxies support an explicit pair call.

The controller then appears in the app under Settings → Network → Smartphones added (up to 8).
If "Restrict adding smartphones" is on, the projector shows a PIN on screen instead (from the app; not seen, see below).

Seen once from macOS (firmware 3.1.15, `tools/lspx_cli.py pair`, 2026-11-24):

- **Restriction on, projector in standby:** macOS asked for a passcode, and with no picture there was no PIN to read; the pairing failed after timeouts (the encrypted read timed out twice). Whether the projector shows a PIN while it is on was not tried.
- **Restriction off:** macOS showed a "Connection Request" window from the projector, which closes by itself after a few seconds if not clicked; a second run of `pair`, with a click on Connect, paired it. So on macOS this "Just Works" pairing still needs someone at the computer.
- **Sony's app open on the phone:** one attempt in between did not find the projector at all.
- **The list:** the new entry was added next to the two already there (three entries, all allowed).

`BondedDeviceList` is a sequence of `[id:u16][flag:u8][name…]\n` records; flag `0` means allowed.

### When the projector rejects a controller

From the app; not yet tried on a projector ([#41](https://github.com/kachkaev/ha-sony-lspx-p1/issues/41)).

- **Where `0xA0` comes from:** the app maps `0xA0` to access denied both as a NAK on the command characteristic and as the GATT status of a failed read or write (`BleReaderWriter`), so a characteristic may refuse an unencrypted link at the ATT level with that code too. Its connector also expects ID of this controller (`0x06`) to fail with access denied on a phone the projector does not accept (`BleConnector`).
- **What the app does then** (`e/e.java`): it reads Get "Restrict adding smartphones" (`0x13`) on that link. If the flag is on, it offers to run its setup again (where the PIN is entered). If it is off, or the read fails: when the phone still holds a bond, it shows "Device info incorrect" and asks the user to delete the phone's Bluetooth pairing and try again; when it does not, it pairs again by itself. It has nothing for a blocked ("Deny") entry.
- **Sony's help guide:** a deleted phone is added to the list again when it next connects, unless "Restrict adding smartphones" is on.
- **Open:** what a blocked controller, a deleted one and one forgotten by a reset are answered (`0xA0` to commands, an encryption failure or a dropped link); whether `0x06`, `0x13` and `BondedDeviceList` answer on such a link; whether pairing again replaces the old entry of the same host or adds one.

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

A reply has no sequence number: only its opcode tells which command it answers. Replies sometimes arrive **twice**. Seen through Home Assistant with firmware 3.1.15, on 2026-11-22, in three sessions:

- **Sessions 1 and 2** (about three hours, without a Home Assistant restart; which adapter or proxy carried the link is not in those logs): nearly every reply came twice, the copy always before the reply to the next command (often before that command was even sent), and each new connection began with the last frame of the connection before, sent again once notifications were on, before the first command. After a Reboot (`0x0F00`), the next connection began with the Reboot's own ACK (`02 0F 00`).
- **Session 3** (after Home Assistant had restarted, through a local adapter): not one copy and no replay at connection start in 50 connections and about 1,200 commands, in deep standby, cold starts and source switches. Then a Reboot cut a link while it was in use; after the reconnect, nearly every reply came twice again and every later connection began with the last reply of the one before, until the session ended.

So the copies come and go with the state of the link (inference: of the host's Bluetooth stack rather than the projector, but which one repeats them is not known). The client drops what arrives between commands, ignores replies for another opcode and, before sending the opcode of the last reply again (a retry, say), waits up to half a second for that reply's copy, also when a new connection's first command repeats the opcode of the last reply on the one before. A frame another controller left there (or one from before Home Assistant restarted) is dropped only if it comes before the first command, as every such frame did in these sessions. The bonding service gets a new subscription for each operation and drops what arrives before its request; opening the bonding window also waits for an answer to that operation (`0x10`), not just any success.

| Error | Meaning |
| --- | --- |
| `0x01` | checksum |
| `0x02` | invalid argument |
| `0x03` | unknown command (also briefly while switching between standby and on; once from GetHttpEndpoint about 46 s into a cold start, just as the main board came up) |
| `0xA0` | access denied: not bonded / link not encrypted |
| `0xC0` | timeout: the Bluetooth chip got no answer from the main board, after about 2 s. Every main-board read once the main board sleeps in standby, main-board reads in the first seconds after switching on from there, and a few system controller reads in the first 8 seconds after switching on ([below](#start-up-of-the-main-board)) |
| `0xD0` | module error |
| `0xE0` | not in the app (it maps to `Unknown`); the integration calls it `MAIN_BOARD_STARTING`: the main board is not up yet (see below). Also GetFocus `0x0141`'s answer in standby (within 0.1 s), in the first half hour as later, so the integration does not ask it there |

Once, the link statistics of a few standby polls and a cold start (integration 1.12.1, whose log was not kept) also counted a NAK as error `0x00`: either a NAK with code `0` or one without a code byte (the client reads both as `0`); which command it answered is not known.

### Start-up of the main board

Commands with opcodes below `0x0100` are handled by the main board (the SoC that shows posters and HDMI and has the Wi-Fi); the others by the system controller (power, battery, sound, light engine, picture).
The main board goes to sleep about 30 minutes into standby, and switching on after that is a cold start: it takes the main board about 45 seconds to come up again. Seen on one projector (main board firmware 3.1.15, 2026-11-22), switched on over Bluetooth, timed from the power command's acknowledgement:

- **Falling asleep:** standby polls (one a minute) read the main board normally up to 29.9–30.0 minutes into standby each time, and got `0xC0` from 31 minutes on (twice; a third time, the main-board entities went unavailable 31 minutes in). From then on every main-board read in standby answered `0xC0` after about 2 s (seen up to about 8 hours in, and after days), while the system controller's answered normally within about 0.1 s, except GetFocus (`0xE0`).
- **Warm start:** after 6 minutes in standby (once) there was no `0xE0`: a source switch 5 s after power-on worked. That the main board's sleep is what makes the next power-on a cold start is inference, but every power-on seen fits it: from 6 minutes of standby warm, from 31 minutes on cold.
- **What answers during a cold start:** main-board reads may still answer `0xC0` for the first few seconds (about 5 s, once, when switched on 31.6 minutes into standby), then every main-board command answers `0xE0` (SetNetTopology, GetAppState, GetPoster, GetNetTopology, GetHdmiStatus, the firmware versions, GetHttpEndpoint, GetWeather), mostly within 0.1 s (`030032e0` for GetAppState), but SetNetTopology took 4.3 s and 6.3 s to be rejected (twice). The first normal GetAppState, within a few seconds of the main board coming up each time, already gave the app the projector showed before standby (`16` or `96`): no `0` phase like the one once seen after [Reboot](#reboot). Once (2026-11-24, after a standby of unknown length with the main board asleep) the first normal GetAppState answered `0`, at +46.3 s, and the next, 3 s later, `16`.
- **The system controller** answered normally throughout, apart from the first 8 seconds: GetPower `0x0801` answered `0xC0` at +3.7–3.8 s and +7.4 s, then normally from +7.5 s (twice; once more only at about +6.7 s; after about 8 hours of standby not at all); once GetPower answered `0xE0` (after 1.5 s, at +6.3 s); once GetAutoStandby `0x0811` answered `0xC0` a few seconds in. The `0xC0` times are when the second and third tries of the poll right after the power-on were sent, the first try's answer having come before; the third answered on. No rejection of GetPower was seen in standby or once on, apart from a stuck internal link ([below](#known-quirks)). The integration tries GetPower three times, 1.5 s apart, when it answers `0x03`, `0xC0` or `0xE0`.
- **How long:** with a main-board read every 2.5–3.5 s, the main board came up between +38.6 and +45.7 s (31.7 minutes of standby), +42.7 and +46.3 s (60 minutes), +44.1 and +46.6 s (about 8 hours) and +44.8 and +47.3 s (31.6 minutes, switched on by choosing a source in standby). With reads further apart: `0xE0` at +34 s and normal at +48 s (31 minutes); `0xE0` at +32 s and normal at the next read, at +96 s (days). So about 45 s each time, however long the standby.
- **After Reboot** (`0x0F00`, [below](#reboot)) the main board starts up the same way, without standby.

The integration therefore treats every power-on (from Home Assistant, or seen by a poll) as a start-up until a main-board read answers, and every Reboot until the main board says what it shows (app state `0` counts as not yet): commands rejected with `0xE0` wait for it (at most 120 s), one GetAppState is sent every 10 s meanwhile, and a poll follows as soon as the board is up, or when the integration stops waiting. Once a start-up is known to be a cold one (after Reboot, or once a main-board read answered `0xE0`), a source switch waits for it to end before it is sent, without holding up Reboot, and is dropped if the projector goes to standby meanwhile; what the projector does with a switch while it answers `0` is unknown. At the 120 s limit after Reboot, a board that answers at all counts as up. While a start-up is under way, a rejected GetPower keeps the power the last poll read.

Tried with integration 1.12.0 (once each): HDMI chosen right after a cold power-on, HDMI chosen right after Reboot, and the Weather poster chosen in standby (which switches the projector on) all waited and were then applied. The switch went out 3–4 s after the first normal answer to a test read sent every 2.5 s (the integration's GetAppState probe is 10 s apart); after a cold start the projector reported the new source 21–22 s after that answer, the switch itself taking 17–19 s, and after Reboot 3 s after it (that switch was answered at once, see [Reboot](#reboot)). In the standby case 1.12.0 failed one whole poll when GetPower answered `0xE0`, so every entity was unavailable for 13 s; that is what the rule above is for.

In standby, once a main-board read in a poll answers `0xC0`, the integration counts the main board as asleep and skips its other reads in that poll (each field unknown with an error, as if it had answered `0xC0` too; diagnostics show `main_board_asleep`): a poll then takes about 2 s plus the system controller's reads instead of 8–12 s (about 5 s in all: 10 polls, about 8 hours into standby). The next poll asks again, and the first one after switching on reads everything.

### Change notifications

From the app (`UartCommandList`, `CharacteristicInfoCache`, `AppCommandCPInfo.parseAppCommand`, `BleCpAdapter.onNotify`); never seen from a real projector (firmware 3.1.15):

- **Changes from Home Assistant:** none arrived on 2026-11-22 in three sessions of switching power and source through Home Assistant, though the link was open only during polls and commands.
- **The power button** (2026-11-24, `tools/at_projector.py` from macOS): a controller stayed connected and sent nothing for about three minutes while the power button switched the projector to standby and, under a minute later, on again (its picture back within seconds, as after a short standby). Not one frame came on `EE002916`, and the link stayed up throughout (GetPower read on afterwards). So this firmware did not notify a power change made with its button (tried once), the case they would matter most for. Other changes at the projector, and those it makes by itself (auto standby, timers), were not listened for.

The projector can also notify on `EE002916` unasked, when something changes. `UartCommandList` lists four such opcodes (`isNotifyCommand`) and no others; no command has an opcode with the top bit set.

| Opcode | Name in the app | Value the app maps it to |
| --- | --- | --- |
| `0x8000` | NotifySOCPowerStateChanged | `SocPowerState`: u8 (0–255), which has no get or set command; that it uses `0x10`/`0x00` as Power (`0x0801`) does is not certain |
| `0x8010` | NotifySocModeChanged | `SocMode`, as GetSocMode `0x0021`: u8, `0` unknown, `16` initial settings, `32` system update, `48` demo, `64` ready |
| `0x8011` | NotifySocNetTopologyChanged | `SocNetTopology`, as GetNetTopology `0x0031`: u8 |
| `0x8012` | NotifySocApplicationStateChanged | `SocAppState`, as GetAppState `0x0032`: u8 |

- **Framing:** the app parses every frame on this characteristic the same way, `[result:u8][opcode:u16][data…]` (`parseAppCommand`), and `UartCommandList.getParentUUID` maps the four opcodes to the values above, which only works with the result byte first. So a notification is presumably `[result][80 12][value]`. Which result byte the projector sends is unknown; `AppCommandCPInfo` names `1` COMMAND next to `2` ACK and `3` NAK, which suggests `1`. Each value is decoded as for its get command (type `17`: the first data byte).
- **What the app does with them: nothing.** `BleCpAdapter.onNotify` only completes the request in flight, when the frame maps to the value that request asked for, and drops every other frame after a debug log. Nothing else looks at these opcodes (`isNotifyCommand` is never called); the screens learn about changes only by reading again. A side effect: as `0x8011`/`0x8012` map to the same values as GetNetTopology/GetAppState, one arriving while the app waits for such a read is taken as its reply.

The integration tells a notification from a reply by its opcode alone (`0x8000` and up; a reply echoes the opcode of a command), so a notification never completes a command, whenever it arrives. Any other frame, such as a notification framed without the result byte, is left to the replies, which drop what does not match, as before. The client logs every notification at debug level (`notification 0x8012 (APP_STATE), result 1, data 10 (frame 01801210)`), and frames dropped between commands (`dropping frame … that arrived between commands`).

On a power notification the coordinator polls as after switching power from Home Assistant (after 3, 8, 15 and 30 seconds); on a topology or app state notification it asks for a poll at once (debounced like any refresh request). It ignores notifications during its own power changes and source switches (which poll when done), a frame that repeats the last of its kind since a poll last saw the power or the source change, `0x8010` and unknown opcodes. Notifications only arrive while the link is open, right after a poll or a command; the integration does not stay connected to wait for them. Values are not applied directly until a projector confirms them. `tools/lspx_cli.py listen` stays connected and prints them.

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
| `0x0171` | `0x0170` | Picture direction | `0` mirror horizontal, `1` rotate 180°, `2` normal, `3` mirror vertical (all four accepted and read back; seen once, each compared with `2`: `1` turned the picture upside down, `0` mirrored it left to right, `3` top to bottom, as the app's names say) |
| `0x0131` | `0x0130` | Auto picture direction | u8 0/1 |
| `0x0181` | | Projection ranges | `[bright min][bright max][keystone min:s8][keystone max:s8]` |
| `0x0501` | `0x0500` | Picture mode | `0`–`2` Preset 1–3, `16`/`17` Custom 1/2 |
| `0x0511` | | Picture ranges | min/max pairs: saturation, brightness, contrast, sharpness, RGB |
| `0x0521` | `0x0520` | Picture saturation | u8 (only effective in Custom modes) |
| `0x0531` | `0x0530` | Picture brightness | u8 |
| `0x0541` | `0x0540` | Picture contrast | u8 |
| `0x0551` | `0x0550` | Picture sharpness | u8 |
| `0x0032` | | Current app | `16` poster, `32` content viewer, `48` mirroring, `64` DLNA, `80` YouTube, `96` HDMI |
| `0x0031` | `0x0030` | Network topology | u8: `16` home, `17` home + poster, `18` home + content viewer, `32`–`34` the same as guest, `48` wireless HDMI. Setting `17` is acknowledged, but reads after it gave `16` every time |
| | `0x0033` | Stop the current app | no payload |
| `0x0067` | `0x0066` | Poster | u16 theme, see below |
| `0x006A` | `0x0069` | Weather poster's weather | u16, the picture the poster shows next time it is shown: the app's codes `1`–`11`; `0`–`19` and `100` were all stored, see below |
| `0x0004` | | Main board firmware | NUL-padded ASCII |
| `0x0831` | | System controller firmware | NUL-padded ASCII |
| `0x0841` | | Light engine firmware ("LD" in the app) | NUL-padded ASCII |
| `0x0005` | | Wireless HDMI unit firmware | NUL-padded ASCII |
| `0x0070` | | Wireless HDMI status | `[enabled][status]`: status `0` disconnected, `1` source unplugged, `2` no signal, `3` connecting, `4` connected, `5` changing resolution, `6` changing source, `16` updating. On one projector, with no source on the wireless HDMI unit (status `1`, source unplugged, once settled on HDMI): `00 00` on a poster, and on HDMI `01 00` at the first read after the switch, `01 01` from the next on (every time). It follows a source plugged in and out within about a second ([below](#hdmi-status-with-a-source)) |
| `0x0844` | | Usage time | `[seconds:u32]` the projector has been on in total; rises by 1 a second while on, stops in standby, never went down. Across one Reboot it resumed from about where it had been 48 s before (inference: it is saved now and then, not every second) |
| `0x0811` | `0x0810` | Auto on/standby | `[phone nearby][inactivity][timer]`, each a bit set: `1` switch to standby, `2` switch on. Set all three bytes. The app always writes `0` as the third byte and never reads it: its timers are [over HTTP](#timers). One projector reads `00 01 01`; what set its third byte is not known, and it once went to standby by itself ([Known quirks](#known-quirks)) |
| `0x0064` | | HTTP endpoint | `[port:u16][IPv4 ASCII]`, see [HTTP API over Wi-Fi](#http-api-over-wi-fi) |
| `0x0065` | | Wi-Fi station status | `[event flags:u16][SSID…]` |
| | `0x0F00` | Reboot | no payload; from the app, seen twice to restart a projector (see below and the start-up of the main board) |

When an "auto" setting is on, the projector keeps overriding the matching manual value; switch the auto setting off before setting the value. Seen once each, in a dark room where nothing moved: switching auto brightness on (`01 01`) took brightness from 100 to 60 at once, and auto focus on moved focus from −383 to −370; neither changed again in the next 5 minutes.

### Poster and HDMI

From the official app's Bluetooth-only code path; confirmed on a projector with firmware 3.1.15.

- **HDMI:** set the network topology to `48`.
- **Poster:** if the current app is not the poster (`16`), stop it (`0x0033`) and wait about 3 seconds; then set the poster. If the topology was `48`, set it back to `17` (firmware 3.0.0 and later) or `16`.

The projector may answer these commands, and reads sent while a switch is still settling, only once the switch is done: Stop the current app and SetPoster mostly within a second (once more than 8 seconds between posters, before these timings), the topology change after 12–18 seconds (below). The app waits up to 60 seconds; so does the integration for these steps.

Timings on 2026-11-22 (firmware 3.1.15, the wireless HDMI unit reporting source unplugged); every switch was read back as the new source:

- **Poster → HDMI:** SetNetTopology `48` answered after 17.2–17.4 s (four times), also when sent between two reads of a poll. Once, sent 3 s after the main board came up from a Reboot, it answered in 0.07 s.
- **HDMI → poster:** Stop the current app and SetPoster answered within about 0.6 s; SetNetTopology `17` after 12.5 s and 15 s (the two times it was timed); the whole switch, with the integration's 3.3-second wait, took 16.4–17.0 s as Home Assistant timed it (four times).

Poster themes, with the app's names (its "Change poster" screen):

| Theme | App name | Theme | App name |
| --- | --- | --- | --- |
| `11` | Weather | `3` | A day |
| `10` | Clock 1 (digital, 12-hour, factory default) | `4` | Journey |
| `9` | Clock 2 (digital) | `5` | Night dreams |
| `7` | Clock 3 (analog) | `6` | Window |
| `8` | Clock 4 (analog) | `13`/`14` | My slideshow 1/2 (photos sent from the app) |

Clocks and weather depend on data the app sends over HTTP (the time, the weather pictures), so without the app they show whatever the projector last received. The integration sets the time itself ([Clock](#clock)).

Clock 1 is a 12-hour face: on one projector (firmware 3.1.15) it showed 11:36 while the projector's clock read 23:36 ([Bluetooth tunnel](#seen-on-a-projector)). No 12/24-hour setting is known, in the app or on the projector.

### HDMI status with a source

Seen once (firmware 3.1.15, 2026-11-24, `tools/at_projector.py` reading GetHdmiStatus `0x0070` and GetAppState about once a second on HDMI, while the person at the projector plugged a source into the wireless HDMI unit and typed what happened; their answers come a few seconds after each change):

- **Nothing plugged in:** `01 00` (disconnected) at every read for the first 38 s after the switch, not `01 01` as in the sessions above.
- **Plugged in:** `01 06` (changing source), 3 s later `01 05` (changing resolution), 1 s later `01 04` (connected); the picture appeared.
- **Unplugged:** `01 01` (source unplugged).
- **Plugged in again:** `01 02` (no signal) at the next read, `01 04` (connected) 2.2 s later; the picture appeared.
- **The picture went** (the person did not say it was unplugged): `01 02` (no signal).
- App state read `96` (HDMI) and the enabled byte `1` throughout; in standby afterwards, and on a poster in the next session, `00 00`. `3` (connecting) was not seen.

So the status follows the source within a read or two. The first report (#14, integration 1.3.x, read once a minute) of it staying "connected" through a couple of minutes of plugging in and out is not explained by this.

### Weather

The app's Bluetooth SDK has SetWeather (`0x0069`) and GetWeather (`0x006A`), the `SocWeather` characteristic, but the app never calls them.

Seen on one projector (firmware 3.1.15, 2026-11-24, its owner looking at the picture; `tools/at_projector.py` and `tools/lspx_cli.py weather`):

- **Stored at once, drawn later:** SetWeather was acknowledged with its payload echoed (`02 00 69 00 07` for code 7) within 0.05–0.18 s, and GetWeather then read the code sent, for every code tried. The picture changed only when the Weather poster was shown again (another poster, then Weather), for every code tried by hand. The one change seen right after a SetWeather alone (code 7, at the start of the session) came 0.07 s after SetPoster had switched to the Weather poster, so presumably while the poster was still being drawn **(inferred)**; the next five codes sent with the poster showing left it unchanged.
- **The pictures**, as the owner described them (shown again after each code):

  | Code | Picture | Code | Picture |
  | --- | --- | --- | --- |
  | 1 | a forest on a clear day | 7 | close-up of a tree in daylight, raining |
  | 2 | sunset or sunrise by a swimming pool | 8 | drops running down a window, sound of heavy rain |
  | 3 | fog in a forest with sun through it, birds | 9 | heavy rain in a dark forest |
  | 4 | bright, a bit cloudy, ocean | 10 | lightning over a rainforest, rain and thunder |
  | 5 | rain clouds over a field, blue-ish sky at the horizon, no rain | 11 | snow-covered spruce branches, snowing, quiet |
  | 6 | dark clouds through a window, sounds like rain | | |

  Outside the app's range, every code was acknowledged and read back too: `12` showed a "get the app" picture (store badges, a QR code), `13`–`19` photos the owner knew from the slideshow posters, `0` and `100` a black screen. `20` was sent in the run that ended in a standby the projector chose by itself ([Known quirks](#known-quirks)); what it showed was not noted. So the code seems to pick one of the pictures the projector holds by number **(inferred)**.
- **Earlier** (2026-11-22, no one looking): code `5` was acknowledged and read back while the projector already held `5`. Sent once while HDMI was showing (by an older version of the integration), it was acknowledged too and the projector stayed on HDMI, by read-back. What it does while another poster shows was not tried. GetWeather is a main-board read: `0xC0` once the main board sleeps in standby (seen again on 2026-11-24), `0xE0` during a start-up.

- **Value:** u16 in both directions (`CharacteristicInfoCache` type `18`, no range given). `BlePosterIfs.setWeather(int)` passes the number through unchanged, and the app's message handler only hands it on (`POSTER_WEATHER_SET`, nothing sends it). So the SDK says nothing about what the number means; the only weather number the app gives the projector anywhere is its poster code below, so that is what the integration sends. For code 7: `00 69 00 07`.
- **What the app does instead:** at most once an hour it fetches the current conditions from AccuWeather, turns the icon number into a poster code (`poster/weather/c.convertWeatherCodeToPosterCode`) and, through the HTTP poster API, makes the poster content tagged `weather_00NN` (or `weather00NN`) the Weather poster group's contents (`PosterCoordinator.applyWeather`). If the Weather poster is showing, it switches to the Blank theme for 300 ms and back, so that the poster is redrawn: the same need as for SetWeather above.
- **Blank is not a theme over Bluetooth:** it is a poster group of the HTTP API (`PosterCoordinator.PosterState.Blank`), tagged `blank` and titled "Blank", that the app creates itself (`POST /v1/poster/groups`) when the projector has none, and chooses by its id with `POST /v2/poster/groups/selected` `{"id": …}`, as it does the Weather group (`weather0`). SetPoster takes only the `PosterTheme.V10` numbers (`1`–`14`, `10001`–`10005`, `20001`–`20005`, `30001`–`30100`), and none of them is Blank. So it could be reached over the [Bluetooth tunnel](#bluetooth-tunnel) at best (group list, perhaps creating the group on the projector, then choosing it), none of which was tried; the integration shows a built-in poster in between instead (below).

The app's names for its codes, which do not always match what the projector shows for them (above: `3` is fog, `9` heavy rain):

| Code | AccuWeather icons | Picture | Code | AccuWeather icons | Picture |
| --- | --- | --- | --- | --- | --- |
| 1 | 1, 2, 33, anything else | sunny / clear | 7 | 12, 13, 14 | showers |
| 2 | 3, 4, 34 | partly sunny | 8 | 18, 24, 25, 26, 29, 39, 40 | rain, ice, sleet, rain and snow |
| 3 | 5, 35, 36 | hazy | 9 | 19, 20, 21, 43 | flurries |
| 4 | 6, 30 | mostly cloudy, hot | 10 | 15, 16, 17, 41, 42 | thunderstorms |
| 5 | 7, 31, 38 | cloudy, cold | 11 | 22, 23, 44 | snow |
| 6 | 8, 11, 32, 37 | dreary, fog, windy | | | |

The integration (only when a weather entity is chosen in its options) maps Home Assistant's conditions by what the pictures show: `sunny` → 1; `clear-night` → 2 (the one clear sky that is not daylight); `partlycloudy`, `windy` → 4; `cloudy`, `windy-variant` → 5; `fog` → 3; `rainy` → 7; `pouring` → 9; `hail`, `snowy-rainy` → 8; `lightning`, `lightning-rainy` → 10; `snowy` → 11; anything else (`exceptional`, unknown) sends nothing. 6 (rain heard, not seen) is not used. It sends the code only while the Weather poster is shown (what SetWeather does while another poster shows is unknown; with HDMI, see above), waiting for a reply as long as for a poster switch (60 s):

- **Chosen in Home Assistant:** right after the SetPoster, as in the one change seen drawn at once (0.07 s after it).
- **The condition changes while the poster shows** (or polls reach the projector again with a code not sent yet): GetPower, GetAppState and GetPoster first (the last poll may be a minute old, and the power button sends no notification), and nothing more unless they show the Weather poster; then SetWeather, and the poster is drawn again: SetPoster Night dreams (`5`), 0.5 s, GetPower, SetPoster Weather (`11`). Night dreams stands in for Blank (the darkest-sounding built-in poster; a clock was not wanted); 0.5 s is about the app's 300 ms plus its HTTP round trips. The Weather poster is chosen again (once more if that fails) also when choosing Night dreams failed, as it may have been chosen all the same, but not when GetPower reads standby or is rejected (switching power): SetPoster in standby switches the projector on. A failure is logged as a warning and polled at once, so the source then shows what the projector shows; a failed send is tried once more a minute later. The whole sequence holds the lock that source switches and power changes take, so one chosen meanwhile waits for it, and a poll that reads Night dreams meanwhile shows the Weather poster.
- **The poster appears some other way** (the app, the projector, switched on onto it): GetWeather first; nothing more if the projector holds the code (it draws it as the poster appears), otherwise as for a change.

Diagnostics read GetWeather; polls do not. `tools/lspx_cli.py weather <code> --redraw` sends a code and redraws the poster the same way.

### Reboot

From the app; tried twice on a projector (firmware 3.1.15), while on and showing the Weather poster, through Home Assistant:

- **Reply:** an ACK with no data (`02 0F 00`), within 0.6 s of starting to connect (0.15 s after sending, when timed): the link did not drop before it. The projector stays on (GetPower answered on throughout) and restarts its main board, which answers `0xE0` until about +46 s and +47.2 s and normally from about +49.4 s and +49.7 s after the ACK: a few seconds longer than a [cold start](#start-up-of-the-main-board).
- **The link:** the integration disconnects after the ACK. Once, the next connection was lost 2 s after it opened, about 6 s after the ACK; the other time the first frame on the next connection was the Reboot's ACK again ([Command framing](#command-framing)).
- **After it:** the first normal main-board reads once gave app state `0` and topology `0` for about 13 s before `16` and `16` (the Weather poster again); the other time no `0` was seen, and HDMI, chosen right after the Reboot, was sent as soon as the main board answered and acknowledged in 0.07 s (a switch from a poster otherwise takes about 17 s), then read back as HDMI.
- **Kept:** every setting read back as before, the controller list and the bond were unchanged (no new pairing), both times. Usage time once resumed from about where it had been 48 s before the Reboot (the other time no loss showed at 0.01 h). The battery's charge state (charging or full) changed around it both times; whether that is the Reboot is not known. Firmware versions were not read again.

From the app:

- **Bytes:** `0F 00`, nothing else. `CharacteristicInfoCache` defines Reboot as `APP_COMMAND_NO_VAL`, so the value `1` that `BleDeviceIfs.reboot()` passes is dropped when the command is built, as for Stop the current app (`0x0033`).
- **Reply:** the app waits for the usual notification (up to 60 s) but, for Reboot, factory reset and initialise only, counts the link dropping before it as success (`BleControl.writeSync`): the projector may restart before it answers. A NAK is a failure.
- **Use in the app:** only right after entering or leaving demo mode, which is hidden on the About screen (tap a version number quickly more than ten times); nothing is read or waited for afterwards. The normal screens never send it.

The integration sends it the same way, also counts no reply within 8 seconds as success, and then disconnects and polls again a few times.

## HTTP API over Wi-Fi

From the official app (`lspxsdk/web/RowAPI.java`, `RowSettingsAPI.java`, `lspxsdk/DeviceController.java`, `e0/i.java`); not yet tried over Wi-Fi on a projector (the clock's path was, through the [Bluetooth tunnel](#bluetooth-tunnel)).

What one projector reported with no home-network address Home Assistant could reach (firmware 3.1.15, 2026-11-22; topology `16` on posters, `48` on HDMI; that it was not on a home Wi-Fi network at all is inference, Wi-Fi station status `0x0065` was not read): GetHttpEndpoint `0x0064` answered port 80 with the wireless HDMI unit's address (`isWivu`, below), on HDMI and on a clock poster right after leaving HDMI, in one session; in a later one, port 80 with an empty address, on HDMI and on the Weather poster; and once `0x03` during a cold start. HTTP to the wireless HDMI unit's address timed out from Home Assistant (the clock and all three settings over Wi-Fi, 10 s each). So no part of the HTTP API could be tried over Wi-Fi, nor the timers, the poster settings or the clock. On 2026-11-24 it was the same: an empty address on Clock 1 after a cold start, and in a later session (HDMI shown in between) `192.168.8.100` on Clock 1 with topology `16`. The clock's path did work through the [Bluetooth tunnel](#bluetooth-tunnel).

- **Address:** the app asks over Bluetooth with `0x0064` (`BleDeviceIfs.getEndPoint()`, parsed by `ReadAppEndpointInfo`: `[port:u16]`, then the IPv4 address as ASCII, trimmed) and stores only the address (`e0.i.q()`, preference `lspxp1_address`). It never uses the port: `DeviceController.connect(url)` always adds `:80`, so the base URL is `http://<address>:80`. It refuses to store `192.168.8.100` (`EndPoint.isWivu`, the wireless HDMI unit); `192.168.8.x` is the projector's direct link (`isDirect`) and `192.168.8.1` its own access point (`isSoftAp`). On its start-up check (`oobe/f.java`) and on the Wi-Fi status screen (`Z/g.java`) it ignores direct addresses, and after switching back from wireless HDMI (`f0/e.java`) it reads the address again while it is direct; elsewhere it stores any address and uses it if it answers a reachability check. Wi-Fi Direct (the app's setting, "guest mode" in the SDK) is decided by the topology instead: switching it on calls `enterGuestMode` and waits for `32` (`z/i.java`), and the home-network reachability check is skipped in `32` and `48` (`z/c.java`). So the app uses a `192.168.8.x` address over HTTP when the phone is joined to the projector's own access point with Wi-Fi Direct on (which Home Assistant normally is not), and on a home network that itself uses `192.168.8.x`, which its start-up check refuses but other screens accept. While it waits for the address after Wi-Fi setup, it stores `0.0.0.0`; its default is `192.168.11.1`.
- **The projector's own network in the integration** (`HttpEndpoint.home_host()` in `protocol.py`, used by the clock and the settings over Wi-Fi): nothing is sent over HTTP while the topology is Wi-Fi Direct (`32`–`34`) or wireless HDMI (`48`), as the app decides, and never to a `192.168.8.x` address (`isDirect`), whatever the topology: Home Assistant is not on that network, the projector may still report such an address for a while after switching back to the home network (the app reads it again until it is not direct), and `192.168.8.1` is also the default address of some home routers (GL.iNet's). So a projector on a home network that itself uses `192.168.8.x` gets no clock or settings over Wi-Fi, as with the app's start-up check; elsewhere the app is laxer and uses any address that answers. A refused address is not kept, so the next attempt reads it again. Taken from the app; what a real projector reports with Wi-Fi Direct on has not been seen yet (`tools/lspx_cli.py endpoint` prints the address and the topology).
- **Requests:** OkHttp 2, every request with `Connection: close`; POST and PUT bodies are JSON (`Content-Type: application/json; charset=utf-8`, Gson output without spaces). No authentication, token or cookie anywhere in `RowAPI`. Timeouts 30 s (connect, read, write), 5–10 s for some control panel requests, and it retries on connection failure.
- **Replies:** `RowAPIResponse` JSON: `{"status": "200", "message": …, "result": …}`. A failed HTTP status throws. A 200 with an empty or non-JSON body counts as `{"status": "200", "message": "OK"}` (no `result`). Most setters return the new value in `result`; some (`/v1/settings/cp/...`) only check `status == "200"`.
- **Reachability:** `RowAPI.isAvailable()` is `GET /v1/settings/time` with a 4 s connect timeout. When it fails, `DeviceController.connect()` falls back to the current Bluetooth connection, through which some calls (such as `getName()`) use the tunnel instead ([below](#bluetooth-tunnel)).

### Clock

`DeviceController.setLocalTime()` → `RowSettingsAPI.setTimeSetting()`:

```http
POST /v1/settings/time HTTP/1.1
Content-Type: application/json; charset=utf-8
Connection: close

{"time":1782914400,"timezone":"GMT+0"}
```

- **`time`** is the phone's local wall-clock time as Unix seconds: `(now_ms + TimeZone.getDefault().getOffset(now_ms)) / 1000` (`e0.v.a()`), so 14:00 in Berlin in summer is sent as 14:00 UTC. `timezone` is always `GMT+0`. Since no real time zone is sent, the projector cannot follow daylight saving: the time has to be sent again after each change.
- **Reply:** `result` is `SettingsTime` (`{"time": …, "timezone": …}`); the app counts a reply without `result` as a failure (`setLocalTime()` returns −1, `e0.v.e()` false); only on start (`g()`) does it then try the Bluetooth tunnel.
- **Reading it:** `GET /v1/settings/time` returns the same `SettingsTime` (`getLocalTime()`; the app only uses it to check reachability).
- **When the app sends it** (`e0.v`): `g()` on start (`MainActivity`, `a0/d.java`), over HTTP first (`e()`) and through the tunnel if that fails (`f()`); when a clock poster is chosen (`lspxp1/b.java` `N()`/`P()`, `j/d.java` `i()`/`j()`, through the tunnel on the Bluetooth-only path and over HTTP otherwise); otherwise at most once an hour (preference `last_update_localtime`).
- **Timers** use the same wall-clock seconds ([below](#timers)), so they only fire at the right time while the clock is right; the app sets the clock whenever its Startup/Networked Standby settings screen opens (`a0/d.java` → `e0.v.g()`).

The integration (`clock.py`, `tunnel.py`) sets it through the [Bluetooth tunnel](#bluetooth-tunnel) as the app's `setTime(long)` does: `POST /v1/settings/time` with `{"time":N}` alone (the same wall-clock seconds, no `timezone`), right after a poll while the link is still open, holding the link for the whole request (no command in between; the time is taken once the link is free), then disconnecting as after a poll. It reads the reply as below (a JSON `status` other than `200` or a `code` other than `0` counts as a refusal, as does an HTTP status outside 2xx; the tunnel's replies carry `code`, [seen](#seen-on-a-projector)), takes only notifications with the `seq` and op of the frame just sent, waits up to 8 s for each, asks for the response again up to 5 times a second apart while it answers an error, and gives up after 20 s in all. Its `seq` runs on across requests and connections, as the app's does, and moves past each step before its answer is awaited, so a late answer to a failed request cannot be taken for the next one's. It sets the clock when a poll first sees a clock poster (or after one is chosen in Home Assistant), once a day, and when Home Assistant's UTC offset changes, only while the projector is on, its main board up (not in standby, not starting up) and a poster showing; after a failure, automatic attempts wait 5 minutes, then an hour. Sync clock tries whatever shows, but only while the projector is on and its main board up. The time zone is never written: only `GMT+0` was seen in a reply.

Only when the tunnel is missing or refuses (an error in a notification, no answer, an HTTP or JSON refusal; not when the Bluetooth link itself fails) does it fall back to Wi-Fi, as follows (never seen to work). It reads `0x0064` only then; skips it while the topology is Wi-Fi Direct (`32`–`34`) or wireless HDMI (`48`); treats an empty address, `0.0.0.0` or anything that is not IPv4 as "not on Wi-Fi" and sends nothing to the projector's own network ([above](#http-api-over-wi-fi)); and POSTs exactly the body above to port 80 with Home Assistant's time zone, a 10 s timeout and no lock held. A non-2xx status or a JSON `status` other than `200` counts as a failure; any other 2xx counts as done, with or without `result`.

### Timers

The app's Settings → Startup/Networked Standby settings → "On at a specified time" and "Switch to Networked Standby at a specified time" (`a0/d.java` lists them, `a0/e.java` is the time picker: 24-hour, any minute, and a switch). `DeviceController` turns each into entries of `/v1/settings/timers` (`RowSettingsAPI`):

- **Entries** are `SettingsTimer`: `{"id": …, "tag": …, "state": "on"|"off", "time": N, "creation": …, "modification": …}`. Each switches the projector on (`state` `on`) or to Networked Standby (`off`) **once**, at `time`, which is wall-clock time as Unix seconds like the [clock](#clock) (no time zone). The app tags its own entries `timer_on` and `timer_off` and ignores any others. (`SettingsAlarm`, with `enable`, `state` and `time` as `"HH:mm"`, is only the app's own view of them; it is never sent.)
- **Reading:** `GET /v1/settings/timers` returns them all as a JSON array in `result` (`getTimersList()`, tried twice). A timer is on when at least one entry has its tag; its time is the `HH:mm` of the first such entry (`getCurrentONAlarm()`/`getCurrentOFFAlarm()`). When none exists, the app shows "Off" and keeps the time it last knew in its preferences (`a0/h.java`, `KEY_ALERM_HM@ON_TIMER`).
- **Switching on or changing the time** (`setCurrentONAlarm("HH:mm")`, `h()`): read the list, then `POST /v1/settings/timers` seven new entries, one a day from **today** at that time (today's even when it has passed), and then `DELETE /v1/settings/timers/<id>` every old entry with the same tag. Each POST body is Gson's output of a new `SettingsTimer` (no `id` yet), for 23:00 on 1 July 2026: `{"state":"off","time":1782946800,"creation":0,"modification":0,"tag":"timer_off"}`. The app adds 24 hours per entry and uses the UTC offset of the moment it writes them, so all seven have the same wall-clock time.
- **Switching off** (`deleteONAlarms()`/`deleteOFFAlarms()`): delete every entry with the tag. The app ignores failed deletes and failed creates (no reply needed).
- **Renewing:** since each entry fires only once, seven of them last a week. At most once a day while it is open (`e0.v.d()`, from the main screen, `lspxp1/c.java`, preference `last_update_alarm_time`), the app reads both timers and, for each that is on, writes its seven entries again from today. That is why it re-sends them daily, not daylight saving: entries are wall-clock times, like the clock.
- **Not over Wi-Fi:** the app refuses to change timers while the projector shows HDMI or screen mirroring ("cannot set because of HDMI or Miracast"); it has no Bluetooth tunnel route for them (`BleWebApiPassThroughIfs` has no timer calls).
- **The auto standby flags** (`0x0811`, third byte "timer") are unrelated in the app: it writes `0` there whenever it changes the other two and never reads it. The bonding service's Get/SetSleepTimeMinute and Get/SetWakeTimeMinute (`0x20`–`0x23`) are not used by the app's screens either.

The integration (`web_settings.py`, entities disabled by default) does the same: a switch per timer (on when entries with its tag exist), a time entity per timer (the first entry's `HH:mm`, in Home Assistant's time zone, which is the projector's once the clock is synced; while the timer is off, the last time it knew, kept across restarts), writing seven entries from today and then deleting the old ones, and deleting them all to switch it off. It writes them again once the last entry is less than five days ahead, so about daily, but only while one of these entities is enabled and the projector is on. It reads them right after a poll: every 10 minutes, after a change, and after the projector moves between home, Wi-Fi Direct and wireless HDMI; only while it is on, with Wi-Fi Direct off and not showing wireless HDMI; reusing the endpoint the clock read (both read `0x0064` at most once an hour, and again after standby, a topology change or a failed request).

### Poster settings

- **Initial mode** (the app's Settings → "Initial mode", `a0/c.java` with `a0/b.java`; "Select the Projector's mode when it is turned on or awakes from Networked Standby"): `GET /v1/settings/posterResume` returns `{"posterResume": true|false}` in `result` (`SettingsPosterResume`); `true` is the app's "Poster", `false` "Last mode displayed". The app sets it with `POST /v1/settings/posterResume` and the body `{"posterResume":true}` and counts a reply without `result`, or with another value, as a failure (`PosterCoordinator.setPosterResumeMode`). Over Wi-Fi when it can (`RowSettingsAPI`, timeouts 3 s while the screen is open), otherwise through the tunnel (`POSTER_RESUME_MODE_GET`/`SET`, the same JSON).
- **Slideshow interval** (Poster/slideshow settings → "Photo display time", `m/a.java`; picker `n/g.java`, custom picker `n/d.java`): `GET /v2/poster/duration` returns `{"value": N}` in `result` (`PosterPlaybackInterval`), `N` in **milliseconds**; `POST /v2/poster/duration` with `{"value":N}` sets it, and the app again counts another value in the reply as a failure. The picker offers 10 s, 5 min, 15 min, 30 min, 1 hr and 12 hr (default 10 s), or Custom: hours 0–11, minutes and seconds 0–59, not all zero (1 s to 11:59:59). A value that is none of the presets shows as Custom. This is for firmware 2.0.0 and later (`e0.b.q()`); for older firmware the app writes the interval into each "ambient" poster group's layout instead (`PosterCoordinator.setIntervalForAll`), which suggests it also applies to the photo posters (A day, Journey, Night dreams, Window), not only My slideshow. Over the tunnel these are `POSTER_PLAYBACK_INTERVAL_GET`/`SET` with the same path and JSON (`BleWebApiPassThroughIfs.get/setPlaybackInterval`).

The integration (`web_settings.py`, entities disabled by default) shows them as a select (`last_mode`, `poster`) and a number of seconds (1 to 43200, step 1; the projector's milliseconds divided by 1000), read and written as above, on the same schedule as the [timers](#timers). Each setting fails on its own; the endpoint is read again only when none of the enabled settings answered.

### Bluetooth tunnel

The app can send the same HTTP requests over Bluetooth, through service `EE001950` (`BleWebApiPassThroughIfs` → `BleControl.webApi{Get,Post,Delete}Sync` → `BleWebApiExecutor`, with `BleWebApiJob`, `HttpApi` and `WebApiCommand`).
Reconstructed from the app, and tried on one projector for the clock only ([below](#seen-on-a-projector), [#1](https://github.com/kachkaev/ha-sony-lspx-p1/issues/1)): `GET` and `POST /v1/settings/time` worked as written up here. `tools/probes/web_tunnel.py` follows this section byte by byte and prints every frame.

Each statement is marked **(app)** when it is read from the app's code and **(inferred)** when it is what the projector presumably does for that code to work.
jadx's Java output of `BleWebApiJob.parseCPNotify` merges the branches of its `switch`, so that every reply op seems to carry a body length; the layouts below follow the method's raw instructions (`jadx --single-class … -m fallback`), which differ (and also differ from the layout first guessed in #1).

#### Characteristics

| Characteristic | Name in the app | The app | Carries |
| --- | --- | --- | --- |
| `EE00270D` | WebAPIpassthroughControlPoint | writes, subscribes (`Authority` write + notify) | short request frames out, a status notification back for each |
| `EE002C46` | WebAPIpassthroughParam | writes, reads (`Authority` read + write) | the path and the body out, the response body back |

#### Control point frames

Every number is big-endian (`CharacteristicConverter`) **(app)**. A write to `EE00270D` is `[seq:u16][op:u16][arguments…]` **(app)**:

| Op | Name in the app | Arguments | Written to `EE002C46` just before |
| --- | --- | --- | --- |
| `0x40` | HTTPPOSTWriteRequest | `[path length:u16][body length:u32]` | the path |
| `0x41` | HTTPPOSTWriteRequestBody | `[body length:u16]` | the body |
| `0x42` | HTTPPOSTReadResponse | none | |
| `0x43` | HTTPPOSTReadResponseBody | none | (read after it) |
| `0x50` | HTTPGETWriteRequest | `[path length:u16]` | the path |
| `0x52` | HTTPGETReadResponse | none | |
| `0x53` | HTTPGETReadResponseBody | none | (read after it) |
| `0x58` | HTTPDELWriteRequest | `[path length:u16]` | the path |
| `0x5A` | HTTPDELReadResponse | none | |
| `0x5B` | HTTPDELReadResponseBody | none | (read after it) |

`0x44` and `0x54` (HTTPPOSTWriteRequestGzip, HTTPGETWriteRequestGzip) are defined and parsed but never sent: the executor builds every POST with `0x40` and nothing creates a gzip request **(app)**.
Lengths are in bytes of the encoded path and body (`String.getBytes()`, UTF-8 on Android) **(app)**. The POST body length appears twice, as a u32 in `0x40` and as a u16 in `0x41` **(app)**.

The projector answers each control point write with a notification on `EE00270D`, `[seq:u16][op:u16][error:u8][extra…]` (`parseCPNotify`) **(app)**:

- **error ≠ 0:** the app reads nothing more. The codes are those of the command characteristic ([Command framing](#command-framing), `BleLowLayerError`); the app reports `0xA0` as access denied and every other code as a module error.
- **`0x42`/`0x52`/`0x5A`, error 0:** `[http status:u16][total length:u32]` in bytes 5–10, read only when the frame is at least 11 bytes long (otherwise the app takes status 0 and length 0, so an empty body).
- **`0x43`/`0x53`/`0x5B`, error 0:** `[length:u16]` in bytes 5–6 when the frame is at least 7 bytes long; the app parses it and never uses it. Presumably the length of the chunk now ready to read **(inferred)**.
- **`0x40`/`0x41`/`0x50`/`0x58`** (and the unused `0x44`/`0x54`): nothing after the error byte.
- **Any other op:** the app fails the request ("BLE parsing failed"). A frame shorter than 5 bytes would throw in the app, so the projector presumably never sends one **(inferred)**.

The app does not look at `seq` in a notification, nor check that `op` is the one it sent; it only uses `op` to pick the layout **(app)**.

**`seq`** is a counter in the executor, which lives as long as the app (`BleConnector` holds one static `BleControl`): it starts at 0, is never reset and grows by one for every step of a request, including the writes to and reads from `EE002C46`, which carry no counter (enabling notifications does not count) **(app)**. A frame carries the counter of its step, except that the `0x40`/`0x41`/`0x50`/`0x58` frames carry the one of the `EE002C46` write just before them. For a GET whose first step gets `n`: `0x50` carries `n`, `0x52` `n+2`, the body reads `n+3`, `n+5`, `n+7`, …; for a POST: `0x40` `n`, `0x41` `n+2`, `0x42` `n+4`, the body reads `n+5`, `n+7`, …; the next request continues after the last value used **(app)**. Since a second connection or a restarted app starts anywhere, the projector presumably ignores it or only echoes it **(inferred)**.

#### Sequence

Each step waits for the previous one; a write to the control point is done only when **both** its write response and its notification have arrived, in either order (`WebApiPassThrowPacketResult`) **(app)**. For `GET /v1/settings/time` with `n = 0`:

1. Enable notifications on `EE00270D` (CCCD `01 00`). The app does this at the start of every request **(app)**.
2. Write `EE002C46` ← `/v1/settings/time` (17 bytes).
3. Write `EE00270D` ← `00 00 00 50 00 11`; notification `[seq] 00 50 00`.
4. Write `EE00270D` ← `00 02 00 52`; notification `[seq] 00 52 00 [status:u16] [total:u32]`, for example `… 00 C8 00 00 00 2A` for HTTP 200 and 42 bytes.
5. If `total > 0`: write `EE00270D` ← `00 03 00 53`; notification `[seq] 00 53 00 [length:u16]`; then read `EE002C46`. The value read is the next chunk of the body. Subtract its length from what remains and, while something remains, repeat with `00 05 00 53`, `00 07 00 53`, … **(app)**.
6. The response is the HTTP status from step 4 and the chunks joined as text **(app)**.

A POST with a body (`POST /v1/settings/posterResume`, `{"posterResume":true}`, 25 and 21 bytes) adds the body before asking for the response:

1. Enable notifications on `EE00270D`.
2. Write `EE002C46` ← the path; write `EE00270D` ← `00 00 00 40 00 19 00 00 00 15`; notification `[seq] 00 40 00`.
3. Write `EE002C46` ← the body; write `EE00270D` ← `00 02 00 41 00 15`; notification `[seq] 00 41 00`.
4. Write `EE00270D` ← `00 04 00 42`; notification with the status and total length, as for `0x52`.
5. Body reads as for a GET, with `00 05 00 43`, `00 07 00 43`, ….

A DELETE is a GET with `0x58`/`0x5A`/`0x5B` **(app)**. No HTTP headers travel in either direction: only the method (the op), the path and the body **(app)**; the projector presumably treats the body as JSON itself **(inferred)**.

#### Chunks, re-polls and timeouts

- **Request side:** the path and the body are one GATT write each; the app never splits them and requests no larger MTU (`requestMtu` appears nowhere) **(app)**. Android sends values longer than the MTU as a long (prepared) write, but only for a write with response, so a path or body up to 512 bytes, the longest attribute value GATT allows, presumably fits **(inferred)**; whether longer bodies work is unknown.
- **Write type:** the app never sets one, so Android's default applies: without response when the characteristic's properties include write-without-response, with response otherwise **(inferred)**, from how Android's GATT client behaves, not from the app. On one projector `EE00270D` declared no write-without-response, so Android writes it with response too ([seen](#seen-on-a-projector)); `EE002C46`'s properties were not looked at.
- **Response side:** one GATT read of `EE002C46` per `0x43`/`0x53`/`0x5B` **(app)**. Android, BlueZ and CoreBluetooth all continue a long read on their own, so a chunk is whatever value the projector prepared, at most 512 bytes **(inferred)**. The app decodes each chunk as text separately (a UTF-8 character split between chunks would break), and an empty read before the total is reached would make it ask again until the request times out **(app)**.
- **Re-polls:** when the notification to `0x42`/`0x52`/`0x5A` carries an error, the app waits 1 s and sends the same frame again (same `seq`), whatever the error code, while it has waited less than 30 000 ms in all, so 30 re-sends; the 31st error fails the request **(app)**. This suggests that the projector answers with an error while the HTTP request is still running **(inferred)**; which code is unknown. An error on any other step fails the request at once **(app)**.
- **Timeouts:** there is no timeout per step, only the 30 s re-poll budget above (presumably the executor's `timeoutMilliSec = 30000`, which the compiler inlines, **inferred**); the caller waits up to 60 s for the whole request (`waitExecutionEnd(60000)`) **(app)**. A disconnection fails the request **(app)**.
- **One at a time:** every `Ifs` call, tunnel requests included, holds one shared lock (`BleConnector.g()`), so no command is sent while a tunnel request runs; a second tunnel request would also be refused ("WebApi is already executing") **(app)**.
- **The probe differs:** `tools/probes/web_tunnel.py` waits for each notification as long as is left of the 60 s, skips (and prints) a notification for another op than the one just sent, subscribes once per connection rather than per request, stops at an empty chunk, and always writes `EE002C46` with response (the control point follows its properties).

#### Results and errors

The listener gets the HTTP status and the body, but `BleWebApiPassThroughIfs` ignores the status and parses the body as the usual `RowAPIResponse` JSON (`{"status": …, "result": …}`) **(app)**. Paths are those of the Wi-Fi API, with a leading `/`, no host and possibly a query (`/v1/poster/groups?index=12`); the Wi-Fi setup calls use paths outside `/v1` (`/settings/setWifi`, `/settings/wpsPin`, `/settings/wpsCancel`) **(app)**. Its `setTime(long)` posts `{"time":N}`, with no `timezone`, to `/v1/settings/time` **(app)**.

#### Pairing and the encrypted link

The app sends tunnel requests only on the connection it uses for commands, which is bonded and encrypted ([Pairing](#pairing)), including during first setup, right after registering the phone **(app)**. The executor treats `0xA0` as access denied, like the command characteristic **(app)**. So the tunnel presumably needs the same bond and an encrypted link **(inferred)**; whether the projector then refuses at the GATT level (insufficient authentication) or with `0xA0` in a notification is unknown.

#### When the app uses it

Not only without Wi-Fi **(app)**:

- **As a fallback:** many settings try HTTP first and use the tunnel when that fails: the slideshow interval (`m/a.java`), the clock on start (`e0.v.g()`), the device name when it is changed (`z/f.java`), and during first setup the language and the time when the projector reports no usable Wi-Fi address yet (`oobe/f.java`). Some screens also skip HTTP when `r.a.d()` is false, that is unless the phone is connected to a Wi-Fi network (`e0.o.c()`) and the projector shows neither wireless HDMI (topology `48`) nor screen mirroring (app state Miracast): for example the initial mode (`a0/b.java`).
- **Always:** setting up Wi-Fi and WPS during first setup (`oobe/p.java`, `oobe/m.java`), reading the device name on its settings screen (`z/f.java`), reading the firmware package version (`e0.b.A()`, `/v1/settings/firmwareGetPackageVersion`) and starting a forced firmware update (`b0/c.java`). The timers have no tunnel route ([Timers](#timers)).

#### Seen on a projector

Firmware 3.1.15, 2026-11-24, from macOS with `tools/at_projector.py` (which uses `tools/probes/web_tunnel.py`), on the bonded, encrypted link it uses for commands; the projector on, showing Clock 1, topology `16`, reporting no address on a home network (an empty one in the first session, `192.168.8.100` in the second). Five GETs and two POSTs of `/v1/settings/time`, all answered:

- **GET:** exactly the sequence above. `00 00 00 50 00 11` → `00 00 00 50 00`; `00 02 00 52` → `00 02 00 52 00 00 C8 00 00 00 40` (11 bytes: HTTP 200, 64 bytes); `00 03 00 53` → `00 03 00 53 00 00 40` (7 bytes: 64, the length of the chunk); one read of `EE002C46` then gave the whole 64-byte body. Each notification to a GET came within 0.03 s, most within 0.003 s; no error, no re-poll.
- **POST** `{"time":N}` (19 bytes, the app's tunnel body, without `timezone`): `00 05 00 40 00 11 00 00 00 13` → `00 05 00 40 00`; the body, then `00 07 00 41 00 13` → `00 07 00 41 00`; `00 09 00 42` → status 200, 64 bytes, after about 1 s (0.96 s and 0.99 s), with no error notification before it; then the body as for a GET. The `0x40` notification arrived before the write's response, the other way round from the rest. Clock 1 jumped 90 minutes back at once (11:36 to 10:06, as its owner read it) and came back when the time was posted again; the clock then read within 1 s of the time sent.
- **The reply body** is `{"code": 0, "result": {"timezone": "GMT+0", "time": 1795562456}}`, both for a GET and as the answer to a POST (with the time just set): a `code` of `0` and no `status`, not the `RowAPIResponse` shape (`"status": "200"`) the Wi-Fi replies are read as. Whether replies over Wi-Fi look the same was not seen.
- **`seq`:** every notification carried the `seq` of the frame it answered; the session counted as the app does (0, 2, 3, then 5, 7, 9, 10, … on one connection). Whether the projector checks it was not tried.
- **The time** read 23:20 UTC as Unix seconds when the computer's clock read 23:20, in a zone that was UTC+0 that day (GMT), so whether the projector takes the seconds as UTC or as wall-clock time cannot be told from this; `timezone` read `GMT+0`. Clock 1 showed the time as a 12-hour clock (11:20).
- **Writes:** both characteristics were written with response, `EE00270D` because the session found no write-without-response among its properties.

#### Open questions for a real projector

1. Does `GET /v1/settings/time` work as above? Yes, with the layouts above ([seen](#seen-on-a-projector)).
2. Does the tunnel need the bond and an encrypted link, and how does it refuse otherwise? Only an encrypted link was tried.
3. Does the projector care about the value of `seq`? It echoes it.
4. Does `0x52`/`0x42` report errors while a slower request runs, with which code, and for how long? None did: a POST's `0x42` was simply answered after about 1 s.
5. How long is each chunk once a body is longer than 64 bytes? The `0x53` notification's u16 was the chunk's length.
6. Do paths or bodies longer than the MTU or 512 bytes go through?
7. Does it work in Networked Standby and while wireless HDMI is shown? It works on a projector reporting no home-network address (an empty one, or `192.168.8.100`).
8. Does `POST /v1/settings/time` through the tunnel accept the `timezone` field the Wi-Fi request sends? Only the app's tunnel body, without it, was tried.

## Known quirks

- The projector's internal link between the Bluetooth chip and the main board can get stuck, after which every command times out (`0xC0`) and the official app stops working too. The recessed RESET button next to the DC IN jack recovers it. Whether [Reboot](#reboot) gets through then is not known: GetPower, which also goes to the system controller, times out too, which is how the integration tells this state (five polls in a row). Its repair flow tries Reboot only after a fresh poll still times out that way (not while a start-up is under way), waits up to two minutes for a poll that reads the power (one that keeps the last power while starting up does not count), and logs a warning with whether Reboot was answered or timed out and how it ended.
- Right after switching power, reads can fail (`0x03`/`0xC0`, each `0xC0` after about 2 s; GetPower once `0xE0`): GetPower kept answering `0xC0` until about +7.4 s after switching on from a long standby; poll again a few seconds later. After more than about 30 minutes of standby, main-board reads answer `0xE0` for about 45 s ([Start-up of the main board](#start-up-of-the-main-board)).
- Many stacks keep a single connection to the projector; disconnect when idle so the smartphone app can still connect.
- Once (firmware 3.1.15, 2026-11-24), during a run of SetWeather codes sent now and then with `tools/lspx_cli.py`, roughly 20 minutes or more after it was switched on, the projector showed "Display will turn off in a few moments" and went to standby by itself (GetPower read standby, `00`, afterwards). Nothing sent asked for standby. Its auto on/standby flags (`0x0811`) read `00 01 01` at the start and end of both sessions that day: switch to standby on inactivity (second byte) and by the timer (the third byte, which the app never sets, see [Timers](#timers)); the timers over HTTP were not read. The cause is not known.
