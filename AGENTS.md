# Notes for agents

Home Assistant integration for the Sony LSPX-P1 projector over Bluetooth LE, plus a dashboard card. Setup and commands are in the README's Development section; skills in `.agents/skills` cover translations, screenshots, releases and protocol research.

## Pitfalls

- **One connection at a time.** The projector serves one Bluetooth client; the integration disconnects soon after each poll so the phone app and proxies stay usable. Keep it that way.
- **Most commands are only known from the decompiled app.** Mark anything not tried on a real projector as such in release notes, and make a failing read affect only its own entities (per-field errors in `read_state`, never a failed poll).
- **Replies can be slow.** Source switches answer only when done (up to 60 s, `SWITCH_REPLY_TIMEOUT`); other commands 8 s. A timeout drops the connection.
- **Setup must not depend on the projector.** It is often off; entities go unavailable, they must not disappear. Every entity goes unavailable while polls fail, so a feature meant to recover the projector needs to stay available (see the Restart button's `always_available`).
- **Entry update listeners also fire for discovery.** Home Assistant updates the config entry when another Bluetooth adapter or proxy discovers the projector (`discovery_keys`), so the options listener in `async_setup_entry` reloads only when the options changed; `OptionsFlowWithReload` is newer than the minimum Home Assistant version in `hacs.json`.
- **Every user-facing string exists in 16 languages**: `translations/` (generated, see the translations skill) and the card's `WORDS` table.
- **The card** (`frontend/sony-lspx-p1-card.js`) is plain JS with no build step. It registers itself only after Home Assistant's scoped custom-element registry exists (`registerCard`). Every control change goes through `_act()`: optimistic value at once, sent through `LANES` (one call per lane, latest value wins, power first) so related settings never race; try changes with `tools/screenshots/demo_ha.py --slow`. Changing it means retaking the README screenshots.
- **Card tests** (`tests/test_card_ui.py`, part of `pytest`) drive the real card in headless Chrome on `tests/card/harness.html` (fake `hass`; fake `setTimeout`, so time passes only with `T.tick(ms)` and page code must not wait on timers). They skip locally without Chrome (set `CHROME=`) but fail in CI. Tests that need no Home Assistant override the autouse `auto_enable_custom_integrations` fixture.
- **Checks** are ruff, `.venv/bin/mypy` (strict, integration only; `platform = "linux"` so BlueZ code is checked on macOS too) and `pytest --cov` (above 95% in every module, as the quality scale asks; keep `quality_scale.yaml` honest). The coordinator uses `always_update=False`: a test poll that returns the same data notifies no listeners, so change something first.
- **Typing:** recent Home Assistant types form and service schemas as `probatio.Schema`; pass voluptuous schemas with a commented `type: ignore[arg-type]` (see `config_flow.py`) so older versions keep working.
- **Tests and time:** freezegun's `freezer` freezes the event loop's clock too, so the fake projector's replies hang; patch the module's `dt_util` instead (see `tests/test_clock.py`).
- **Entities disabled by default** (the settings over Wi-Fi): there is no fixture to enable them in tests; patch `LSPXWebEntity._attr_entity_registry_enabled_default` (see `tests/test_web_settings.py`).
- **Git:** unless you were given a branch or worktree, commit straight to `main`; end messages with the `Co-Authored-By` trailer; release with the release skill.
- **Never commit** anything from the official app (APK, decompiled code); it lives in `.notes/`, which is excluded in the main checkout's `.git/info/exclude` (not `.gitignore`).
