---
name: screenshots
description: Retake the README screenshots (dashboard card layouts, card popover, device page) from a demo Home Assistant with a simulated projector. Use whenever the dashboard card (custom_components/sony_lspx_p1/frontend/), entity names or icons change, or when asked to update the README images.
---

# README screenshots

Follow `tools/screenshots/README.md`. In short:

1. `.venv/bin/python tools/screenshots/demo_ha.py --reset`: a demo Home Assistant on http://127.0.0.1:8123 with an in-memory projector (from `tests/fake_projector.py`) and a dashboard with the card in all three layouts. Run it in a terminal tab the user can see; it keeps running. Port 8123 must be free.
2. `.venv/bin/python tools/screenshots/capture.py`: headless Chromium, fixed viewport, 2x, light theme, English. It writes `docs/images/card-{compact,standard,full,popover}.png` and `docs/images/device-page.png`.
3. Look at every image before committing (icons loaded, nothing half-rendered), then stop the demo.

Crop rules are constants at the top of `capture.py` (`VIEWPORT`, `CARD_MARGIN`, `DIALOG_MARGIN`, `DEVICE_VIEWPORT`). Change them there and update the "Crop rules" section of `tools/screenshots/README.md`; never hand-crop images. The demo state (80% on battery, Custom 1 picture mode and so on) is in `simulate_projector()` in `demo_ha.py`.

The source differs per shot on purpose: `capture.py` selects HDMI before the `compact` shot, Clock 2 before `standard` and Window before `full` and the popover (`SOURCES`), so together the images show the source chips at the start, in the middle and at the end of their scroll. Keep that spread when changing the shots or the source list. Check that each selected chip is fully visible and that every faded edge cuts through a chip.

`demo_ha.py --reset` deletes `tools/screenshots/.demo/`, so keep any experimental captures somewhere else.

The README refers to the images by absolute raw.githubusercontent.com URLs on `main`, so that they also show in Home Assistant's Marketplace. New images appear there only once they are pushed.
