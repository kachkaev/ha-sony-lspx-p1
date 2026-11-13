---
name: release
description: Release a new version of the integration (version bump, checks, push, GitHub release with notes). Use whenever a change is ready to ship to users, or when asked to release, publish or tag a version.
---

# Releasing

Releases are GitHub releases on `main`; Home Assistant's Marketplace picks them up from there and shows the release notes to people when they update.

## Version

Semantic versioning, from the user's point of view:

- **Patch** (`1.3.0` → `1.3.1`): fixes only.
- **Minor** (`1.3.1` → `1.4.0`): new entities, card features or other additions; existing setups keep working.
- **Major** (`1.x` → `2.0.0`): anything that makes people change something, such as renamed or removed entities, or a change to required setup.

Set the same version in `custom_components/sony_lspx_p1/manifest.json` and `pyproject.toml`. The dashboard card is loaded with `?v=<version>`, so a new version also makes browsers fetch the new card.

## Steps

1. `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest -q`
2. If the card, entity names or icons changed, retake the README screenshots first (`.agents/skills/screenshots`).
3. Commit to `main` (the message says what changed and ends with `; <version>` in the title), push, and wait for the Tests and Validate workflows to pass: `gh run list --commit $(git rev-parse HEAD)`.
4. `gh release create v<version> --target main --title v<version> --notes-file <notes>`

## Release notes

Write for people updating their Home Assistant, not for developers: what they will see or can now do, in plain words. Sections, in this order, leaving out empty ones:

1. **Breaking changes** (major versions only): what people have to do, first, so nobody misses it.
2. **New**: the most exciting part, so it comes before fixes.
3. **Changed**
4. **Fixed**
5. **Notes**: for example which new parts have not been tried on a real projector yet, and that the rest of the integration is not affected if they fail.

A patch release usually has only **Fixed**. An image of a new card feature (from `docs/images`, by its raw.githubusercontent.com URL on `main`) is welcome in **New**.
