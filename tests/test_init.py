"""Setup and entity tests against the fake projector."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .fake_projector import FakeBleakClient, FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_6320"


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_entities_reflect_projector(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    mp = hass.states.get(MP)
    assert mp.state == STATE_ON
    assert mp.attributes["volume_level"] == 0.67
    assert mp.attributes["source"] == "HDMI"
    assert hass.states.get("number.lspx_p1_6320_brightness").state == "81"
    focus = hass.states.get("number.lspx_p1_6320_focus")
    assert focus.state == "-370"
    assert focus.attributes["min"] == -383
    assert focus.attributes["max"] == 149
    assert hass.states.get("number.lspx_p1_6320_keystone").state == "5"
    assert hass.states.get("switch.lspx_p1_6320_auto_focus").state == STATE_ON
    assert hass.states.get("switch.lspx_p1_6320_auto_brightness").state == STATE_OFF
    assert hass.states.get("select.lspx_p1_6320_screen_rotation").state == "normal"
    assert hass.states.get("select.lspx_p1_6320_picture_mode").state == "preset_1"
    assert hass.states.get("sensor.lspx_p1_6320_battery").state == "100"
    assert hass.states.get("sensor.lspx_p1_6320_charge_state").state == "full"
    # Picture parameters only apply to the custom modes.
    assert hass.states.get("number.lspx_p1_6320_picture_contrast").state == STATE_UNAVAILABLE


async def test_power_and_volume(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    assert projector.power == 0
    assert hass.states.get(MP).state == STATE_OFF

    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)
    assert projector.power == 0x10
    assert hass.states.get(MP).state == STATE_ON

    await hass.services.async_call(
        "media_player", "volume_set", {ATTR_ENTITY_ID: MP, "volume_level": 0.3}, blocking=True
    )
    assert projector.volume == 30
    assert hass.states.get(MP).attributes["volume_level"] == 0.3


async def test_manual_focus_turns_auto_focus_off(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: "number.lspx_p1_6320_focus", "value": -200}, blocking=True
    )
    assert projector.focus == -200
    assert projector.focus_assist == 0
    assert hass.states.get("switch.lspx_p1_6320_auto_focus").state == STATE_OFF


async def test_rotation_turns_auto_rotation_off(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.lspx_p1_6320_screen_rotation", "option": "rotate_180"},
        blocking=True,
    )
    assert projector.direction == 1
    assert projector.auto_direction == 0
    assert hass.states.get("switch.lspx_p1_6320_auto_rotation").state == STATE_OFF


async def test_custom_picture_mode_enables_picture_controls(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.lspx_p1_6320_picture_mode", "option": "custom_1"},
        blocking=True,
    )
    assert projector.picture_mode == 16
    contrast = "number.lspx_p1_6320_picture_contrast"
    assert hass.states.get(contrast).state == "32"
    await hass.services.async_call("number", "set_value", {ATTR_ENTITY_ID: contrast, "value": 20}, blocking=True)
    assert projector.picture["contrast"] == 20


async def test_switches(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.lspx_p1_6320_auto_brightness"}, blocking=True
    )
    assert projector.auto_brightness == [1, 1]
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: "switch.lspx_p1_6320_xloud"}, blocking=True)
    assert projector.xloud == 0


async def test_lost_pairing_starts_reauth(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, fake_connection: list[FakeBleakClient]
) -> None:
    projector.bonded = False
    for client in fake_connection:
        client.connected = False
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_busy_projector_marks_entities_unavailable(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    projector.busy = True
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(MP).state == STATE_UNAVAILABLE
