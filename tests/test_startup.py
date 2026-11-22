"""Switching on from deep standby: the main board answers MAIN_BOARD_STARTING until it is up (fake projector).

Time is real here (kept short): the fake projector's replies need the event loop's clock, see AGENTS.md.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Generator
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


@pytest.fixture(autouse=True)
def no_copy_window() -> Generator[None]:
    """The fake projector sends no copies here; the probe asks every 10 ms, which the 0.5 s window would slow."""
    with patch("custom_components.sony_lspx_p1.client.DUPLICATE_WINDOW", 0):
        yield


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


async def until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until ``condition`` holds (real time, so slow runners only take longer)."""
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110 - the fake projector offers nothing to wait on
            await asyncio.sleep(0.005)


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
    assert time.monotonic() - started < COLD_START
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
    with patch.object(coordinator, "async_request_refresh") as refresh:
        await coordinator._settle_refresh(None)
    refresh.assert_not_called()
    await wait_for_start_up(hass)  # times out
    with patch.object(coordinator, "async_request_refresh") as refresh:
        await coordinator._settle_refresh(None)
    refresh.assert_called_once()


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
        # From the 15 s linger after switching on to the short one, once the switch is rejected and waits.
        await until(lambda: client._idle_deadline - asyncio.get_running_loop().time() < 2.5)
        assert not task.done()
        assert coordinator.starting_up
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


async def test_system_controller_timeout_during_a_cold_start_is_retried_at_once(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """0xC0 right after switching on came from the system controller too (GetPower): no wait for the main board."""
    await switch_on_cold(hass, projector)
    projector.transition_code = ErrorCode.TIMEOUT
    projector.transition_replies = 1
    started = time.monotonic()
    await hass.services.async_call(
        "media_player", "volume_set", {ATTR_ENTITY_ID: MP, "volume_level": 0.3}, blocking=True
    )
    assert time.monotonic() - started < COLD_START
    assert projector.volume == 30


async def test_rejection_just_after_the_board_came_up_is_retried_once(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """The start-up ended between a command and its rejection: it tries once more without starting another."""
    coordinator = coordinator_of(loaded)
    coordinator._board_up_at = asyncio.get_running_loop().time()
    projector.transition_code = ErrorCode.MAIN_BOARD_STARTING
    projector.transition_replies = 1
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert projector.poster == 6
    assert not coordinator.starting_up


async def test_main_board_starting_without_a_start_up_is_a_plain_error(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """Rejected again right after the board came up: not "still starting up", but what the projector said."""
    coordinator = coordinator_of(loaded)
    coordinator._board_up_at = asyncio.get_running_loop().time()
    projector.transition_code = ErrorCode.MAIN_BOARD_STARTING
    projector.transition_replies = 2
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
        )
    assert err.value.translation_key == "communication_error"
    assert "MAIN_BOARD_STARTING" in str(err.value)


# Restart, as seen once on a real projector showing the Weather poster: main-board reads answered MAIN_BOARD_STARTING
# for about 43 s, then app state and topology 0 for about 13 s, then the Weather poster again.

RESTART = "button.lspx_p1_1a2b_restart"
BLANK = 0.3  # seconds of app state and topology 0, standing in for about 13 s


async def restart_showing_weather(hass: HomeAssistant, coordinator: LSPXCoordinator, projector: FakeProjector) -> None:
    projector.app_state, projector.topology, projector.poster = 0x10, 0x11, 11
    await coordinator.async_refresh()
    assert hass.states.get(MP).attributes["source"] == "Weather"
    projector.restart = (COLD_START, BLANK)
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: RESTART}, blocking=True)


async def test_restart_is_a_start_up_until_the_source_is_back(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    coordinator = coordinator_of(loaded)
    await restart_showing_weather(hass, coordinator, projector)
    assert coordinator.starting_up
    await coordinator.async_refresh()
    assert coordinator.data.main_board_starting
    assert hass.states.get(MP).state == STATE_ON
    assert hass.states.get(MP).attributes.get("source") is None
    # The main board answers, but shows nothing yet: still starting up, and no source is shown.
    loop = asyncio.get_running_loop()
    await until(lambda: loop.time() > projector.main_board_up_at)
    await coordinator.async_refresh()
    assert (coordinator.data.app_state, coordinator.data.net_topology) == (0, 0)
    assert coordinator.starting_up
    assert hass.states.get(MP).attributes.get("source") is None
    # Once it shows the Weather poster again, the probe polls.
    await wait_for_start_up(hass)
    assert not coordinator.starting_up
    assert hass.states.get(MP).attributes["source"] == "Weather"


@pytest.mark.parametrize("phase", ["main_board_starting", "showing_nothing"])
async def test_source_chosen_after_restart_waits_for_the_start_up(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector, phase: str
) -> None:
    """Not sent while the board shows nothing yet: the fake one then goes back to what it showed, over the choice."""
    coordinator = coordinator_of(loaded)
    await restart_showing_weather(hass, coordinator, projector)
    await coordinator.async_refresh()  # sees MAIN_BOARD_STARTING
    loop = asyncio.get_running_loop()
    if phase == "showing_nothing":
        await until(lambda: loop.time() > projector.main_board_up_at)
        await coordinator.async_refresh()
        assert coordinator.data.app_state == 0
    log_start = len(projector.log)
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    assert not projector.blank_until  # sent once the projector showed the Weather poster again
    assert Op.SET_POSTER in [op for op, _ in projector.log[log_start:]]
    assert Op.STOP_APP not in [op for op, _ in projector.log[log_start:]]  # a poster was showing, as the app saw
    assert (projector.app_state, projector.poster) == (0x10, 6)
    await wait_for_start_up(hass)
    assert hass.states.get(MP).attributes["source"] == "Window"


async def test_source_chosen_in_standby_waits_while_the_board_shows_nothing(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """The same after a cold power-on, if its main board answers app state 0 for a while too (not seen yet)."""
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.cold_start = COLD_START
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    projector.app_state, projector.topology, projector.poster = 0x10, 0x11, 11
    projector.blank(projector.main_board_up_at + BLANK)
    await wait_for_start_up(hass)
    assert not projector.blank_until
    assert (projector.app_state, projector.poster) == (0x10, 6)
    assert hass.states.get(MP).attributes["source"] == "Window"


async def test_board_showing_nothing_at_the_deadline_counts_as_up(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    projector: FakeProjector,
    short_timeout: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A board that answers but never shows anything is not waited for longer, and gets the source chosen."""
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.cold_start = 0.05
    await hass.services.async_call(
        "media_player", "select_source", {ATTR_ENTITY_ID: MP, "source": "Window"}, blocking=True
    )
    projector.blank(float("inf"))
    await wait_for_start_up(hass)
    coordinator = coordinator_of(loaded)
    assert not coordinator.starting_up
    assert "did not finish starting up" not in caplog.text
    assert (projector.app_state, projector.poster) == (0x10, 6)


async def test_warm_power_on_showing_nothing_adds_no_wait(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """App state 0 without MAIN_BOARD_STARTING first (say, after StopApp) does not hold up the start-up."""
    coordinator = coordinator_of(loaded)
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    projector.app_state = 0
    await hass.services.async_call("media_player", "turn_on", {ATTR_ENTITY_ID: MP}, blocking=True)
    await coordinator.async_refresh()
    assert not coordinator.starting_up


async def test_restart_after_a_start_up_timed_out_waits_again(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    """Restart is what one might press for a main board that never came up."""
    coordinator = coordinator_of(loaded)
    coordinator._startup_spent = True
    await restart_showing_weather(hass, coordinator, projector)
    await coordinator.async_refresh()
    assert coordinator.starting_up
    await wait_for_start_up(hass)
    assert hass.states.get(MP).attributes["source"] == "Weather"


async def test_restart_in_standby_starts_no_wait(
    hass: HomeAssistant, loaded: MockConfigEntry, projector: FakeProjector
) -> None:
    await hass.services.async_call("media_player", "turn_off", {ATTR_ENTITY_ID: MP}, blocking=True)
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: RESTART}, blocking=True)
    assert not coordinator_of(loaded).starting_up
