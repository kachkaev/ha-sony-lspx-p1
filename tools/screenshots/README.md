# README screenshots

The screenshots in `docs/images/` (`card-*.png`, `device-page.png`) come from a demo Home Assistant with a simulated projector. Retake them whenever the card or the entities change, so the README matches the current UI. `readme-header.png` is not a screenshot; it comes from `tools/brand/`.

## Taking them

1. Install the demo's extra packages once, with the constraints file of the Home Assistant release in `.venv` (`package_constraints.txt`, shipped inside it) so the frontend matches it (an unpinned frontend can be newer than that release expects, and then the login page fails):

   ```bash
   uv pip install --python .venv -r requirements_demo.txt \
     -c "$(.venv/bin/python -c 'import homeassistant, pathlib; print(pathlib.Path(homeassistant.__file__).parent / "package_constraints.txt")')"
   ```

   Then start the demo Home Assistant:

   ```bash
   .venv/bin/python tools/screenshots/demo_ha.py --reset              # http://127.0.0.1:8123
   .venv/bin/python tools/screenshots/demo_ha.py --reset --port 8130  # when 8123 is taken
   ```

   `--reset` deletes the git-ignored `tools/screenshots/.demo/` and starts from a clean instance. When the terminal prints the URL, wait until `/projector/projector` there shows the three cards (about 20 s). No login is needed from this computer. If the port is taken (another Home Assistant, or another checkout's demo), the script stops and asks for another `--port`.

2. In another terminal:

   ```bash
   .venv/bin/python tools/screenshots/capture.py
   ```

   This uses headless Chromium: Playwright's `chrome-headless-shell`, Google Chrome, or the browser in `CHROME`. It overwrites `docs/images/card-compact.png`, `card-standard.png`, `card-full.png`, `card-popover.png` and `device-page.png` (`--out DIR` writes them elsewhere, for experiments).

3. Look at every image before committing: icons drawn, no half-loaded content, values as listed below. Then stop the demo with Ctrl+C.

To try the card's pending state (a control keeps your choice and pulses until the projector confirms it), start the demo with `--slow`: every change then takes 2.5 seconds to confirm, and leaving HDMI about 11. Do not take the README screenshots in this mode.

### Several demos at once

Each checkout (or worktree) has its own `.demo/`, so demos from several checkouts can run side by side; only the ports must differ. Give each demo its own `--port`. `capture.py` shoots the demo of its own checkout: it reads the port that checkout's demo was last started on (from `.demo/.storage/http`), so it needs no option and does not shoot another checkout's demo by mistake; `--port` or `--base URL` point it elsewhere. Chrome picks a free debugging port by itself (`--debug-port` fixes one).

The demo writes the port into Home Assistant's own HTTP settings (`.demo/.storage/http`) on every start, as the confirmed configuration, and marks the YAML migration done. Recent Home Assistant releases (2026.10 here) keep these settings in storage and migrate an `http:` section of `configuration.yaml` only once, as a trial: the frontend shows a "Confirm new HTTP server configuration" dialog over the dashboard and, unconfirmed, Home Assistant restarts on the previous port about five minutes later. So the demo's `configuration.yaml` has no `http:` section; don't add one (with the migration marked done it would be ignored and raise a repair).

## What the demo shows

`demo_ha.py` points the integration at `tests/fake_projector.py`, which answers with the real wire protocol, so every entity behaves as it would with a projector. The state is chosen to show as much as possible:

- **Power:** on.
- **Source:** `capture.py` switches it before each shot (`SOURCES`): HDMI for `compact`, Clock 2 for `standard`, Window for `full` and the popover. One projector feeds every card, so this is the only way to show the source chips in all three scroll positions: at the start (fade on the right), in the middle (fades on both sides) and at the end (fade on the left). Keep the three positions if you change the list.
- **Battery:** 80% and discharging, so the battery icon shows a level.
- **Main controls:** volume 40, brightness 70 with Auto off, focus with Auto on (to show a dimmed slider), keystone 0, screen rotation normal with Auto on.
- **Picture:** mode Custom 1 (to show the picture sliders), white balance 100/96/92, xLOUD on.

The device is called "LSPX-P1" with the made-up address AA:BB:CC:DD:EE:FF, so no real device name or address appears. To change the state, edit `simulate_projector()` in `demo_ha.py`.

## Crop rules

The rules live as constants at the top of `capture.py`; change them there, not by hand-cropping.

- **English, light theme.** Light is the Home Assistant default (`--scheme dark` exists for experiments).
- **2x pixel density.** Every file is twice its CSS size, so it stays sharp on high-density screens. The README sets display widths with `width=`.
- **Cards:** each card is cut exactly along its border, and the corners outside its rounded border are transparent (the radius is read from the card). No dashboard background is kept, so the images line up at the top of the README tables and look right on GitHub's light and dark themes. The viewport is 1440 × 1100 CSS px (`VIEWPORT`), which gives three dashboard columns of about 350 px card width, the usual width on a desktop.
- **Popover:** the open dialog from the standard card, plus 24 px of the dimmed page around it (`DIALOG_MARGIN`). If it is taller than the viewport, it is cut at the viewport height.
- **Device page:** the whole page right of the sidebar, including its top bar, at a 1240 px wide viewport (`DEVICE_VIEWPORT`). That gives two columns of cards with no empty space on the right.

At phone width the sections view overlaps the stacked cards, so the demo dashboard cannot be shot as a whole there: `capture.py` keeps to desktop widths. For a phone-width shot of one card, hide the other cards' sections first.

If Home Assistant's layout changes enough that these no longer produce tidy images (for example, the card no longer fits three columns), adjust the constants and describe the new rule here.
