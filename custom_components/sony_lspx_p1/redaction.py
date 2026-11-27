"""Bluetooth addresses left out of what users share: diagnostics and the Connection sensor's error attribute."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "**REDACTED**"  # as Home Assistant's diagnostics redact
# Bluetooth addresses (of the projector, adapters and proxies) that error texts and adapter names may contain, also
# as in BlueZ object paths (dev_AA_BB_CC_DD_EE_FF).
_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}[:_-]){5}[0-9A-Fa-f]{2}")


def scrub(value: Any, address: str) -> Any:
    """``value`` with Bluetooth addresses in its strings redacted."""
    if isinstance(value, dict):
        return {key: scrub(item, address) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, address) for item in value]
    if isinstance(value, str):
        # Also the projector's address where it is not a MAC address (CoreBluetooth on macOS).
        return _MAC.sub(REDACTED, re.sub(re.escape(address), REDACTED, value, flags=re.IGNORECASE))
    return value
