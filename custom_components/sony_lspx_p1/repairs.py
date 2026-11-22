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
from .coordinator import LSPXCoordinator

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
        self._sent: bool | None = None  # None: Restart not sent; False: it timed out like the other commands

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
        try:
            async with asyncio.timeout(ATTEMPT_LIMIT):
                return await self._try_restart(coordinator, entry.title)
        except TimeoutError:
            pass
        _LOGGER.warning("%s: Restart from the repair issue not over after %.0f s", entry.title, ATTEMPT_LIMIT)
        if answers(coordinator):
            return RECOVERED, {}
        if self._sent is None:
            return "not_reached", {"error": f"no answer within {ATTEMPT_LIMIT:.0f} s"}
        return ("restart_did_not_help" if self._sent else "restart_not_answered"), {}

    async def _try_restart(self, coordinator: LSPXCoordinator, name: str) -> tuple[str, dict[str, str]]:
        # A fresh poll first: Restart only for a projector that still times out the way that raised the issue, not
        # for one that answers again, is out of range or switched off.
        await coordinator.async_refresh()
        if answers(coordinator):
            _LOGGER.warning("%s: responds again; Restart from the repair issue not sent", name)
            return RECOVERED, {}
        last = coordinator.last_exception
        if not coordinator.last_update_success and not isinstance(getattr(last, "__cause__", None), ProjectorBusyError):
            error = describe_error(last.__cause__ or last) if last is not None else "unknown"
            _LOGGER.warning(
                "%s: Restart from the repair issue not sent, the projector cannot be reached: %s", name, error
            )
            return "not_reached", {"error": error}

        recovered = asyncio.Event()

        @callback
        def check() -> None:
            if answers(coordinator):
                recovered.set()

        unsub = coordinator.async_add_listener(check)
        try:
            if coordinator.last_update_success:
                # The poll kept the power from before: a start-up is under way (Restart pressed, or switched on) and
                # its power read timed out. Restart again would only start it over; wait for this one instead.
                _LOGGER.warning("%s: starting up already; Restart from the repair issue not sent again", name)
                self._sent = True
            else:
                try:
                    await coordinator.async_reboot()
                    self._sent = True
                except HomeAssistantError as err:
                    if not isinstance(err.__cause__, ProjectorBusyError):
                        error = describe_error(err.__cause__ or err)
                        _LOGGER.warning("%s: Restart from the repair issue failed: %s", name, error)
                        return "not_reached", {"error": error}
                    self._sent = False  # it timed out too; whether it restarts all the same is not known, so wait
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(RESTART_WAIT):
                    await recovered.wait()
        finally:
            unsub()
        if not recovered.is_set():
            await coordinator.async_refresh()  # the last word, in case no poll came in time
        answer = "sent" if self._sent else "timed out (0xC0)"
        if answers(coordinator):
            _LOGGER.warning("%s: Restart from the repair issue %s; the projector responds again", name, answer)
            return RECOVERED, {}
        _LOGGER.warning(
            "%s: Restart from the repair issue %s; the projector still does not respond (%s)",
            name,
            answer,
            why_not(coordinator),
        )
        return ("restart_did_not_help" if self._sent else "restart_not_answered"), {}


def answers(coordinator: LSPXCoordinator) -> bool:
    """Whether the last poll read the power. While starting up a poll that could not keeps the last one instead."""
    return coordinator.last_update_success and "power" not in coordinator.data.errors


def why_not(coordinator: LSPXCoordinator) -> str:
    """What the last poll got instead of an answer, for the log."""
    if not coordinator.last_update_success and coordinator.last_exception is not None:
        return describe_error(coordinator.last_exception)
    return coordinator.data.errors.get("power", "unknown")
