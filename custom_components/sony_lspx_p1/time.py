"""Time entities: when the daily timers switch the projector on or to standby (over Wi-Fi, experimental)."""

from __future__ import annotations

import contextlib
from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import web_api
from .coordinator import LSPXConfigEntry
from .entity import LSPXWebEntity
from .web_settings import TIMERS, WebSettingsCoordinator

PARALLEL_UPDATES = 1

# key -> the timer's tag; the matching switches are in switch.py.
TIMER_TIMES = {"switch_on_time": web_api.TIMER_ON, "standby_time": web_api.TIMER_OFF}


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    web_settings = entry.runtime_data.web_settings
    assert web_settings is not None  # set up with the coordinator
    async_add_entities(LSPXTimerTime(web_settings, key, tag) for key, tag in TIMER_TIMES.items())


class LSPXTimerTime(LSPXWebEntity, TimeEntity, RestoreEntity):
    """A daily timer's time, in Home Assistant's time zone (the projector's clock follows it, see clock.py).

    While the timer is off the projector keeps no time for it, so the last one is kept here (across restarts too)
    for when the timer is switched on again.
    """

    def __init__(self, coordinator: WebSettingsCoordinator, key: str, tag: str) -> None:
        super().__init__(coordinator, key, TIMERS)
        self._attr_translation_key = key
        self._tag = tag

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) is not None and self._tag not in self.coordinator.remembered:
            with contextlib.suppress(ValueError):  # unknown or unavailable
                self.coordinator.remembered[self._tag] = dt_time.fromisoformat(last.state)
        await super().async_added_to_hass()

    @property
    def native_value(self) -> dt_time | None:
        return self.coordinator.timer_time(self._tag)

    async def async_set_value(self, value: dt_time) -> None:
        await self.coordinator.async_set_timer_time(self._tag, value)
