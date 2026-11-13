"""The projector's change notifications make Home Assistant poll sooner."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.sony_lspx_p1.protocol import Op

from .fake_projector import FakeBleakClient, FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup")

MP = "media_player.lspx_p1_1a2b"
# Framed as the app parses them: [result][opcode][value]; the result byte is a guess.
POWER_STANDBY = bytes.fromhex("01800000")
APP_STATE_POSTER = bytes.fromhex("01801210")
APP_STATE_HDMI = bytes.fromhex("01801260")
TOPOLOGY_HOME_POSTER = bytes.fromhex("01801111")
MODE_READY = bytes.fromhex("01801040")
UNKNOWN = bytes.fromhex("01802001")


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry, fake_connection: list[FakeBleakClient]):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def link(loaded: MockConfigEntry, fake_connection: list[FakeBleakClient]) -> FakeBleakClient:
    """The connection the first poll left open (it lingers for a moment before disconnecting)."""
    assert fake_connection[-1].is_connected
    return fake_connection[-1]


def polls(projector: FakeProjector) -> int:
    return sum(1 for op, _ in projector.log if op == Op.GET_POWER)


async def test_source_changed_on_the_projector_shows_at_once(
    hass: HomeAssistant, link: FakeBleakClient, projector: FakeProjector
) -> None:
    projector.app_state, projector.topology, projector.poster = 0x10, 0x11, 9  # Clock 2, chosen with the remote
    link.notify(TOPOLOGY_HOME_POSTER)
    link.notify(APP_STATE_POSTER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MP).attributes["source"] == "Clock 2"


async def test_power_changed_on_the_projector_polls_once_it_settles(
    hass: HomeAssistant, link: FakeBleakClient, projector: FakeProjector
) -> None:
    before = polls(projector)
    projector.power = 0
    link.notify(POWER_STANDBY)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before  # reads fail for a moment after switching power
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MP).state == STATE_OFF


async def test_repeated_notifications_poll_only_once(
    hass: HomeAssistant, link: FakeBleakClient, projector: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    before = polls(projector)
    link.notify(APP_STATE_HDMI)  # nothing changed, as far as the poll can tell
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before + 1
    link.notify(APP_STATE_HDMI)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))  # past the refresh cooldown
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before + 1

    # Once a poll has seen the source change, the same notification counts again.
    projector.app_state, projector.topology, projector.poster = 0x10, 0x11, 9
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))  # the next regular poll
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before + 2
    assert hass.states.get(MP).attributes["source"] == "Clock 2"
    projector.app_state, projector.topology = 0x60, 0x30  # back to HDMI with the remote, right after that poll
    fake_connection[-1].notify(APP_STATE_HDMI)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before + 3
    assert hass.states.get(MP).attributes["source"] == "HDMI"


async def test_other_notifications_are_only_logged(
    hass: HomeAssistant, link: FakeBleakClient, projector: FakeProjector, caplog: pytest.LogCaptureFixture
) -> None:
    before = polls(projector)
    with caplog.at_level("DEBUG", "custom_components.sony_lspx_p1.client"):
        link.notify(MODE_READY)
        link.notify(UNKNOWN)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before
    assert "notification 0x8010 (MODE), result 1, data 40" in caplog.text
    assert "notification 0x8020 (unknown), result 1, data 01" in caplog.text


async def test_notifications_during_a_switch_wait_for_its_own_polls(
    hass: HomeAssistant, link: FakeBleakClient, projector: FakeProjector
) -> None:
    # Sent while the source switch is under way (with the reply to its first step).
    projector.unsolicited = [TOPOLOGY_HOME_POSTER, APP_STATE_POSTER]
    before = polls(projector)
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Clock 2"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before  # no poll in the middle of the switch
    assert hass.states.get(MP).attributes["source"] == "Clock 2"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))  # the switch's own settle poll
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before + 1


async def test_no_notifications_handled_after_unload(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_connection: list[FakeBleakClient], projector: FakeProjector
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    link, client = fake_connection[-1], config_entry.runtime_data.client
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    before = polls(projector)
    link.notify(APP_STATE_POSTER)  # a frame bleak still delivers as the link goes down
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(projector) == before
    assert client._notification_callback is None
