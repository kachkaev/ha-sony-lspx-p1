"""The Bluetooth signal sensor: read from the advertisements Home Assistant keeps, not from the polls."""

from __future__ import annotations

from collections.abc import Callable, Generator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
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


def _path(rssi: int) -> SimpleNamespace:
    """What async_scanner_devices_by_address returns for one adapter or proxy that hears the projector."""
    return SimpleNamespace(advertisement=SimpleNamespace(rssi=rssi))


@pytest.fixture
def paths() -> Generator[list[SimpleNamespace]]:
    """The adapters and proxies that hear the projector (none of the test's fake scanners record it)."""
    heard: list[SimpleNamespace] = []
    with patch("homeassistant.components.bluetooth.async_scanner_devices_by_address", return_value=heard):
        yield heard


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
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry, paths: list, lost: list):
    with patch.object(LSPXSignalSensor, "_attr_entity_registry_enabled_default", True):
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)


def _advertise(hass: HomeAssistant, ble_device) -> None:
    async_get_advertisement_callback(hass)(make_service_info(ble_device))


async def test_disabled_by_default(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entry = er.async_get(hass).async_get(SIGNAL)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(SIGNAL) is None


async def test_strongest_path_while_heard(
    hass: HomeAssistant, loaded: MockConfigEntry, paths: list, lost: list, ble_device
) -> None:
    # Not heard since Home Assistant started.
    assert hass.states.get(SIGNAL).state == STATE_UNAVAILABLE

    paths[:] = [_path(-80), _path(-55)]
    _advertise(hass, ble_device)
    await hass.async_block_till_done()
    state = hass.states.get(SIGNAL)
    assert state.state == "-55"
    assert state.attributes["unit_of_measurement"] == "dBm"
    assert state.attributes["device_class"] == "signal_strength"
    assert state.attributes["state_class"] == "measurement"

    # Advertisements that differ only in signal strength reach no callback: the timer reads it again.
    paths[:] = [_path(-80), _path(-62)]
    async_fire_time_changed(hass, dt_util.utcnow() + SIGNAL_INTERVAL)
    await hass.async_block_till_done()
    assert hass.states.get(SIGNAL).state == "-62"

    for callback in lost:
        callback(make_service_info(ble_device))
    await hass.async_block_till_done()
    assert hass.states.get(SIGNAL).state == STATE_UNAVAILABLE


async def test_no_path_that_can_connect(hass: HomeAssistant, loaded: MockConfigEntry, ble_device) -> None:
    # Heard (by a scanner that cannot connect, say), but nothing could connect to it: nothing to show.
    _advertise(hass, ble_device)
    await hass.async_block_till_done()
    assert hass.states.get(SIGNAL).state == STATE_UNAVAILABLE


async def test_stays_available_while_polls_fail(
    hass: HomeAssistant, loaded: MockConfigEntry, paths: list, projector: FakeProjector, ble_device
) -> None:
    paths[:] = [_path(-70)]
    _advertise(hass, ble_device)
    projector.busy = True
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(MP).state == STATE_UNAVAILABLE
    assert hass.states.get(SIGNAL).state == "-70"
