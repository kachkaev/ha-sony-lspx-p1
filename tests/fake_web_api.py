"""An in-memory stand-in for the projector's HTTP API (as the app's code describes it), through aioclient_mock."""

from __future__ import annotations

import json
import re
from typing import Any

from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse
from yarl import URL

HOST = "192.168.1.50"


class FakeWebApi:
    """Timers, poster settings and the clock.

    ``fail`` maps "METHOD /path" (or "* /path") to a status code or an exception; with ``keep_poster_settings``,
    the projector answers changes of the poster settings with the old value.
    """

    def __init__(self, aioclient_mock: AiohttpClientMocker, host: str = HOST) -> None:
        self.timers: list[dict[str, Any]] = []
        self.poster_resume = True  # the app's default, "Poster"
        self.poster_duration = 10_000  # milliseconds; the app's default, 10 s
        self.keep_poster_settings = False
        self.fail: dict[str, int | BaseException] = {}
        self.requests: list[tuple[str, str, Any]] = []  # (method, path, body)
        self._next_id = 1
        pattern = re.compile(rf"^http://{re.escape(host)}/")
        for method in ("get", "post", "delete"):
            aioclient_mock.request(method, pattern, side_effect=self._handle)

    def add_timer(self, tag: str, at: int, state: str | None = None) -> None:
        state = state or {"timer_on": "on", "timer_off": "off"}[tag]
        self.timers.append({"id": f"id{self._next_id}", "tag": tag, "state": state, "time": at})
        self._next_id += 1

    def calls(self, method: str, path: str) -> list[Any]:
        """Bodies of the requests made with this method to this path (or below it, for "/prefix/*")."""
        prefix = path.removesuffix("*")
        return [
            body
            for m, p, body in self.requests
            if m == method and (p == path or (path.endswith("*") and p.startswith(prefix)))
        ]

    def times(self, tag: str) -> list[int]:
        return sorted(t["time"] for t in self.timers if t["tag"] == tag)

    async def _handle(self, method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        method, path = method.upper(), url.path
        body = json.loads(data) if data else None
        self.requests.append((method, path, body))
        failure = self.fail.get(f"{method} {path}", self.fail.get(f"* {path}"))
        if isinstance(failure, BaseException):
            return AiohttpClientMockResponse(method, url, exc=failure)
        if failure is not None:
            return AiohttpClientMockResponse(method, url, status=failure)
        reply = {"status": "200", "message": "OK", **self._result(method, path, body)}
        return AiohttpClientMockResponse(method, url, json=reply)

    def _result(self, method: str, path: str, body: Any) -> dict[str, Any]:
        if path == "/v1/settings/time":
            return {"result": {"time": body["time"] if body else 0, "timezone": "GMT+0"}}
        if path == "/v1/settings/timers" and method == "GET":
            return {"result": list(self.timers)}
        if path == "/v1/settings/timers" and method == "POST":
            timer = {**body, "id": f"id{self._next_id}"}
            self._next_id += 1
            self.timers.append(timer)
            return {"result": timer}
        if path.startswith("/v1/settings/timers/") and method == "DELETE":
            timer_id = path.rsplit("/", 1)[1]
            self.timers = [t for t in self.timers if t["id"] != timer_id]
            return {}
        if path == "/v1/settings/posterResume":
            if method == "POST" and not self.keep_poster_settings:
                self.poster_resume = body["posterResume"]
            return {"result": {"posterResume": self.poster_resume}}
        if path == "/v2/poster/duration":
            if method == "POST" and not self.keep_poster_settings:
                self.poster_duration = body["value"]
            return {"result": {"value": self.poster_duration}}
        raise AssertionError(f"unexpected request: {method} {path}")
