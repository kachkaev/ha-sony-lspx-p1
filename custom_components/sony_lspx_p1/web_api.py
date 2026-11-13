"""The projector's HTTP API on its Wi-Fi network: its clock (issue #1) and timers (issue #5), experimental.

As the official app does over Wi-Fi (RowAPI, RowSettingsAPI, DeviceController): plain HTTP on port 80 of the address
GetHttpEndpoint reports, JSON bodies, no authentication. Only aiohttp, no Home Assistant APIs, so tools/lspx_cli.py
can use it too; Home Assistant passes its shared session.
"""

from __future__ import annotations

import asyncio
import calendar
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any

import aiohttp

# The app ignores the port in GetHttpEndpoint and always uses 80 (DeviceController.connect).
HTTP_PORT = 80
TIME_PATH = "/v1/settings/time"
TIMERS_PATH = "/v1/settings/timers"
# The app allows 30 s; a projector on the home network answers far sooner, and nothing should wait that long.
REQUEST_TIMEOUT = 10.0
# What the app sends with the local time (DeviceController.setLocalTime): the time is wall-clock time already.
TIME_ZONE = "GMT+0"
HEADERS = {"Content-Type": "application/json; charset=utf-8", "Connection": "close"}


class WebApiError(Exception):
    """The projector's HTTP API could not be reached or refused the request."""


@dataclass(frozen=True)
class ProjectorTime:
    """SettingsTime as the projector returns it: its wall-clock time as if it were UTC, in Unix seconds."""

    time: int | None
    timezone: str | None


@dataclass(frozen=True)
class Timer:
    """SettingsTimer: the projector switches on or to standby once, at ``time``.

    ``time`` is wall-clock time as Unix seconds, like the clock. The app's two daily timers are seven entries each
    (one per day), tagged TIMER_ON or TIMER_OFF; ``id`` is the projector's, for deleting the entry.
    """

    id: str | None
    tag: str | None
    state: str | None  # "on": switch on, "off": switch to Networked Standby
    time: int | None


# The app's tags and states for its two timers (DeviceController.h()).
TIMER_ON = "timer_on"
TIMER_OFF = "timer_off"
TIMER_STATES = {TIMER_ON: "on", TIMER_OFF: "off"}
# The app writes this many entries, one a day from today, and writes them again at most daily (e0.v.d()).
TIMER_DAYS = 7


def local_unix_seconds(now: datetime) -> int:
    """Wall-clock time in ``now``'s time zone as Unix seconds, as the app sends it (e0.v.a()).

    The projector shows this time as it is, so it has to be sent again whenever the UTC offset changes.
    """
    offset = now.utcoffset() or timedelta(0)
    return int(now.timestamp() + offset.total_seconds())


def wall_clock_text(seconds: int | None) -> str | None:
    """A time as the projector holds it (wall-clock time as Unix seconds), as text without a time zone."""
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None).isoformat()
    except (OverflowError, OSError, ValueError):
        return str(seconds)


def base_url(host: str, port: int = HTTP_PORT) -> str:
    return f"http://{host}" if port == HTTP_PORT else f"http://{host}:{port}"


def describe(err: BaseException) -> str:
    text = str(err).strip()
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


async def _request(session: aiohttp.ClientSession, method: str, url: str, body: dict[str, Any] | None) -> Any:
    """Send one request; returns the reply's ``result``, or None when it has none. Raises WebApiError."""
    data = None if body is None else json.dumps(body, separators=(",", ":"))
    try:
        async with (
            asyncio.timeout(REQUEST_TIMEOUT),
            session.request(method, url, data=data, headers=HEADERS) as response,
        ):
            text = await response.text()
            status = response.status
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise WebApiError(describe(err)) from err
    if not 200 <= status < 300:
        raise WebApiError(f"HTTP {status}")
    try:
        reply = json.loads(text) if text.strip() else None
    except ValueError:
        return None
    if not isinstance(reply, dict):
        return None
    # RowAPIResponse: {"status": "200", "message": ..., "result": ...}. The app only checks "status" for some
    # settings; a status other than 200 is taken as a refusal here.
    if (reply_status := reply.get("status")) is not None and str(reply_status) != "200":
        raise WebApiError(f"status {reply_status}: {reply.get('message')}")
    return reply.get("result")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _projector_time(result: Any) -> ProjectorTime:
    if not isinstance(result, dict):
        return ProjectorTime(None, None)
    return ProjectorTime(_int(result.get("time")), _str(result.get("timezone")))


async def set_time(
    session: aiohttp.ClientSession,
    host: str,
    local_seconds: int,
    *,
    port: int = HTTP_PORT,
) -> ProjectorTime:
    """POST /v1/settings/time; returns what the projector answers with (its time, if it says)."""
    body = {"time": local_seconds, "timezone": TIME_ZONE}
    return _projector_time(await _request(session, "POST", base_url(host, port) + TIME_PATH, body))


async def get_time(session: aiohttp.ClientSession, host: str, *, port: int = HTTP_PORT) -> ProjectorTime:
    """GET /v1/settings/time: the projector's clock (the app also uses this request to see if the API answers)."""
    return _projector_time(await _request(session, "GET", base_url(host, port) + TIME_PATH, None))


async def get_timers(session: aiohttp.ClientSession, host: str, *, port: int = HTTP_PORT) -> list[Timer]:
    """GET /v1/settings/timers: every timer entry (RowSettingsAPI.getTimersList)."""
    result = await _request(session, "GET", base_url(host, port) + TIMERS_PATH, None)
    if not isinstance(result, list):
        raise WebApiError("no list of timers in the reply")
    return [
        Timer(_str(item.get("id")), _str(item.get("tag")), _str(item.get("state")), _int(item.get("time")))
        for item in result
        if isinstance(item, dict)
    ]


async def create_timer(session: aiohttp.ClientSession, host: str, at: int, tag: str, *, port: int = HTTP_PORT) -> None:
    """POST /v1/settings/timers with one entry, as the app's Gson writes a new SettingsTimer (no id yet)."""
    body = {"state": TIMER_STATES[tag], "time": at, "creation": 0, "modification": 0, "tag": tag}
    await _request(session, "POST", base_url(host, port) + TIMERS_PATH, body)


async def delete_timer(session: aiohttp.ClientSession, host: str, timer_id: str, *, port: int = HTTP_PORT) -> None:
    """DELETE /v1/settings/timers/<id>."""
    await _request(session, "DELETE", f"{base_url(host, port)}{TIMERS_PATH}/{timer_id}", None)


def daily_times(today: date, at: dt_time) -> list[int]:
    """The entries the app writes for a daily timer: ``at`` today and on the next six days, as wall-clock seconds.

    Today's entry is written even when ``at`` has passed, as the app does.
    """
    start = calendar.timegm((today.year, today.month, today.day, at.hour, at.minute, 0))
    return [start + day * 86400 for day in range(TIMER_DAYS)]


def timer_time(timers: list[Timer], tag: str) -> dt_time | None:
    """The time of day of a daily timer, from its first entry (as the app reads it); None when it is off."""
    for timer in timers:
        if timer.tag == tag and timer.time is not None:
            return datetime.fromtimestamp(timer.time, UTC).time().replace(second=0, microsecond=0)
    return None


async def set_daily_timer(
    session: aiohttp.ClientSession, host: str, tag: str, today: date, at: dt_time, *, port: int = HTTP_PORT
) -> None:
    """Switch a daily timer on at ``at``, as the app does (DeviceController.setCurrentONAlarm/OFFAlarm).

    The app reads the list, writes TIMER_DAYS new entries and then deletes the old ones with the same tag.
    """
    old = await get_timers(session, host, port=port)
    for at_time in daily_times(today, at):
        await create_timer(session, host, at_time, tag, port=port)
    await _delete_timers(session, host, old, tag, port=port)


async def clear_daily_timer(session: aiohttp.ClientSession, host: str, tag: str, *, port: int = HTTP_PORT) -> None:
    """Switch a daily timer off: delete its entries (DeviceController.deleteONAlarms/deleteOFFAlarms)."""
    await _delete_timers(session, host, await get_timers(session, host, port=port), tag, port=port)


async def _delete_timers(
    session: aiohttp.ClientSession, host: str, timers: list[Timer], tag: str, *, port: int
) -> None:
    # Like the app, try every entry even when one fails; then report the first failure.
    error: WebApiError | None = None
    for timer in timers:
        if timer.tag != tag or not timer.id:
            continue
        try:
            await delete_timer(session, host, timer.id, port=port)
        except WebApiError as err:
            error = error or err
    if error is not None:
        raise error
