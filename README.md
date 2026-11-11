# Sony LSPX-P1 for Home Assistant

Control the [Sony LSPX-P1 portable ultra short throw projector](https://helpguide.sony.net/projector/lspxp1/v1/en/index.html) from Home Assistant over Bluetooth, without the smartphone app and without putting the projector on Wi-Fi.

Everything in the app's control panel is available:

| Entity | What it does |
| --- | --- |
| Media player | Power (on ↔ standby), volume, current source (Poster, HDMI, …) |
| Brightness | Projection brightness; moving it switches Auto brightness off |
| Focus | Manual focus; moving it switches Auto focus off |
| Keystone | Keystone correction |
| Screen rotation | Normal, upside down, mirrored; choosing one switches Auto rotation off |
| Auto brightness, Auto focus, Auto rotation | The app's "Auto" checkboxes |
| Picture mode | Preset 1–3, Custom 1–2 |
| Picture brightness, contrast, saturation, sharpness | Tuning for the Custom picture modes (unavailable in Preset modes) |
| xLOUD | Sony's loudness enhancement |
| Battery, Charge state | Battery level and whether it is charging |

## Requirements

- Home Assistant 2025.1 or newer with the [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/) working.
- A Bluetooth adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) (with `active: true`) within range of the projector.
- The projector itself (not the wireless unit) must be in range. Projector firmware 3.1.15 has been tested.

## Installation

### HACS

1. In HACS, open the menu → **Custom repositories** and add `https://github.com/kachkaev/ha-sony-lspx-p1` as an **Integration**.
2. Install **Sony LSPX-P1 Projector** and restart Home Assistant.

### Manual

Copy `custom_components/sony_lspx_p1` into your Home Assistant `config/custom_components` folder and restart.

## Setup

1. Switch the projector on (projecting or in standby).
2. In the smartphone app, make sure **Settings → Network → Restrict adding smartphones** is off.
3. Home Assistant discovers the projector automatically: **Settings → Devices & services** shows it as discovered. If not, add the **Sony LSPX-P1 Projector** integration manually.
4. Confirm the pairing step. Home Assistant registers itself as a controller called "Home Assistant", the same way a phone does; it appears in the app under **Smartphones added**.

If you later remove "Home Assistant" from the list (or reset the projector), Home Assistant asks you to pair again.

## How it works

The integration talks to the projector over Bluetooth LE using the same protocol as the official app, documented in [docs/protocol.md](docs/protocol.md).
It polls once a minute and a few extra times right after switching power, and disconnects when idle so the smartphone app keeps working.

## Troubleshooting

- **Entities unavailable with "The projector did not respond"**: the projector's internal link can get stuck (the official app then stops responding too). Press the recessed **RESET** button next to the DC IN jack; settings and pairings survive.
- **Pairing fails**: check that "Restrict adding smartphones" is off, that the projector is switched on, and that it is close to the Bluetooth adapter or proxy.
- Diagnostics are available from the device page (⋮ → Download diagnostics).

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`tools/lspx_cli.py` drives a projector from the command line with the integration's own client (it needs a terminal that is allowed to use Bluetooth):

```bash
.venv/bin/python tools/lspx_cli.py pair --name "My laptop"
.venv/bin/python tools/lspx_cli.py state
.venv/bin/python tools/lspx_cli.py set brightness 70
.venv/bin/python tools/lspx_cli.py power off --watch 10
```

`tools/probes/` holds the scripts used to reverse-engineer the protocol, and `tools/control_panel/` a small web UI for testing.

## Disclaimer

This project is not affiliated with or endorsed by Sony. The protocol was worked out for interoperability with hardware the author owns. Use at your own risk.

## Licence

MIT
