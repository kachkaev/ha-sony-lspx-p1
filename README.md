# Sony LSPX-P1 for Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/kachkaev/ha-sony-lspx-p1/main/docs/images/readme-header.png" alt="Home Assistant logo and Sony LSPX-P1 projector" height="112">
</p>

Control the [Sony LSPX-P1 portable ultra short throw projector](https://helpguide.sony.net/projector/lspxp1/v1/en/index.html) from Home Assistant over Bluetooth, without the smartphone app and without putting the projector on Wi-Fi. Everything on the app's control panel (the screen with the sliders) is available, plus picture settings and battery status, both as regular entities and in a [dashboard card](#dashboard-card).

| Device page | Dashboard card |
| --- | --- |
| <img src="https://raw.githubusercontent.com/kachkaev/ha-sony-lspx-p1/main/docs/images/device-page.png" alt="The projector's device page in Home Assistant" width="400"> | <img src="https://raw.githubusercontent.com/kachkaev/ha-sony-lspx-p1/main/docs/images/card-standard.png" alt="The dashboard card, standard layout" width="300"> |

The entities:

| Entity | What it does |
| --- | --- |
| Media player | Power (on ↔ standby), volume, current source (Poster, HDMI, …) |
| Brightness | Projection brightness; moving it switches Auto brightness off |
| Focus | Manual focus; moving it switches Auto focus off |
| Keystone | Keystone correction |
| Screen rotation | Normal, upside down, mirrored; choosing one switches Auto rotation off |
| Auto brightness, Auto focus, Auto rotation | The app's "Auto" checkboxes |
| Picture mode | Preset 1–3, Custom 1–2 |
| Picture saturation, brightness, contrast, sharpness, white balance (R, G, B) | Tuning for the Custom picture modes (unavailable in Preset modes) |
| Reset keystone, Reset picture settings | The app's Reset buttons |
| xLOUD | Sony's loudness enhancement |
| Battery, Charge state | Battery level and whether it is charging |
| Restrict adding smartphones | The app's setting of the same name: when on, a new phone has to enter a PIN shown by the projector. Phones already added and Home Assistant keep working. |
| Smartphones added | How many controllers are registered (Home Assistant included), with their names and whether each is allowed, as an attribute |

## Requirements

- Home Assistant 2025.3 or newer with the [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/) working.
- A Bluetooth adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) (with `active: true`) within range of the projector.
- The projector itself (not the wireless unit) must be in range. Projector firmware 3.1.15 has been tested.

## Installation

### Marketplace

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=kachkaev&repository=ha-sony-lspx-p1&category=integration)

1. Click the button above, or go to **Settings → Marketplace → Add from link** and enter `https://github.com/kachkaev/ha-sony-lspx-p1`.
2. Install **Sony LSPX-P1 Projector**.

On Home Assistant 2026.10 and older, use HACS (the Marketplace's predecessor) instead: menu → **Custom repositories** → add the URL above as an **Integration**, install, then restart Home Assistant.

### Manual

Copy `custom_components/sony_lspx_p1` into your Home Assistant `config/custom_components` folder and restart.

## Setup

1. Switch the projector on (projecting or in standby).
2. In the smartphone app, make sure **Settings → Network → Restrict adding smartphones** is off.
3. Home Assistant discovers the projector automatically: **Settings → Devices & services** shows it as discovered. If not, add the integration manually:

   [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=sony_lspx_p1)

4. Confirm the pairing step. Home Assistant registers itself as a controller called "Home Assistant", the same way a phone does; it appears in the app under **Smartphones added** (the projector keeps up to 8).

If you later remove "Home Assistant" from the list (or reset the projector), Home Assistant asks you to pair again.

The integration is available in English, German, French, Spanish, Italian, Dutch, Polish, Portuguese (Brazil), Swedish, Danish, Norwegian, Czech, Ukrainian, Russian, Chinese (Simplified) and Japanese.

## Dashboard card

The integration comes with a card laid out like the app's control panel. It uses your Home Assistant theme, so it works in light and dark mode.
Edit a dashboard, **Add card**, and pick **Sony LSPX-P1 Projector** (no extra resources to add). Or in YAML:

```yaml
type: custom:sony-lspx-p1-card
entity: media_player.lspx_p1_6320 # your projector's media player
layout: standard # compact, standard or full
```

| Layout | Shows |
| --- | --- |
| `compact` | One row: power and volume |
| `standard` | The app's control panel: volume, brightness, focus, keystone and screen rotation, each with its Auto checkbox or Reset button |
| `full` | Everything in `standard`, plus picture mode, the Custom mode sliders (saturation, brightness, contrast, sharpness, white balance), xLOUD and the picture Reset |

| `compact` | `standard` | `full` |
| --- | --- | --- |
| <img src="https://raw.githubusercontent.com/kachkaev/ha-sony-lspx-p1/main/docs/images/card-compact.png" alt="Compact layout" width="260"> | <img src="https://raw.githubusercontent.com/kachkaev/ha-sony-lspx-p1/main/docs/images/card-standard.png" alt="Standard layout" width="260"> | <img src="https://raw.githubusercontent.com/kachkaev/ha-sony-lspx-p1/main/docs/images/card-full.png" alt="Full layout" width="260"> |

Moving a slider whose Auto checkbox is ticked unticks it, as in the app.

Tap the projector's icon or name to see everything: from the `compact` and `standard` layouts this opens the full controls in a dialog, with buttons for the projector's history and settings; from the `full` layout it opens Home Assistant's own dialog for the projector. In the `compact` layout the battery shows as an icon only (hover it for the percentage).

<img src="https://raw.githubusercontent.com/kachkaev/ha-sony-lspx-p1/main/docs/images/card-popover.png" alt="The dialog with the full controls" width="320">

## How it works

The integration talks to the projector over Bluetooth LE using the same protocol as the official app, documented in [docs/protocol.md](docs/protocol.md).
It polls once a minute (and a few extra times right after switching power) and disconnects a couple of seconds after each poll, or about 15 seconds after you change something, so the smartphone app and Bluetooth proxy slots stay free.

## Limitations

- Switching the source (Poster, HDMI, …) is not supported yet; the media player only shows the current one.
- Fully switching the projector off (long press of its power button) cannot be undone remotely; Home Assistant switches between on and standby.
- Tested with Home Assistant OS on a Raspberry Pi (built-in Bluetooth adapter) and with Home Assistant on macOS. ESPHome Bluetooth proxies (a recent ESPHome version with `active: true`) support the pairing this integration needs, but pairing through a proxy has not been verified yet; reports are welcome.
- Pairing can take up to a minute. On Linux, the integration briefly registers its own Bluetooth pairing agent while pairing (Home Assistant OS does not run one), approving pairing with the projector only.
- The pairing is stored by the Bluetooth adapter or proxy that made it. If Home Assistant later reaches the projector through a different adapter or proxy, the projector rejects it and Home Assistant asks you to pair again, which adds another "Home Assistant" entry under **Smartphones added**. Remove stale entries there.

## Troubleshooting

- **Entities unavailable with "The projector did not respond"**: the projector's internal link can get stuck (the official app then stops responding too). Press the recessed **RESET** button next to the DC IN jack; settings and pairings survive.
- **Pairing fails**: check that "Restrict adding smartphones" is off, that the projector is switched on, and that it is close to the Bluetooth adapter or proxy.
- Diagnostics are available from the device page (⋮ → Download diagnostics).

<!-- ha-marketplace:exclude-start -->

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

`tools/probes/` holds the scripts used to reverse-engineer the protocol and `tools/control_panel/` a small web UI for testing.
`tools/brand/` has the sources of the brand icon and the README header (a 3D model rendered with three.js, plus a line-art alternative); see its README.
`tools/screenshots/` runs Home Assistant with a simulated projector and takes the README screenshots; see its README.

<!-- ha-marketplace:exclude-end -->

## Disclaimer

This project is not affiliated with or endorsed by Sony. The protocol was worked out for interoperability with hardware the author owns. Use at your own risk.

## Licence

[BSD 3-Clause](LICENSE.md)
