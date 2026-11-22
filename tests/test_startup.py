"""Switching on from deep standby: the main board answers MAIN_BOARD_STARTING until it is up (fake projector).

Time is real here (kept short): the fake projector's replies need the event loop's clock, see AGENTS.md.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sony_lspx_p1.coordinator import LSPXCoordinator
from custom_components.sony_lspx_p1.protocol import ErrorCode, Op

from .fake_projector import FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"
HDMI_STATUS = "sensor.lspx_p1_1a2b_hdmi_status"
COLD_START = 0.3  # seconds, standing in for the 34-96 s seen on a real projector


@pytest.fixture
def short_timeout() -> Generator[None]:
    with patch("custom_components.sony_lspx_p1.coordinator.STARTUP_TIMEOUT", 0.2):
        yield


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    yield config_entry
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


def coordinator_of(entry: MockConfigEntry) -> LSPXCoordinator:
    coordinator: LSPXCoordinator = entry.runtime_data
    return coordinator


async def switch_on_cold(hass: HomeAssistant, projector: FakeProjector) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.cold_start = COLD_START
    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)


async def wait_for_start_up(hass: HomeAssistant) -> None:
    """Until the probe has seen the main board up (or given up) and polled."""
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_cold_start_system_controller_works_main_board_waits(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await switch_on_cold(hass, projector)
    coordinator = coordinator_of(loaded)
    assert coordinator.starting_up
    # The system controller answers at once: volume does not wait for the main board.
    started = time.monotonic()
    await hass.services.async_call(
        "media_player", "volume_set", {ATTR_ENTITY_ID: MP, "volume_level": 0.25}, blocking=True
    )
    assert time.monotonic() - started < COLD_START
    assert projector.volume == 25
    # A poll meanwhile: on, but the source and HDMI status are not known yet.
    await coordinator.async_refresh()
    assert coordinator.data.main_board_starting
    assert "MAIN_BOARD_STARTING" in coordinator.data.errors["app_state"]
    assert hass.states.get(MP).state == STATE_ON
    assert hass.states.get(MP).attributes.get("source") is None
    assert hass.states.get(HDMI_STATUS).state == STATE_UNAVAILABLE
    # Once the main board answers, a poll fills them in without waiting for the next regular one.
    await wait_for_start_up(hass)
    assert not coordinator.starting_up
    assert not coordinator.data.main_board_starting
    assert hass.states.get(MP).attributes["source"] == "HDMI"
    assert hass.states.get(HDMI_STATUS).state == "connected"


async def test_source_switch_during_cold_start_succeeds_once_the_board_is_up(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await switch_on_cold(hass, projector)
    log_start = len(projector.log)
    started = time.monotonic()
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert time.monotonic() - started >= COLD_START * 0.9  # it waited for the main board
    assert (projector.app_state, projector.poster) == (0x10, 6)
    assert hass.states.get(MP).attributes["source"] == "Window"
    # Tried once, rejected, then once more after the probe saw the board up: no retry of its own meanwhile.
    switch_reads = [op for op, _ in projector.log[log_start:] if op == Op.GET_NET_TOPOLOGY]
    assert len(switch_reads) == 2


async def test_source_switch_fails_clearly_if_the_board_never_comes_up(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, short_timeout: None
) -> None:
    await switch_on_cold(hass, projector)
    projector.main_board_up_at = float("inf")
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
        )
    assert err.value.translation_key == "starting_up"
    assert "0xe0" not in str(err.value).lower()
    coordinator = coordinator_of(loaded)
    assert not coordinator.starting_up
    # A board that never answers is not waited for again and again: polls and commands fail at once now.
    await coordinator.async_refresh()
    assert coordinator.data.main_board_starting
    assert not coordinator.starting_up
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "HDMI"}, blocking=True
        )
    assert err.value.translation_key == "starting_up"
    # Off and on again: a new start-up.
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)
    assert coordinator.starting_up


async def test_source_chosen_in_standby_survives_a_cold_start(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """As in an automation: the choice switches the projector on, and is applied once the main board is up."""
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.cold_start = COLD_START
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert projector.power == 0x10
    # A settle poll during the start-up leaves the choice pending instead of trying it and dropping it.
    await coordinator_of(loaded).async_refresh()
    assert coordinator_of(loaded)._pending_source is not None
    assert projector.app_state == 0x60
    await wait_for_start_up(hass)
    assert (projector.app_state, projector.poster) == (0x10, 6)
    assert hass.states.get(MP).attributes["source"] == "Window"


@pytest.mark.parametrize("was_off", [True, False], ids=["standby_to_on", "found_starting"])
async def test_power_on_seen_by_a_poll_starts_the_wait(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, was_off: bool
) -> None:
    """Switched on by its button, the app or a timer: a poll sees power on, or main-board reads rejected."""
    coordinator = coordinator_of(loaded)
    if was_off:
        projector.power = 0x00
        await coordinator.async_refresh()
        assert hass.states.get(MP).state == "off"
    projector.power = 0x10
    projector.main_board_up_at = asyncio.get_running_loop().time() + COLD_START
    await coordinator.async_refresh()
    assert coordinator.starting_up
    assert hass.states.get(MP).attributes.get("source") is None
    # A command meanwhile waits rather than fail.
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Clock 2"}, blocking=True
    )
    assert (projector.app_state, projector.poster) == (0x10, 9)
    await wait_for_start_up(hass)
    assert not coordinator.starting_up
    assert hass.states.get(MP).attributes["source"] == "Clock 2"


async def test_main_board_starting_in_standby_starts_no_wait(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """In standby GetFocus answers 0xE0 (a system controller command): that is not a start-up."""
    coordinator = coordinator_of(loaded)
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.rejections = {Op.GET_FOCUS: ErrorCode.MAIN_BOARD_STARTING}
    await coordinator.async_refresh()
    assert not coordinator.starting_up
    assert not coordinator.data.main_board_starting


async def test_warm_start_adds_no_delay(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    """Woken soon after standby, the main board is still up: a source switch right away goes through at once."""
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)
    log_start = len(projector.log)
    started = time.monotonic()
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert time.monotonic() - started < 0.2
    assert [op for op, _ in projector.log[log_start:] if op == Op.GET_NET_TOPOLOGY] == [Op.GET_NET_TOPOLOGY]
    assert hass.states.get(MP).attributes["source"] == "Window"
    # The next poll sees the main board up: the start-up is over.
    coordinator = coordinator_of(loaded)
    await coordinator.async_refresh()
    assert not coordinator.starting_up


async def test_settle_polls_pause_during_a_cold_start(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, short_timeout: None
) -> None:
    await switch_on_cold(hass, projector)
    projector.main_board_up_at = float("inf")
    coordinator = coordinator_of(loaded)
    await coordinator.async_refresh()  # confirms the cold start
    polls = projector.log.count((Op.GET_POWER, b""))
    with patch.object(coordinator, "async_request_refresh") as refresh:
        await coordinator._settle_refresh(None)
    refresh.assert_not_called()
    await wait_for_start_up(hass)  # times out
    with patch.object(coordinator, "async_request_refresh") as refresh:
        await coordinator._settle_refresh(None)
    refresh.assert_called_once()
    assert projector.log.count((Op.GET_POWER, b"")) == polls


async def test_waiting_frees_the_link(hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector) -> None:
    """A command waiting for the main board does not keep its link open for the usual 15 s."""
    coordinator = coordinator_of(loaded)
    client = coordinator.client
    with patch("custom_components.sony_lspx_p1.coordinator.STARTUP_PROBE_INTERVAL", 30):
        await switch_on_cold(hass, projector)
        task = hass.async_create_task(
            hass.services.async_call(
                "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
            )
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        assert client._idle_deadline - asyncio.get_running_loop().time() < 2.5
        projector.main_board_up_at = 0.0
        coordinator._end_startup()
        await task
    assert projector.poster == 6


async def test_command_seeing_main_board_starting_starts_the_wait(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """Switched on some other way and no poll since: the command is the first to see the main board starting."""
    projector.main_board_up_at = asyncio.get_running_loop().time() + COLD_START
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert (projector.app_state, projector.poster) == (0x10, 6)


async def test_source_chosen_in_standby_is_dropped_if_the_board_never_comes_up(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    projector: FakeProjector,
    short_timeout: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.cold_start = float("inf")
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    await wait_for_start_up(hass)
    assert coordinator_of(loaded)._pending_source is None
    assert "did not finish starting up within" in caplog.text
    assert "dropping the source chosen in standby" in caplog.text
    assert projector.app_state == 0x60
