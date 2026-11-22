"""The Bluetooth signal sensor: read from the advertisements Home Assistant keeps, not from the polls."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import async_get_advertisement_callback
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.sony_lspx_p1.sensor import SIGNAL_INTERVAL, LSPXSignalSensor

from .conftest import make_service_info
from .fake_projector import FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

SIGNAL = "sensor.lspx_p1_1a2b_bluetooth_signal"
MP = "media_player.lspx_p1_1a2b"


@pytest.fixture
def lost() -> Generator[list[Callable[[Any], None]]]:
    """The callbacks Home Assistant calls when it stops hearing the projector."""
    callbacks: list[Callable[[Any], None]] = []

    def track(hass: HomeAssistant, callback: Callable[[Any], None], address: str, connectable: bool) -> Callable:
        callbacks.append(callback)
        return lambda: None

    with patch("homeassistant.components.bluetooth.async_track_unavailable", track):
        yield callbacks


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry, lost: list):
    with patch.object(LSPXSignalSensor, "_attr_entity_registry_enabled_default", True):
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)


def _advertise(hass: HomeAssistant, ble_device: BLEDevice, rssi: int) -> None:
    async_get_advertisement_callback(hass)(make_service_info(ble_device, rssi=rssi))


async def test_disabled_by_default(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entry = er.async_get(hass).async_get(SIGNAL)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(SIGNAL) is None


async def test_follows_the_advertisements(
    hass: HomeAssistant, loaded: MockConfigEntry, lost: list, ble_device: BLEDevice
) -> None:
    # Not heard since Home Assistant started.
    assert hass.states.get(SIGNAL).state == STATE_UNAVAILABLE

    _advertise(hass, ble_device, -55)
    await hass.async_block_till_done()
    state = hass.states.get(SIGNAL)
    assert state.state == "-55"
    assert state.attributes["unit_of_measurement"] == "dBm"
    assert state.attributes["device_class"] == "signal_strength"
    assert state.attributes["state_class"] == "measurement"

    # An advertisement that differs only in signal strength reaches no callback: the timer reads it.
    _advertise(hass, ble_device, -62)
    await hass.async_block_till_done()
    assert hass.states.get(SIGNAL).state == "-55"
    async_fire_time_changed(hass, dt_util.utcnow() + SIGNAL_INTERVAL)
    await hass.async_block_till_done()
    assert hass.states.get(SIGNAL).state == "-62"

    for callback in lost:
        callback(make_service_info(ble_device))
    await hass.async_block_till_done()
    assert hass.states.get(SIGNAL).state == STATE_UNAVAILABLE


async def test_stays_available_while_polls_fail(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, ble_device: BLEDevice
) -> None:
    _advertise(hass, ble_device, -70)
    projector.busy = True
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(MP).state == STATE_UNAVAILABLE
    assert hass.states.get(SIGNAL).state == "-70"
