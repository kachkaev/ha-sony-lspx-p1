"""Constants for the Sony LSPX-P1 integration."""

from datetime import timedelta

DOMAIN = "sony_lspx_p1"
MANUFACTURER = "Sony"
MODEL = "LSPX-P1"

# The name this controller registers under in the projector's "Smartphones added" list.
CONTROLLER_NAME = "Home Assistant"

UPDATE_INTERVAL = timedelta(seconds=60)
# Poll faster for a short while after switching power, because the projector takes a
# few seconds to settle into standby or to boot.
POWER_SETTLE_REFRESH_DELAYS = (3, 8, 15, 30)
