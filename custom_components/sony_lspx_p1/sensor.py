"""Sensor entities: battery level and charge state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity
from .protocol import ChargeState, ProjectorState

PARALLEL_UPDATES = 0

CHARGE_STATES = {s.value: s.name.lower() for s in ChargeState}


@dataclass(frozen=True, kw_only=True)
class LSPXSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ProjectorState], int | str | None]


SENSORS: tuple[LSPXSensorDescription, ...] = (
    LSPXSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery.level if s.battery else None,
    ),
    LSPXSensorDescription(
        key="charge_state",
        translation_key="charge_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(CHARGE_STATES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: CHARGE_STATES.get(s.battery.charge_state) if s.battery else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(LSPXSensor(entry.runtime_data, d) for d in SENSORS)


class LSPXSensor(LSPXEntity, SensorEntity):
    entity_description: LSPXSensorDescription
    _state_attr = "battery"

    def __init__(self, coordinator: LSPXCoordinator, description: LSPXSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | str | None:
        return self.entity_description.value_fn(self.coordinator.data)
