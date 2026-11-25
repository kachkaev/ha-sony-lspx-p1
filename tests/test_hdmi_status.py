"""The HDMI status read between polls while HDMI shows (#14)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.sony_lspx_p1.coordinator import HDMI_STATUS_READ_DELAYS
from custom_components.sony_lspx_p1.protocol import AppState, ErrorCode, Op

from .fake_projector import FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

HDMI = "sensor.lspx_p1_1a2b_hdmi_status"


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


def _count(projector: FakeProjector, op: int) -> int:
    return sum(1 for o, _ in projector.log if o == op)


async def _after(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_read_between_polls_while_hdmi_shows(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = loaded.runtime_data
    await coordinator.async_refresh()
    assert hass.states.get(HDMI).state == "connected"
    polls = _count(projector, Op.GET_POWER)
    reads = _count(projector, Op.GET_HDMI_STATUS)
    log = len(projector.log)

    projector.hdmi = [1, 1]  # the source unplugged
    await _after(hass, HDMI_STATUS_READ_DELAYS[0] + 1)
    assert hass.states.get(HDMI).state == "unplugged"
    # The HDMI status alone, not a poll.
    assert [op for op, _ in projector.log[log:]] == [Op.GET_HDMI_STATUS]

    projector.hdmi = [1, 4]
    await _after(hass, HDMI_STATUS_READ_DELAYS[1] + 1)
    assert hass.states.get(HDMI).state == "connected"
    assert _count(projector, Op.GET_HDMI_STATUS) == reads + 2
    assert _count(projector, Op.GET_POWER) == polls

    # The next poll a minute after the last, not put off by the changes shown in between.
    await _after(hass, 61)
    assert _count(projector, Op.GET_POWER) == polls + 1


async def test_unchanged_status_writes_nothing(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = loaded.runtime_data
    await coordinator.async_refresh()
    before = hass.states.get(HDMI)
    reads = _count(projector, Op.GET_HDMI_STATUS)
    await _after(hass, HDMI_STATUS_READ_DELAYS[0] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads + 1
    assert hass.states.get(HDMI).last_reported == before.last_reported


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda p: setattr(p, "app_state", 0x10), id="poster"),
        pytest.param(lambda p: setattr(p, "power", 0x00), id="standby"),
    ],
)
async def test_not_read_unless_hdmi_shows(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, change
) -> None:
    change(projector)
    await loaded.runtime_data.async_refresh()
    reads = _count(projector, Op.GET_HDMI_STATUS)
    await _after(hass, HDMI_STATUS_READ_DELAYS[-1] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads


async def test_not_read_while_the_sensor_is_disabled(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    er.async_get(hass).async_update_entity(HDMI, disabled_by=er.RegistryEntryDisabler.USER)
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    await loaded.runtime_data.async_refresh()
    reads = _count(projector, Op.GET_HDMI_STATUS)
    await _after(hass, HDMI_STATUS_READ_DELAYS[-1] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads


async def test_failed_read_keeps_the_status_and_stops_until_the_next_poll(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = loaded.runtime_data
    await coordinator.async_refresh()
    reads = _count(projector, Op.GET_HDMI_STATUS)
    projector.rejections[Op.GET_HDMI_STATUS] = ErrorCode.UNKNOWN_COMMAND
    await _after(hass, HDMI_STATUS_READ_DELAYS[0] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads + 1
    assert hass.states.get(HDMI).state == "connected"  # neither unavailable nor changed
    assert coordinator.last_update_success
    await _after(hass, HDMI_STATUS_READ_DELAYS[1] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads + 1

    # The next poll plans them again.
    del projector.rejections[Op.GET_HDMI_STATUS]
    await coordinator.async_refresh()
    projector.hdmi = [1, 2]
    await _after(hass, HDMI_STATUS_READ_DELAYS[0] + 1)
    assert hass.states.get(HDMI).state == "no_signal"


async def test_failed_poll_stops_them(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    coordinator = loaded.runtime_data
    await coordinator.async_refresh()
    projector.busy = True
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    projector.busy = False
    reads = _count(projector, Op.GET_HDMI_STATUS)
    await _after(hass, HDMI_STATUS_READ_DELAYS[-1] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads


async def test_skipped_during_a_switch(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    coordinator = loaded.runtime_data
    await coordinator.async_refresh()
    reads = _count(projector, Op.GET_HDMI_STATUS)
    async with coordinator._switching:
        await _after(hass, HDMI_STATUS_READ_DELAYS[0] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads


async def test_skipped_once_a_command_left_hdmi(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = loaded.runtime_data
    await coordinator.async_refresh()
    reads = _count(projector, Op.GET_HDMI_STATUS)
    coordinator.data.app_state = AppState.POSTER  # as a source switch applies it, before its settle polls
    await _after(hass, HDMI_STATUS_READ_DELAYS[0] + 1)
    assert _count(projector, Op.GET_HDMI_STATUS) == reads
