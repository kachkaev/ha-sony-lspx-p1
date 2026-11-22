---
name: screenshots
description: Retake the README screenshots (dashboard card layouts, card popover, device page) from a demo Home Assistant with a simulated projector. Use whenever the dashboard card (custom_components/sony_lspx_p1/frontend/), entity names or icons change, or when asked to update the README images.
---

# README screenshots

Follow `tools/screenshots/README.md`. In short:

1. Once, install the demo's packages with the constraints of the Home Assistant release in `.venv` (an unpinned frontend newer than that release breaks the login page):
   `uv pip install --python .venv -r requirements_demo.txt -c "https://raw.githubusercontent.com/home-assistant/core/$(.venv/bin/python -c 'from homeassistant.const import __version__; print(__version__)')/homeassistant/package_constraints.txt"` (without these packages the dashboard stays on "Loading").
   Then `.venv/bin/python tools/screenshots/demo_ha.py --reset` (`--slow`, `--unreachable` for pending and unavailable states): a demo Home Assistant on http://127.0.0.1:8123 with an in-memory projector (from `tests/fake_projector.py`) and a dashboard with the card in all three layouts. Run it in a terminal tab the user can see; it keeps running.
2. `.venv/bin/python tools/screenshots/capture.py`: headless Chromium, fixed viewport, 2x, light theme, English. It writes `docs/images/card-{compact,standard,full,popover}.png` and `docs/images/device-page.png` (`--out DIR` for elsewhere).
3. Look at every image before committing (icons loaded, nothing half-rendered), then stop the demo.

**Pick a port.** Other agents may be running their own demo (and the user their own Home Assistant on 8123). If you are not the only one, start the demo with `--port` and a port nobody else uses (`demo_ha.py` stops if it is taken). `capture.py` needs no option: it shoots the demo of its own checkout, on the port that demo was last started on, and Chrome picks a free debugging port itself. Never set the port with an `http:` section in the demo's `configuration.yaml`: Home Assistant would take it as a trial and cover the dashboard with a "Confirm new HTTP server configuration" dialog (see "Several demos at once" in the README).

At phone width the sections view overlaps stacked cards, so `capture.py` shoots at desktop widths only; for a phone-width shot of one card, hide the other sections first.

Crop rules are constants at the top of `capture.py` (`VIEWPORT`, `DIALOG_MARGIN`, `DEVICE_VIEWPORT`). Card images are cut along the card's border with transparent rounded corners (`round_corners()`), no dashboard background; the popover and device page keep their surroundings. Change them there and update the "Crop rules" section of `tools/screenshots/README.md`; never hand-crop images. The demo state (80% on battery, Custom 1 picture mode and so on) is in `simulate_projector()` in `demo_ha.py`.

The source differs per shot on purpose: `capture.py` selects HDMI before the `compact` shot, Clock 2 before `standard` and Window before `full` and the popover (`SOURCES`), so together the images show the source chips at the start, in the middle and at the end of their scroll. Keep that spread when changing the shots or the source list. Check that each selected chip is fully visible and that every faded edge cuts through a chip.

`demo_ha.py --reset` deletes `tools/screenshots/.demo/`, so keep any experimental captures somewhere else.

The README refers to the images by absolute raw.githubusercontent.com URLs on `main`, so that they also show in Home Assistant's Marketplace. New images appear there only once they are pushed.
