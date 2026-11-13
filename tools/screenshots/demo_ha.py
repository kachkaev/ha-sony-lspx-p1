"""Run Home Assistant with a simulated projector, for README screenshots.

No projector or Bluetooth pairing is needed: the integration talks to the in-memory
projector from tests/fake_projector.py, which speaks the real wire protocol. The demo
instance lives in tools/screenshots/.demo/ (git-ignored), opens without a login from
this computer, and has a dashboard with the card in each layout.

    .venv/bin/python tools/screenshots/demo_ha.py            # http://127.0.0.1:8123
    .venv/bin/python tools/screenshots/demo_ha.py --reset    # start from a clean instance
    .venv/bin/python tools/screenshots/demo_ha.py --slow     # changes take seconds, as on a projector

See tools/screenshots/README.md for how the screenshots are taken.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CONFIG = HERE / ".demo"
PORT = 8123

ADDRESS = "AA:BB:CC:DD:EE:FF"
TITLE = "LSPX-P1"  # entity ids become media_player.lspx_p1, number.lspx_p1_brightness, ...
ENTRY_ID = "01DEMO0LSPXP10000000000000"
OWNER_ID = "0000000000000000000000000000demo"

CONFIGURATION = """\
homeassistant:
  name: Home
  time_zone: Europe/London
  country: GB
  language: en
  unit_system: metric
  auth_providers:
    - type: trusted_networks
      trusted_networks:
        - 127.0.0.1/32
        - "::1/128"
      allow_bypass_login: true
    - type: homeassistant
http:
frontend:
config:
api:
bluetooth:
logger:
  default: warning
"""

CARD = "custom:sony-lspx-p1-card"
DASHBOARD = {
    "views": [
        {
            "title": "Projector",
            "path": "projector",
            "type": "sections",
            "max_columns": 3,
            "sections": [
                {"type": "grid", "cards": [{"type": CARD, "entity": "media_player.lspx_p1", "layout": layout}]}
                for layout in ("compact", "standard", "full")
            ],
        }
    ]
}


def storage(key: str, data: dict, version: int = 1, minor_version: int = 1) -> str:
    return json.dumps({"version": version, "minor_version": minor_version, "key": key, "data": data}, indent=2)


def prepare(reset: bool) -> None:
    if reset and CONFIG.exists():
        shutil.rmtree(CONFIG)
    store = CONFIG / ".storage"
    store.mkdir(parents=True, exist_ok=True)
    (CONFIG / "configuration.yaml").write_text(CONFIGURATION)
    components = CONFIG / "custom_components"
    components.mkdir(exist_ok=True)
    link = components / "sony_lspx_p1"
    if not link.exists():
        link.symlink_to(REPO / "custom_components" / "sony_lspx_p1")
    if (store / "auth").exists():
        return  # keep the instance as it is (registries, dashboard tweaks)
    files = {
        "onboarding": storage("onboarding", {"done": ["user", "core_config", "analytics", "integration"]}, version=4),
        "auth": storage(
            "auth",
            {
                "users": [
                    {
                        "id": OWNER_ID,
                        "group_ids": ["system-admin"],
                        "is_owner": True,
                        "is_active": True,
                        "name": "Demo",
                        "system_generated": False,
                        "local_only": False,
                    }
                ],
                "groups": [
                    {"id": "system-admin", "name": "Administrators"},
                    {"id": "system-users", "name": "Users"},
                    {"id": "system-read-only", "name": "Read Only"},
                ],
                "credentials": [],
                "refresh_tokens": [],
            },
        ),
        "core.config_entries": storage(
            "core.config_entries",
            {
                "entries": [
                    {
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "modified_at": "2026-01-01T00:00:00+00:00",
                        "data": {"address": ADDRESS},
                        "disabled_by": None,
                        "discovery_keys": {},
                        "domain": "sony_lspx_p1",
                        "entry_id": ENTRY_ID,
                        "minor_version": 1,
                        "options": {},
                        "pref_disable_new_entities": False,
                        "pref_disable_polling": False,
                        "source": "user",
                        "subentries": [],
                        "title": TITLE,
                        "unique_id": ADDRESS,
                        "version": 1,
                    }
                ]
            },
            minor_version=5,
        ),
        "lovelace_dashboards": storage(
            "lovelace_dashboards",
            {
                "items": [
                    {
                        "id": "projector",
                        "url_path": "projector",
                        "title": "Projector",
                        "icon": "mdi:projector",
                        "mode": "storage",
                        "require_admin": False,
                        "show_in_sidebar": True,
                    }
                ]
            },
        ),
        "lovelace.projector": storage("lovelace.projector", {"config": DASHBOARD}),
    }
    for name, text in files.items():
        (store / name).write_text(text)


def simulate_projector(*, slow: bool = False) -> None:
    """Point the integration at an in-memory projector instead of Bluetooth."""
    sys.path.insert(0, str(REPO))  # for tests.fake_projector and custom_components
    from bleak.backends.device import BLEDevice
    from homeassistant.components import bluetooth

    import custom_components.sony_lspx_p1.client as client_module
    from custom_components.sony_lspx_p1.protocol import Op
    from tests.fake_projector import FakeBleakClient, FakeProjector

    projector = FakeProjector()
    # A projector in use: HDMI, battery at 80% and discharging, a Custom picture mode.
    projector.volume = 40
    projector.brightness = 70
    projector.auto_brightness = [0, 0]
    projector.focus_assist = 1
    projector.keystone = 0
    projector.picture_mode = 16
    projector.picture = {"brightness": 128, "contrast": 32, "saturation": 88, "sharpness": 7}
    projector.rgb = [100, 96, 92]
    if slow:
        # A real projector can take seconds to confirm a change (a source switch most of all): use this
        # to see the card's pending state.
        projector.reply_delays = {op: 2.5 for op in Op if op.name.startswith(("SET_", "RESET_")) or op is Op.STOP_APP}
    handle = projector.handle

    def handle_with_battery(frame: bytes, encrypted: bool) -> bytes:
        if frame[:2] == Op.GET_BATTERY.to_bytes(2, "big"):
            return bytes([2]) + frame[:2] + bytes([80, 0])
        return handle(frame, encrypted)

    projector.handle = handle_with_battery
    device = BLEDevice(ADDRESS, "LSPXP1_DEMO", details=None)

    async def establish_connection(client_class, ble_device, name, disconnected_callback=None, **kwargs):
        return FakeBleakClient(projector, disconnected_callback=disconnected_callback)

    client_module.establish_connection = establish_connection
    bluetooth.async_ble_device_from_address = lambda hass, address, connectable=True: device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the demo instance and start afresh")
    parser.add_argument("--slow", action="store_true", help="make the projector take seconds to confirm changes")
    args = parser.parse_args()
    prepare(args.reset)
    simulate_projector(slow=args.slow)
    from homeassistant import runner

    print(f"Demo Home Assistant: http://127.0.0.1:{PORT}/projector/projector (Ctrl+C to stop)")
    sys.exit(runner.run(runner.RuntimeConfig(config_dir=str(CONFIG), skip_pip=True, log_no_color=True)))


if __name__ == "__main__":
    main()
