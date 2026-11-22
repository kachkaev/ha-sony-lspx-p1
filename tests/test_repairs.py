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
    result = await manager(hass).async_init(DOMAIN, context={"issue_id": issue_id})
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
    while not reboots(projector):  # noqa: ASYNC110 - the fake projector has no event for it
        await asyncio.sleep(0.01)
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    result = await manager(hass).async_configure(flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert reboots(projector) == 1
    assert not issue_exists(hass, stuck)
    assert hass.states.get(MP).state == STATE_ON
    assert "Restart from the repair issue sent; the projector responds again" in caplog.text


@pytest.mark.parametrize("wait", ["RESTART_WAIT", "ATTEMPT_LIMIT"])
async def test_restart_that_times_out_too(
    hass: HomeAssistant, stuck: str, projector: FakeProjector, wait: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Reboot answers TIMEOUT like everything else: still waited for (it might restart all the same), then RESET."""
    with patch(f"custom_components.sony_lspx_p1.repairs.{wait}", 0.05):
        result = await run(hass, await start(hass, stuck))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "restart_not_answered"
    assert result["description_placeholders"] == {"name": "LSPX-P1 1A2B"}
    assert reboots(projector) == 1
    assert issue_exists(hass, stuck)  # with its RESET advice, and the flow can be tried again
    assert "timed out (0xC0)" in caplog.text or "not over after" in caplog.text


async def test_restart_sent_that_does_not_help(hass: HomeAssistant, stuck: str, projector: FakeProjector) -> None:
    with (
        patch("custom_components.sony_lspx_p1.repairs.RESTART_WAIT", 0),
        patch.object(LSPXClient, "reboot", AsyncMock(return_value="link dropped")) as reboot,
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
    """Restart pressed already, and the projector still times out: waited for, not restarted again."""
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
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "restart_did_not_help")
    assert issue_exists(hass, stuck)  # such a poll is no sign of an answer
