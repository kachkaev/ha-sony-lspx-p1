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
- Whether the UI ever sends a command: grep for its `e.f.e` message constant (e.g. `POSTER_WEATHER_SET`); some exist only in the SDK.
- When the app counts a write as done: `controller/BleControl.writeSync` and `BleCpAdapter.onWritten`/`onNotify` (most commands wait for the ACK/NAK notification, up to 60 s).
- What the UI actually does (order of calls, waits) is in obfuscated classes (`jp/co/sony/ts/lspxp1/b.java`, `c.java`, `e/f.java`; the Smartphones added screens are `z/e.java` and `z/d.java`; other screens are in short top-level packages too). Search for the `Ifs` method name or its `e.f.e` message constant. Labels are in `jadx/resources/res/values/strings.xml`.
- The HTTP API (`lspxsdk/web/Row*API.java`): what the app sends over Wi-Fi is in docs/protocol.md ("HTTP API over Wi-Fi"); the integration does not use Wi-Fi. The app also tunnels it over Bluetooth (`BleWebApiPassThroughIfs`, `BleWebApiExecutor`); docs/protocol.md ("Bluetooth tunnel") reconstructs it from the app, and `tools/probes/web_tunnel.py get|post <path> [json]` tries it with every frame printed (issue #1). jadx's Java can merge `switch` branches (it did in `BleWebApiJob.parseCPNotify`): when a layout matters, check the raw instructions with `jadx --single-class <class> -m fallback -d <scratch dir> lspx001-3.2.11.apk`. `tools/lspx_cli.py listen` prints the projector's change notifications. HTTP features (timers, poster settings, time) are wrapped by `DeviceController` and `PosterCoordinator`; screens choose HTTP or the tunnel with `r.a.d()`, and by firmware with `e0.b.q()`.

## Trying it on the projector

`tools/lspx_cli.py` uses the integration's own client. It pairs as a separate controller ("Home Assistant (dev)"); remove that entry under Smartphones added afterwards. Home Assistant may hold the connection; the CLI then waits or fails, so try again after a few seconds. The user has to look at the projector, so ask them to watch and report.

Checks that need someone watching belong in `tools/at_projector.py` (README, "At the projector"): a guided session that asks what they see, records frames and answers to a git-ignored folder with a redacted `shareable.md`, and puts back what each step changes. Steps that are hard to undo (smartphone detection, the computer's own entry) have an `opt_in` text and run only on a `y`; the person changes "Smartphones added" in Home Assistant or Sony's app, never the session. A new check there is a `step_<key>` method plus an entry in `STEPS`; anything it sends must be in `ALLOWED_COMMANDS` (or the tunnel allow-list; the bonding service is read-only there, and the session never pairs), and every change is noted with `change()` before it is made. Test it in `tests/test_at_projector.py`.

Never send factory reset, initialise, demo mode or Wi-Fi setup commands.
