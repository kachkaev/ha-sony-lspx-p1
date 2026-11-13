"""The projector's HTTP API on its Wi-Fi network: setting its clock (experimental, issue #1).

As the official app does over Wi-Fi (RowAPI, RowSettingsAPI, DeviceController.setLocalTime): plain HTTP on port 80
of the address GetHttpEndpoint reports, JSON bodies, no authentication. Only aiohttp, no Home Assistant APIs, so
tools/lspx_cli.py can use it too; Home Assistant passes its shared session.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp

# The app ignores the port in GetHttpEndpoint and always uses 80 (DeviceController.connect).
HTTP_PORT = 80
TIME_PATH = "/v1/settings/time"
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


def local_unix_seconds(now: datetime) -> int:
    """Wall-clock time in ``now``'s time zone as Unix seconds, as the app sends it (e0.v.a()).

    The projector shows this time as it is, so it has to be sent again whenever the UTC offset changes.
    """
    offset = now.utcoffset() or timedelta(0)
    return int(now.timestamp() + offset.total_seconds())


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


def _projector_time(result: Any) -> ProjectorTime:
    if not isinstance(result, dict):
        return ProjectorTime(None, None)
    time, timezone = result.get("time"), result.get("timezone")
    return ProjectorTime(
        time if isinstance(time, int) and not isinstance(time, bool) else None,
        timezone if isinstance(timezone, str) else None,
    )


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
