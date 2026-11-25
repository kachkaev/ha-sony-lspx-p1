"""The projector's HTTP API (experimental): clock (#1), timers (#5), poster settings (#6).

Paths, JSON bodies and replies as in the official app (RowAPI, RowSettingsAPI, DeviceController). The integration
sends them through the Bluetooth tunnel only (clock.py, web_settings.py, tunnel.py), never over Wi-Fi: the settings
take an Api, which is the tunnel (web_settings.TunnelApi). No Home Assistant APIs.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Protocol

TIME_PATH = "/v1/settings/time"
TIMERS_PATH = "/v1/settings/timers"
POSTER_RESUME_PATH = "/v1/settings/posterResume"  # RowSettingsAPI.SETTINGS_POSTER_RESUME
POSTER_DURATION_PATH = "/v2/poster/duration"  # RowPosterAPI.POSTER_PLAYBACK_INTERVAL, firmware 2.0.0 and later


class WebApiError(Exception):
    """The projector's HTTP API refused the request or answered otherwise than expected."""


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
# Old entries deleted by one write at most (two weeks' worth); any left are deleted by the next.
MAX_TIMER_DELETES = 2 * TIMER_DAYS


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


def parse_reply(status: int, text: str) -> Any:
    """A reply's ``result``, or None when it has none; raises WebApiError for a refusal.

    RowAPIResponse in the app: {"status": "200", "message": ..., "result": ...}. The app only checks "status" for
    some settings; a status other than 200 is taken as a refusal here. Through the tunnel one projector answered
    {"code": 0, "result": ...} instead (firmware 3.1.15, only ever code 0): another code is taken as a refusal too.
    """
    if not 200 <= status < 300:
        raise WebApiError(f"HTTP {status}")
    try:
        reply = json.loads(text) if text.strip() else None
    except ValueError:
        return None
    if not isinstance(reply, dict):
        return None
    if (reply_status := reply.get("status")) is not None and str(reply_status) != "200":
        raise WebApiError(f"status {reply_status}: {reply.get('message')}")
    if (code := reply.get("code")) is not None and str(code) != "0":
        raise WebApiError(f"code {code}: {reply.get('message')}")
    return reply.get("result")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def projector_time(result: Any) -> ProjectorTime:
    if not isinstance(result, dict):
        return ProjectorTime(None, None)
    return ProjectorTime(_int(result.get("time")), _str(result.get("timezone")))


class Api(Protocol):
    """The projector's HTTP API through the Bluetooth tunnel (web_settings.TunnelApi)."""

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Send one request; returns the reply's ``result`` (parse_reply). Raises WebApiError, or the tunnel's own
        errors."""
        ...


async def get_timers(api: Api) -> list[Timer]:
    """GET /v1/settings/timers: every timer entry (RowSettingsAPI.getTimersList)."""
    result = await api.call("GET", TIMERS_PATH)
    if not isinstance(result, list):
        raise WebApiError("no list of timers in the reply")
    return [
        Timer(_str(item.get("id")), _str(item.get("tag")), _str(item.get("state")), _timer_seconds(item.get("time")))
        for item in result
        if isinstance(item, dict)
    ]


def _timer_seconds(value: Any) -> int | None:
    """A timer's time, unless it is not one a date can hold (taken as none)."""
    seconds = _int(value)
    return seconds if seconds is not None and 0 <= seconds < 2**37 else None


async def create_timer(api: Api, at: int, tag: str) -> None:
    """POST /v1/settings/timers with one entry, as the app's Gson writes a new SettingsTimer (no id yet)."""
    await api.call(
        "POST", TIMERS_PATH, {"state": TIMER_STATES[tag], "time": at, "creation": 0, "modification": 0, "tag": tag}
    )


async def delete_timer(api: Api, timer_id: str) -> None:
    """DELETE /v1/settings/timers/<id>."""
    await api.call("DELETE", f"{TIMERS_PATH}/{timer_id}")


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


def timer_changes(timers: list[Timer], tag: str, wanted: list[int]) -> tuple[list[int], list[str]]:
    """What makes ``tag``'s entries exactly ``wanted`` (none: the timer off): the times to create, the ids to delete.

    Entries already there are kept, so writing a timer again only creates what is missing (the app writes all seven
    again) and a write that stopped part way is finished by the next one. Entries with no id cannot be deleted.
    """
    have: set[int] = set()
    delete: list[str] = []
    for timer in timers:
        if timer.tag != tag:
            continue
        if timer.time in wanted and timer.time not in have:
            have.add(timer.time)
        elif timer.id:
            delete.append(timer.id)
    return [at for at in wanted if at not in have], delete


async def write_daily_timer(
    api: Api, tag: str, wanted: list[int], timers: list[Timer] | None = None, *, delete_first: bool = False
) -> None:
    """Make ``tag``'s entries ``wanted`` (daily_times; none to switch the timer off), as the app does: read the list
    (unless ``timers`` was just read), create the new entries, then delete the old ones
    (DeviceController.setCurrentONAlarm, deleteONAlarms).

    Unlike the app, entries already there are kept, at most MAX_TIMER_DELETES are deleted, and the first failure
    stops it: running it again goes on from what the projector then has. Creating
    first keeps a timer that is being changed on throughout; ``delete_first`` (for renewals) keeps the list from
    growing when deleting fails.
    """
    create, delete = timer_changes(await get_timers(api) if timers is None else timers, tag, wanted)
    if delete_first:
        for timer_id in delete[:MAX_TIMER_DELETES]:
            await delete_timer(api, timer_id)
    for at in create:
        await create_timer(api, at, tag)
    if not delete_first:
        for timer_id in delete[:MAX_TIMER_DELETES]:
            await delete_timer(api, timer_id)


async def get_poster_resume(api: Api) -> bool:
    """GET /v1/settings/posterResume: True when the projector starts with a poster, False with the last mode shown."""
    return _poster_resume(await api.call("GET", POSTER_RESUME_PATH))


async def set_poster_resume(api: Api, poster: bool) -> bool:
    """POST /v1/settings/posterResume; like the app, a reply with another value (or none) is a failure."""
    reply = _poster_resume(await api.call("POST", POSTER_RESUME_PATH, {"posterResume": poster}))
    if reply != poster:
        raise WebApiError(f"the projector kept posterResume {reply}")
    return reply


async def get_poster_duration(api: Api) -> int:
    """GET /v2/poster/duration: how long each slideshow photo stays up, in milliseconds."""
    return _poster_duration(await api.call("GET", POSTER_DURATION_PATH))


async def set_poster_duration(api: Api, milliseconds: int) -> int:
    """POST /v2/poster/duration; like the app, a reply with another value (or none) is a failure."""
    reply = _poster_duration(await api.call("POST", POSTER_DURATION_PATH, {"value": milliseconds}))
    if reply != milliseconds:
        raise WebApiError(f"the projector kept a duration of {reply} ms")
    return reply


def _poster_resume(result: Any) -> bool:
    value = result.get("posterResume") if isinstance(result, dict) else None
    if not isinstance(value, bool):
        raise WebApiError("no posterResume in the reply")
    return value


def _poster_duration(result: Any) -> int:
    value = _int(result.get("value")) if isinstance(result, dict) else None
    if value is None:
        raise WebApiError("no duration value in the reply")
    return value
