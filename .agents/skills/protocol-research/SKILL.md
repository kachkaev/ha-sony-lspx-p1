---
name: protocol-research
description: Find out how the official app talks to the projector (opcodes, payload formats, call sequences) and try commands on a real projector. Use before adding a projector feature or when a command misbehaves.
---

# Protocol research

`docs/protocol.md` has everything confirmed so far; read it first.

## The decompiled app

`.notes/android-app/` in the main checkout `~/-/projects/kachkaev/oss/ha-sony-lspx-p1` (git-ignored, so git worktrees do not have it; read it from there; never commit or quote large parts): `lspx001-3.2.11.apk` and its jadx output in `jadx/`. If it is missing, ask the user; decompile with `jadx -d jadx lspx001-3.2.11.apk`. `.notes/sony-help-guide.txt` is Sony's online help guide as text.

Where to look, under `jadx/sources/jp/co/sony/ts/`:

- `lspxsdk/ble/characteristic/cp/UartCommandList.java`: command opcodes. `ManageBondOperationList.java`: bonding operations.
- `lspxsdk/ble/characteristic/CharacteristicInfoCache.java`: each command's value format: `17` u8, `18` u16 (big-endian), `0` string, `2` byte array, `APP_COMMAND_NO_VAL` no payload; other numbers map to `Read*` classes in `cp/`.
- `lspxsdk/ble/ifs/Ble*Ifs.java`: the calls the app makes; `lspxsdk/ble/model/`: enums and value layouts.
- What the UI actually does (order of calls, waits) is in obfuscated classes (`jp/co/sony/ts/lspxp1/b.java`, `c.java`, `e/f.java`). Search for the `Ifs` method name or its `e.f.e` message constant. Labels are in `jadx/resources/res/values/strings.xml`.
- The app also tunnels an HTTP API over Bluetooth (`BleWebApiPassThroughIfs`, `lspxsdk/web/Row*API.java`); not reconstructed yet (issue #1).

## Trying it on the projector

`tools/lspx_cli.py` uses the integration's own client. It pairs as a separate controller ("Home Assistant (dev)"); remove that entry under Smartphones added afterwards. Home Assistant may hold the connection; the CLI then waits or fails, so try again after a few seconds. The user has to look at the projector, so ask them to watch and report.

Never send factory reset, initialise, demo mode or Wi-Fi setup commands.
