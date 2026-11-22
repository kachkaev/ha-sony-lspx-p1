"""Repair flow for a projector that stopped answering: try Restart, then say whether it helped.

Whether Restart gets through while every command times out (the stuck internal link the issue is about) has not been
seen: Reboot goes to the system controller, and so does GetPower, whose timeouts raised the issue. So the flow only
tries, and reports what happened; the RESET button stays the advice whenever the projector does not answer again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .client import ProjectorBusyError, describe_error
from .const import DOMAIN
from .coordinator import LSPXConfigEntry, LSPXCoordinator

if TYPE_CHECKING:
    from homeassistant.components.repairs import RepairsFlowResult

_LOGGER = logging.getLogger(__name__)

# After Restart, how long to wait for a poll that reaches the projector. One that got through answered GetPower
# throughout (twice), so the polls Restart schedules (within 30 s) would see it; the main board took about 50 s
# more, which does not matter here. Then one last poll decides.
RESTART_WAIT = 120.0
# The whole attempt, at most: the first poll, Restart (which may wait for a source switch), the wait, the last poll.
ATTEMPT_LIMIT = 300.0

RECOVERED = "recovered"


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    """The projector_stuck issue's flow (the only fixable issue)."""
    assert data is not None  # the issue is created with the entry's id
    return ProjectorStuckRepairFlow(str(data["entry_id"]))


class ProjectorStuckRepairFlow(RepairsFlow):
    """Ask, try Restart while showing progress, then end as repaired or abort with what to do instead.

    An abort keeps the issue, with the RESET advice, and the flow can be started again from it.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._task: asyncio.Task[tuple[str, dict[str, str]]] | None = None
        # How Restart went, once it did: "sent", "timed_out" (0xC0, like the other commands), or "startup" (not sent:
        # a start-up was under way already).
        self._restart: str | None = None

    def _placeholders(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return {"name": entry.title if entry else "", **(extra or {})}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        # Home Assistant may pass the issue's id as input here: ask in a step of its own.
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="confirm", description_placeholders=self._placeholders())
        return await self.async_step_restart()

    async def async_step_restart(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(self._attempt(), f"{DOMAIN} restart from the repair issue")
        if not self._task.done():
            return self.async_show_progress(
                step_id="restart",
                progress_action="restart",
                progress_task=self._task,
                description_placeholders=self._placeholders(),
            )
        return self.async_show_progress_done(next_step_id="done")

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        assert self._task is not None
        outcome, placeholders = self._task.result()
        if outcome == RECOVERED:
            return self.async_create_entry(data={})  # removes the issue, if the poll that recovered has not yet
        return self.async_abort(reason=outcome, description_placeholders=self._placeholders(placeholders))

    async def _attempt(self) -> tuple[str, dict[str, str]]:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return "not_loaded", {}
        coordinator: LSPXCoordinator = entry.runtime_data
        name = entry.title
        began = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(ATTEMPT_LIMIT):
                outcome = await self._try_restart(entry, coordinator, began)
        except TimeoutError:
            _LOGGER.warning("%s: Restart from the repair issue not over after %.0f s", name, ATTEMPT_LIMIT)
            outcome = (RECOVERED if answered(coordinator, began) else "attempt_timed_out"), {}
        except asyncio.CancelledError:
            _LOGGER.warning("%s: Restart from the repair issue stopped before it was over (dialog closed?)", name)
            raise
        if outcome[0] != RECOVERED and (
            entry.state is not ConfigEntryState.LOADED or entry.runtime_data is not coordinator
        ):
            return "not_loaded", {}  # unloaded or reloaded meanwhile: what this coordinator saw no longer counts
        return outcome

    async def _try_restart(
        self, entry: LSPXConfigEntry, coordinator: LSPXCoordinator, began: float
    ) -> tuple[str, dict[str, str]]:
        name = entry.title
        # A fresh poll first: Restart only for a projector that still times out the way that raised the issue, not
        # for one that answers again, is out of range or switched off.
        await coordinator.async_refresh()
        if answered(coordinator, began):
            _LOGGER.warning("%s: responds again; Restart from the repair issue not sent", name)
            return RECOVERED, {}
        last = coordinator.last_exception
        if not coordinator.last_update_success and not isinstance(getattr(last, "__cause__", None), ProjectorBusyError):
            _LOGGER.warning(
                "%s: Restart from the repair issue not sent, cannot reach the projector: %s",
                name,
                describe_error(last.__cause__ or last) if last is not None else "unknown",
            )
            return "not_reached", {"error": shown(last)}

        recovered = asyncio.Event()

        @callback
        def check() -> None:
            if answered(coordinator, began):
                recovered.set()

        unsub = coordinator.async_add_listener(check)
        try:
            if coordinator.last_update_success:
                # The poll kept the power from before: a start-up is under way (Restart pressed, or switched on) and
                # its power read timed out. Restart again would only start it over; wait for this one instead.
                _LOGGER.warning("%s: a start-up is under way already; Restart from the repair issue not sent", name)
                self._restart = "startup"
            elif entry.state is not ConfigEntryState.LOADED or entry.runtime_data is not coordinator:
                return "not_loaded", {}  # unloaded during the poll: its client must not connect again
            else:
                try:
                    await coordinator.async_reboot()
                    self._restart = "sent"
                except HomeAssistantError as err:
                    if not isinstance(err.__cause__, ProjectorBusyError):
                        _LOGGER.warning(
                            "%s: Restart from the repair issue failed: %s", name, describe_error(err.__cause__ or err)
                        )
                        return "not_reached", {"error": shown(err)}
                    self._restart = "timed_out"  # whether it restarts all the same is not known, so wait
                # Logged at once, so that it is on record even if the dialog is closed during the wait.
                _LOGGER.warning(
                    "%s: Restart from the repair issue %s; waiting up to %.0f s for the projector to respond",
                    name,
                    "sent" if self._restart == "sent" else "timed out (0xC0)",
                    RESTART_WAIT,
                )
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(RESTART_WAIT):
                    await recovered.wait()
        finally:
            unsub()
        if not recovered.is_set():
            await coordinator.async_refresh()  # the last word, in case no poll came in time
        if answered(coordinator, began):
            _LOGGER.warning("%s: responds again after Restart from the repair issue", name)
            return RECOVERED, {}
        _LOGGER.warning(
            "%s: still does not respond after Restart from the repair issue (%s)", name, why_not(coordinator)
        )
        reasons = {"sent": "restart_did_not_help", "timed_out": "restart_not_answered"}
        return reasons.get(self._restart or "", "startup_did_not_help"), {}


def answered(coordinator: LSPXCoordinator, since: float) -> bool:
    """Whether a poll read the power since ``since``: one that kept the last power while starting up does not count,
    nor does a command that succeeded (the bonding service answers apart from the projector controls)."""
    return coordinator.power_read_at is not None and coordinator.power_read_at >= since


def shown(err: BaseException | None) -> str:
    """An error as the user sees it: Home Assistant's translated message where there is one."""
    if err is None:
        return "unknown"
    return str(err).strip() or describe_error(err)


def why_not(coordinator: LSPXCoordinator) -> str:
    """What the last poll got instead of an answer, for the log."""
    if not coordinator.last_update_success and coordinator.last_exception is not None:
        return describe_error(coordinator.last_exception)
    return coordinator.data.errors.get("power", "unknown")
