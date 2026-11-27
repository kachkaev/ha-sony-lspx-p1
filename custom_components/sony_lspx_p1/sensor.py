"""Sensor entities: battery, charge state, HDMI status, usage time, the registered smartphones, Bluetooth signal and
connection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import MAX_LENGTH_STATE_STATE as MAX_STATE_LENGTH
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .coordinator import HDMI_STATUS_CONTEXT, LSPXConfigEntry, LSPXCoordinator
from .entity import LSPXEntity, device_info
from .link_status import LinkStatus
from .protocol import ChargeState, Controller, HdmiStatus, ProjectorState
from .redaction import scrub

PARALLEL_UPDATES = 0
# How often the Bluetooth signal is read again from the advertisements Home Assistant keeps (no Bluetooth traffic),
# besides whenever an advertisement changes: as often as the projector is polled.
SIGNAL_INTERVAL = timedelta(minutes=1)

CHARGE_STATES = {s.value: s.name.lower() for s in ChargeState}
HDMI_STATES = {s.value: s.name.lower() for s in HdmiStatus}
HDMI_OFF = "off"  # wireless HDMI is not set up


def _hdmi_state(state: ProjectorState) -> str | None:
    if state.hdmi_status is None:
        return None
    enabled, status = state.hdmi_status
    return HDMI_STATES.get(status) if enabled else HDMI_OFF


@dataclass(frozen=True, kw_only=True)
class LSPXSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ProjectorState], float | str | None]
    state_attr: str = "battery"  # ProjectorState field; the entity is unavailable while it is unknown
    attributes_fn: Callable[[ProjectorState], dict[str, Any]] | None = None
    context: str | None = None  # the coordinator context the entity listens with, which tells it the entity is enabled


def _controllers_attributes(state: ProjectorState) -> dict[str, Any]:
    # Plain lists of names: Home Assistant shows them comma-separated in the entity's attributes.
    controllers = state.controllers or []
    return {
        "smartphones": [c.name for c in controllers],
        "blocked_smartphones": [c.name for c in controllers if not c.allowed],
        # For the remove/block/allow_smartphone actions, which take the id (names need not be unique).
        "smartphone_ids": {c.id: c.name for c in controllers},
    }


def _names_text(controllers: list[Controller]) -> str:
    text = ", ".join(c.name for c in controllers)
    return text if len(text) <= MAX_STATE_LENGTH else text[: MAX_STATE_LENGTH - 1] + "…"


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
    # The wireless HDMI unit: whether a source is plugged in and sending a signal. While it is enabled and HDMI
    # shows, the coordinator reads it between polls too (HDMI_STATUS_READ_DELAYS).
    LSPXSensorDescription(
        key="hdmi_status",
        context=HDMI_STATUS_CONTEXT,
        translation_key="hdmi_status",
        device_class=SensorDeviceClass.ENUM,
        options=[HDMI_OFF, *HDMI_STATES.values()],
        state_attr="hdmi_status",
        value_fn=_hdmi_state,
    ),
    # How long the projector has been on in total. Disabled by default: of interest to few. Rounded to 0.1 h
    # (6 minutes), so where it is enabled the state does not change (and is not recorded) on every poll while the
    # projector is on; diagnostics keep the decoded value (to 0.01 h).
    LSPXSensorDescription(
        key="usage_time",
        translation_key="usage_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_attr="usage_hours",
        value_fn=lambda s: round(s.usage_hours, 1) if s.usage_hours is not None else None,
    ),
    # The app's "Smartphones added": every registered controller, Home Assistant included.
    LSPXSensorDescription(
        key="smartphones_added",
        translation_key="smartphones_added",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_attr="controllers",
        value_fn=lambda s: len(s.controllers) if s.controllers is not None else None,
        attributes_fn=_controllers_attributes,
    ),
    # The same list as text, so the names show on the device page.
    LSPXSensorDescription(
        key="smartphone_names",
        translation_key="smartphone_names",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_attr="controllers",
        value_fn=lambda s: _names_text(s.controllers) if s.controllers is not None else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: LSPXConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(LSPXSensor(coordinator, d) for d in SENSORS),
            LSPXSignalSensor(coordinator),
            LSPXConnectionSensor(coordinator),
        ]
    )


class LSPXSensor(LSPXEntity, SensorEntity):
    entity_description: LSPXSensorDescription

    def __init__(self, coordinator: LSPXCoordinator, description: LSPXSensorDescription) -> None:
        super().__init__(coordinator, description.key, description.context)
        self.entity_description = description
        self._state_attr = description.state_attr

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attributes_fn
        if fn is None or self.coordinator.data is None:
            return None
        return fn(self.coordinator.data)


class LSPXSignalSensor(SensorEntity):
    """How strongly Home Assistant hears the projector's advertisements, in dBm. Disabled by default.

    Read from what Home Assistant already keeps of the advertisements, so it adds no Bluetooth traffic, and not tied
    to the polls: it shows whether the projector is heard while they fail. The signal of the advertisement Home
    Assistant keeps as current, from the adapter or proxy that can connect and hears it best (switching only on a
    clear margin or when that one goes quiet), as in Home Assistant's own Bluetooth integrations; the diagnostics list
    every adapter and proxy. Unavailable once Home Assistant stops hearing the projector, possibly also while another
    client, such as the phone app, stays connected to it (a connected device need not advertise).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "bluetooth_signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: LSPXCoordinator) -> None:
        self._address = coordinator.client.address
        self._attr_unique_id = f"{self._address}_bluetooth_signal"
        self._attr_device_info = device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        self._read()
        # Advertisements that only differ in signal strength reach no callback, so the value is read again on a
        # timer as well; the callback makes the projector available as soon as it is heard again.
        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                self._async_heard,
                bluetooth.BluetoothCallbackMatcher(address=self._address, connectable=True),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )
        self.async_on_remove(
            bluetooth.async_track_unavailable(self.hass, self._async_lost, self._address, connectable=True)
        )
        self.async_on_remove(async_track_time_interval(self.hass, self._async_tick, SIGNAL_INTERVAL))

    @callback
    def _read(self) -> None:
        # None once Home Assistant stops hearing the projector: it forgets a device whose advertisements stop.
        last = bluetooth.async_last_service_info(self.hass, self._address, connectable=True)
        self._attr_available = last is not None
        self._attr_native_value = last.rssi if last is not None else None

    @callback
    def _async_heard(self, _info: bluetooth.BluetoothServiceInfoBleak, _change: bluetooth.BluetoothChange) -> None:
        self._read()
        self.async_write_ha_state()

    @callback
    def _async_lost(self, _info: bluetooth.BluetoothServiceInfoBleak) -> None:
        self._attr_available = False
        self._attr_native_value = None
        self.async_write_ha_state()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._read()
        self.async_write_ha_state()


class LSPXConnectionSensor(LSPXEntity, SensorEntity):
    """Whether the last poll reached the projector, and if not, why not as far as Home Assistant can tell
    (link_status.py). Available while the polls fail, which is when it says something; unknown until the first poll
    ends. The card shows it in place of "Unavailable".
    """

    _attr_translation_key = "connection"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in LinkStatus]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"error"})

    def __init__(self, coordinator: LSPXCoordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        status = self.coordinator.link_status
        return status.value if status is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # The last failed poll's error, as Home Assistant logs it, without Bluetooth addresses (attributes end up in
        # screenshots and forum posts, as diagnostics do) or the padding NULs names may carry.
        error = self.coordinator.link_error
        return {"error": scrub(error.replace("\x00", ""), self.coordinator.client.address).strip() if error else None}
