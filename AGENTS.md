# Notes for agents

Home Assistant integration for the Sony LSPX-P1 projector over Bluetooth LE, plus a dashboard card. Setup and commands are in the README's Development section; skills in `.agents/skills` cover translations, screenshots, releases and protocol research.

## Pitfalls

- **One connection at a time.** The projector serves one Bluetooth client; the integration disconnects soon after each poll so the phone app and proxies stay usable. Keep it that way.
- **Most commands are only known from the decompiled app.** Mark anything not tried on a real projector as such in release notes, and make a failing read affect only its own entities (per-field errors in `read_state`, never a failed poll).
- **Replies can be slow.** Source switches answer only when done (up to 60 s, `SWITCH_REPLY_TIMEOUT`); other commands 8 s. A timeout drops the connection.
- **Setup must not depend on the projector.** It is often off; entities go unavailable, they must not disappear.
- **Every user-facing string exists in 16 languages**: `translations/` (generated, see the translations skill) and the card's `WORDS` table.
- **The card** (`frontend/sony-lspx-p1-card.js`) is plain JS with no build step. It registers itself only after Home Assistant's scoped custom-element registry exists (`registerCard`). Every control change goes through `_act()`: optimistic value at once, sent through `LANES` (one call per lane, latest value wins, power first) so related settings never race; try changes with `tools/screenshots/demo_ha.py --slow`. Changing it means retaking the README screenshots.
- **Git:** commit straight to `main`, end messages with the `Co-Authored-By` trailer, release with the release skill.
- **Never commit** anything from the official app (APK, decompiled code); it lives in the git-ignored `.notes/`.
