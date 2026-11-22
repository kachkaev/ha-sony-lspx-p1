"""The repair flow of the "does not respond" issue: try Restart, then say whether it helped."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.components.repairs.issue_handler import RepairsFlowManager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sony_lspx_p1.client import LSPXClient
from custom_components.sony_lspx_p1.const import DOMAIN
from custom_components.sony_lspx_p1.coordinator import STUCK_POLLS_BEFORE_ISSUE, LSPXCoordinator
from custom_components.sony_lspx_p1.protocol import Op

from .fake_projector import FakeProjector

pytestmark = pytest.mark.usefixtures("mock_bluetooth", "mock_ble_lookup", "fake_connection")

MP = "media_player.lspx_p1_1a2b"


@pytest.fixture(autouse=True)
def fast() -> Generator[None]:
    """No waits between power reads or for copies; a start-up after Restart ends at once."""
    with (
        patch("custom_components.sony_lspx_p1.client.TRANSITION_RETRY_DELAY", 0),
        patch("custom_components.sony_lspx_p1.client.DUPLICATE_WINDOW", 0),
        patch("custom_components.sony_lspx_p1.coordinator.STARTUP_TIMEOUT", 0.2),
    ):
        yield


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry):
    assert await async_setup_component(hass, "repairs", {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    if config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def stuck(loaded: MockConfigEntry, projector: FakeProjector) -> str:
    """The projector times out every command until the issue is raised; its id."""
    projector.busy = True
    coordinator: LSPXCoordinator = loaded.runtime_data
    for _ in range(STUCK_POLLS_BEFORE_ISSUE):
        await coordinator.async_refresh()
    issue_id = f"projector_stuck_{loaded.entry_id}"
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id) is not None
    return issue_id


def manager(hass: HomeAssistant) -> RepairsFlowManager:
    flows = repairs_flow_manager(hass)
    assert flows is not None
    return flows


async def start(hass: HomeAssistant, issue_id: str) -> str:
    """Open the issue's flow at its first step; the flow's id."""
    # The issue's id in both: older Home Assistant reads it from the data, newer from the context.
    result = await manager(hass).async_init(DOMAIN, context={"issue_id": issue_id}, data={"issue_id": issue_id})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"name": "LSPX-P1 1A2B"}
    return result["flow_id"]


async def run(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    """Confirm, let the attempt finish, and return how the flow ended."""
    result = await manager(hass).async_configure(flow_id, {})
    if result["type"] is not FlowResultType.SHOW_PROGRESS:
        return dict(result)  # over before it had to wait (the task starts eagerly)
    assert result["progress_action"] == "restart"
    await hass.async_block_till_done()
    return dict(await manager(hass).async_configure(flow_id))


def reboots(projector: FakeProjector) -> int:
    return sum(op == Op.REBOOT for op, _ in projector.log)


def issue_exists(hass: HomeAssistant, issue_id: str) -> bool:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_restart_that_gets_through_repairs(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stuck: str,
    projector: FakeProjector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    projector.reboot_unsticks = True
    flow_id = await start(hass, stuck)
    result = await manager(hass).async_configure(flow_id, {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    # The first poll that reads the power ends the wait (here one in place of those Restart schedules after 3 s).
    async with asyncio.timeout(5):
        while not reboots(projector):  # noqa: ASYNC110 - the fake projector has no event for it
            await asyncio.sleep(0.01)
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    result = await manager(hass).async_configure(flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert reboots(projector) == 1
    assert not issue_exists(hass, stuck)
    assert hass.states.get(MP).state == STATE_ON
    assert "Restart from the repair issue sent; waiting" in caplog.text
    assert "responds again after Restart from the repair issue" in caplog.text


async def test_restart_that_times_out_too(
    hass: HomeAssistant, stuck: str, projector: FakeProjector, caplog: pytest.LogCaptureFixture
) -> None:
    """Reboot answers TIMEOUT like everything else: still waited for (it might restart all the same), then RESET."""
    with patch("custom_components.sony_lspx_p1.repairs.RESTART_WAIT", 0.05):
        result = await run(hass, await start(hass, stuck))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "restart_not_answered"
    assert result["description_placeholders"] == {"name": "LSPX-P1 1A2B"}
    assert reboots(projector) == 1
    assert issue_exists(hass, stuck)  # with its RESET advice, and the flow can be tried again
    assert "Restart from the repair issue timed out (0xC0); waiting" in caplog.text
    assert "still does not respond after Restart from the repair issue (UpdateFailed" in caplog.text


async def test_attempt_that_takes_too_long_after_restart(
    hass: HomeAssistant, stuck: str, projector: FakeProjector, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole attempt is bounded too, also while it waits (here with no poll coming)."""
    with patch("custom_components.sony_lspx_p1.repairs.ATTEMPT_LIMIT", 1):
        result = await run(hass, await start(hass, stuck))
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "attempt_timed_out")
    assert reboots(projector) == 1
    assert "not over after 1 s" in caplog.text
    assert issue_exists(hass, stuck)


async def test_attempt_that_hangs_in_restart(hass: HomeAssistant, stuck: str, caplog: pytest.LogCaptureFixture) -> None:
    async def hang() -> str:
        await asyncio.Event().wait()
        return "acknowledged"

    with (
        patch("custom_components.sony_lspx_p1.repairs.ATTEMPT_LIMIT", 0.5),
        patch.object(LSPXClient, "reboot", AsyncMock(side_effect=hang)) as reboot,
    ):
        result = await run(hass, await start(hass, stuck))
    reboot.assert_awaited_once()
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "attempt_timed_out")
    assert "; waiting up to" not in caplog.text  # cut short in Restart, not in the wait after it


async def test_closing_the_dialog_stops_the_attempt(
    hass: HomeAssistant, stuck: str, projector: FakeProjector, caplog: pytest.LogCaptureFixture
) -> None:
    """What Restart answered is logged before the wait, so it is on record when the dialog is closed."""
    flow_id = await start(hass, stuck)
    result = await manager(hass).async_configure(flow_id, {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    async with asyncio.timeout(5):
        while "; waiting up to" not in caplog.text:  # noqa: ASYNC110 - nothing to wait on but the log
            await asyncio.sleep(0.01)
    assert "Restart from the repair issue timed out (0xC0); waiting" in caplog.text
    manager(hass).async_abort(flow_id)  # what closing the dialog does
    await hass.async_block_till_done()
    assert "stopped before it was over" in caplog.text
    assert issue_exists(hass, stuck)


async def test_restart_sent_that_does_not_help(hass: HomeAssistant, loaded: MockConfigEntry, stuck: str) -> None:
    """Acknowledged, and a command succeeds meanwhile (one to the bonding service might): no sign of an answer."""
    coordinator: LSPXCoordinator = loaded.runtime_data

    async def reboot_then_command() -> str:
        hass.loop.call_soon(coordinator.async_set_updated_data, coordinator.data)  # as async_command does
        return "link dropped"

    with (
        patch("custom_components.sony_lspx_p1.repairs.RESTART_WAIT", 0.05),
        patch.object(LSPXClient, "reboot", AsyncMock(side_effect=reboot_then_command)) as reboot,
    ):
        result = await run(hass, await start(hass, stuck))
    reboot.assert_awaited_once()
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "restart_did_not_help")
    assert issue_exists(hass, stuck)


async def test_no_restart_once_it_responds_again(hass: HomeAssistant, stuck: str, projector: FakeProjector) -> None:
    flow_id = await start(hass, stuck)
    projector.busy = False  # RESET pressed meanwhile
    result = await run(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not reboots(projector)
    assert not issue_exists(hass, stuck)


async def test_no_restart_out_of_range(hass: HomeAssistant, stuck: str, projector: FakeProjector) -> None:
    """Out of range or switched off: Restart could not help, so it is not sent."""
    flow_id = await start(hass, stuck)
    with patch("homeassistant.components.bluetooth.async_ble_device_from_address", return_value=None):
        result = await run(hass, flow_id)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "not_reached")
    assert result["description_placeholders"]["name"] == "LSPX-P1 1A2B"
    assert result["description_placeholders"]["error"]
    assert not reboots(projector)
    assert issue_exists(hass, stuck)


async def test_restart_that_cannot_connect(hass: HomeAssistant, stuck: str) -> None:
    with patch.object(LSPXClient, "reboot", AsyncMock(side_effect=BleakError("out of range"))):
        result = await run(hass, await start(hass, stuck))
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "not_reached")
    assert "out of range" in result["description_placeholders"]["error"]
    assert issue_exists(hass, stuck)


async def test_integration_unloaded_meanwhile(hass: HomeAssistant, loaded: MockConfigEntry, stuck: str) -> None:
    flow_id = await start(hass, stuck)
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    result = await run(hass, flow_id)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "not_loaded")


async def test_no_second_restart_while_starting_up(hass: HomeAssistant, loaded: MockConfigEntry, stuck: str) -> None:
    """A start-up under way (Restart pressed here), and the projector still times out: waited for, not restarted."""
    coordinator: LSPXCoordinator = loaded.runtime_data
    with (
        patch("custom_components.sony_lspx_p1.repairs.RESTART_WAIT", 0),
        patch("custom_components.sony_lspx_p1.coordinator.STARTUP_TIMEOUT", 60),
        patch.object(LSPXClient, "reboot", AsyncMock(return_value="acknowledged")) as reboot,
    ):
        await coordinator.async_reboot()
        result = await run(hass, await start(hass, stuck))
        assert coordinator.last_update_success  # the polls keep the power from before while starting up
        reboot.assert_awaited_once()
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "startup_did_not_help")
    assert issue_exists(hass, stuck)  # such a poll is no sign of an answer


async def test_integration_reloaded_during_the_wait(hass: HomeAssistant, loaded: MockConfigEntry, stuck: str) -> None:
    async def reboot_then_reload() -> str:
        # Once Restart is over (its polls scheduled), as an options change would.
        hass.loop.call_soon(lambda: hass.async_create_task(hass.config_entries.async_reload(loaded.entry_id)))
        return "acknowledged"

    with (
        patch("custom_components.sony_lspx_p1.repairs.RESTART_WAIT", 0.2),
        patch.object(LSPXClient, "reboot", AsyncMock(side_effect=reboot_then_reload)),
    ):
        result = await run(hass, await start(hass, stuck))
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "not_loaded")


async def test_integration_unloaded_during_the_first_poll(
    hass: HomeAssistant, loaded: MockConfigEntry, stuck: str, projector: FakeProjector
) -> None:
    """Restart is not sent through a coordinator that was shut down meanwhile (its client would connect again)."""
    refresh = LSPXCoordinator.async_refresh

    async def refresh_then_unload(self: LSPXCoordinator) -> None:
        await refresh(self)
        assert await hass.config_entries.async_unload(loaded.entry_id)

    with patch.object(LSPXCoordinator, "async_refresh", refresh_then_unload):
        result = await run(hass, await start(hass, stuck))
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "not_loaded")
    assert not reboots(projector)
