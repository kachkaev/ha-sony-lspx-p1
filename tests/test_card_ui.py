"""The dashboard card in a real browser, without Home Assistant.

tests/card/harness.html loads the card with a fake `hass` and fake timers; these tests drive it
from headless Chrome over the DevTools protocol (on a pipe, so no ports). Skipped when no Chrome
is found (set CHROME=/path/to/chrome; tools/screenshots/capture.py's find_chrome() looks for one).
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from custom_components.sony_lspx_p1.protocol import POSTER_NAMES

REPO = Path(__file__).resolve().parent.parent
HARNESS = (REPO / "tests" / "card" / "harness.html").as_uri()
TRANSLATIONS = REPO / "custom_components" / "sony_lspx_p1" / "translations"
SOURCES = ["HDMI", *POSTER_NAMES.values()]  # as in the harness
TIMEOUT = 15


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """Override the conftest fixture: these tests need no Home Assistant instance."""


def find_chrome() -> str | None:
    spec = importlib.util.spec_from_file_location("capture", REPO / "tools" / "screenshots" / "capture.py")
    assert spec and spec.loader
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
    try:
        return capture.find_chrome()
    except SystemExit:
        return None


class Browser:
    """Just enough of the DevTools protocol, over --remote-debugging-pipe (fd 3 in, fd 4 out)."""

    def __init__(self, chrome: str) -> None:
        self.profile = tempfile.mkdtemp(prefix="lspx-card-tests-")
        cmd_r, self.cmd_w = os.pipe()
        self.reply_r, reply_w = os.pipe()

        def child_fds() -> None:
            # Move the pipe ends out of the way before placing them on 3 and 4.
            high_r = fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10)
            high_w = fcntl.fcntl(reply_w, fcntl.F_DUPFD, 10)
            os.dup2(high_r, 3)
            os.dup2(high_w, 4)

        args = [
            chrome,
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={self.profile}",
            "--hide-scrollbars",
            "--no-first-run",
            "--allow-file-access-from-files",
        ]
        if sys.platform.startswith("linux"):
            args.append("--no-sandbox")  # CI runners may not allow Chrome's sandbox
        self.proc = subprocess.Popen(
            args,
            preexec_fn=child_fds,  # noqa: PLW1509 - the simplest way to set fds 3 and 4; no threads here
            close_fds=False,  # keep fds 3 and 4 (the pipes themselves are not inherited)
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(cmd_r)
        os.close(reply_w)
        self.buffer = b""
        self.next_id = 0
        self.session: str | None = None
        self.exceptions: list[str] = []
        target = self.send("Target.createTarget", url="about:blank")["targetId"]
        self.session = self.send("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self.send("Runtime.enable")
        self.send("Page.enable")

    def send(self, method: str, **params: Any) -> dict:
        self.next_id += 1
        msg: dict[str, Any] = {"id": self.next_id, "method": method, "params": params}
        if self.session and not method.startswith("Target."):
            msg["sessionId"] = self.session
        os.write(self.cmd_w, json.dumps(msg).encode() + b"\0")
        while True:
            reply = self._receive()
            if reply.get("method") == "Runtime.exceptionThrown":
                details = reply["params"]["exceptionDetails"]
                self.exceptions.append(details.get("exception", {}).get("description") or details["text"])
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def _receive(self) -> dict:
        while b"\0" not in self.buffer:
            if not select.select([self.reply_r], [], [], TIMEOUT)[0]:
                raise TimeoutError("Chrome did not answer")
            chunk = os.read(self.reply_r, 1 << 16)
            if not chunk:
                raise RuntimeError("Chrome exited")
            self.buffer += chunk
        message, self.buffer = self.buffer.split(b"\0", 1)
        return json.loads(message)

    def js(self, expression: str) -> Any:
        """Evaluate `expression` in the page (awaiting it if it is a promise) and return its value."""
        result = self.send("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise RuntimeError(details.get("exception", {}).get("description") or details["text"])
        return result["result"].get("value")

    def open_harness(self) -> None:
        self.send("Page.navigate", url=HARNESS)
        for _ in range(100):
            if self.js("window.T?.ready === true"):
                return
            time.sleep(0.05)
        raise TimeoutError("The harness did not load the card")

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)
        os.close(self.cmd_w)
        os.close(self.reply_r)
        shutil.rmtree(self.profile, ignore_errors=True)


@pytest.fixture(scope="module")
def browser() -> Generator[Browser]:
    chrome = find_chrome()
    if not chrome:
        if os.environ.get("CI"):
            pytest.fail("No Chrome found in CI: set CHROME=/path/to/chrome")
        pytest.skip("No Chrome found: set CHROME=/path/to/chrome")
    b = Browser(chrome)
    yield b
    b.close()


@pytest.fixture
def page(browser: Browser) -> Generator[Browser]:
    """A freshly loaded harness; fails the test on any uncaught error in the page."""
    browser.open_harness()
    browser.exceptions.clear()
    yield browser
    assert browser.exceptions == []


def mount(page: Browser, **options: Any) -> None:
    page.js(f"T.mount({json.dumps(options)})")


def shown(page: Browser) -> dict:
    return page.js("T.shown()")


def calls(page: Browser) -> list[tuple[str, str, dict]]:
    """The service calls so far, as (domain.service, status, data without entity_id)."""
    return [
        (f"{c['domain']}.{c['service']}", c["status"], {k: v for k, v in c["data"].items() if k != "entity_id"})
        for c in page.js("T.calls")
    ]


# --- source chips -----------------------------------------------------------------------------------


def edge_cuts(left: float, strip: dict, shown: float, hidden: float) -> bool:
    """With the strip scrolled to `left`, every edge that hides chips cuts through a chip: one that shows at
    least `shown` px inside the strip and hides at least `hidden` px past its edge."""
    spans = [(c["left"] + strip["scrollLeft"], c["right"] + strip["scrollLeft"]) for c in strip["chips"]]
    right = left + strip["width"]
    if left > 0.5 and not any(a <= left - hidden and b >= left + shown for a, b in spans):
        return False
    return not (
        left < strip["maxScroll"] - 0.5 and not any(b >= right + hidden and a <= right - shown for a, b in spans)
    )


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(("width", "language"), [(320, "en"), (420, "en"), (600, "en"), (420, "ru"), (1200, "en")])
def test_selected_chip_in_view(page: Browser, width: int, language: str, source: str) -> None:
    """The selected chip shows in full and edges that hide chips cut through one (centerSelected)."""
    mount(page, width=width, language=language, source=source)
    strip = page.js("T.chips()")
    edge, shown, hidden = page.js("[EDGE, CUT_SHOWN, CUT_HIDDEN]")
    view = strip["width"]
    chips = strip["chips"]
    (selected,) = (c for c in chips if c["selected"])
    assert selected["source"] == source
    assert selected["left"] >= edge - 0.5, strip
    assert selected["right"] <= view - edge + 0.5, strip

    # Where some scroll position shows the selected chip in full and cuts a chip at each edge that hides
    # chips, the card picks such a position. (For the first and last chips the strip has to sit at its
    # end, and whether the other edge then cuts a chip depends on the chip widths.)
    start = selected["left"] + strip["scrollLeft"]
    end = selected["right"] + strip["scrollLeft"]
    lefts = [x for x in range(int(strip["maxScroll"]) + 1) if start >= x + edge and end <= x + view - edge]
    if any(edge_cuts(x, strip, shown, hidden) for x in lefts):
        assert edge_cuts(strip["scrollLeft"], strip, shown, hidden), strip

    # The fades are on exactly the edges that hide chips.
    hidden_left = any(c["left"] < -0.5 for c in chips)
    hidden_right = any(c["right"] > view + 0.5 for c in chips)
    assert strip["fl"] == (f"{edge}px" if hidden_left else "0px"), strip
    assert strip["fr"] == (f"{edge}px" if hidden_right else "0px"), strip


def test_chips_follow_source_change(page: Browser) -> None:
    """After the projector switches source, the newly selected chip scrolls into view."""
    mount(page, width=320, source="HDMI")
    page.js("T.set({'media_player.lspx_p1': {attributes: {source: 'Window'}}})")
    strip = page.js("T.chips()")
    (selected,) = (c for c in strip["chips"] if c["selected"])
    assert selected["source"] == "Window"
    assert selected["right"] <= strip["width"] - page.js("EDGE") + 0.5, strip
    assert strip["fl"] != "0px"


# --- optimistic updates -----------------------------------------------------------------------------


def test_choice_shows_at_once_and_survives_unrelated_updates(page: Browser) -> None:
    mount(page, manual=True)
    page.js("T.click('[data-source=\"Weather\"]')")
    assert shown(page)["source"] == "Weather"
    assert calls(page) == [("media_player.select_source", "pending", {"source": "Weather"})]

    page.js("T.set({'sensor.lspx_p1_battery': {state: '79'}})")
    assert shown(page)["source"] == "Weather"
    page.js("T.resolve(0)")
    page.js("T.set({'media_player.lspx_p1': {attributes: {volume_level: 0.31}}})")
    assert shown(page)["source"] == "Weather"


def test_choice_settles_on_matching_state(page: Browser) -> None:
    mount(page)
    page.js("T.click('[data-source=\"Weather\"]')")
    assert shown(page)["pending"] == ["source"]
    page.js("T.set({'media_player.lspx_p1': {attributes: {source: 'Weather'}}})")
    assert shown(page)["pending"] == []
    # No longer held: the next state from the projector shows straight away.
    page.js("T.set({'media_player.lspx_p1': {attributes: {source: 'HDMI'}}})")
    assert shown(page)["source"] == "HDMI"


@pytest.mark.parametrize(
    ("act", "key", "before", "hold"),
    [
        ("T.click('[data-source=\"Weather\"]')", "source", "HDMI", "SOURCE_HOLD_MS"),
        ("T.slide('volume', 60)", "volume", "30", "HOLD_MS"),
    ],
)
def test_choice_reverts_after_hold(page: Browser, act: str, key: str, before: str, hold: str) -> None:
    """Held while the call runs, then for the hold time, then back to the projector's value."""
    mount(page, manual=True)
    hold_ms = page.js(hold)
    page.js(act)
    chosen = shown(page)[key]
    assert chosen != before
    page.js("T.tick(60000)")  # the call is still running: keep the choice
    assert shown(page)[key] == chosen
    page.js("T.resolve(0)")
    page.js(f"T.tick({hold_ms} - 100)")
    assert shown(page)[key] == chosen
    page.js("T.tick(200)")
    assert shown(page)[key] == before
    assert shown(page)["pending"] == []


def test_failed_call_reverts_and_notifies(page: Browser) -> None:
    mount(page, manual=True)
    page.js("T.click('[data-source=\"Weather\"]')")
    page.js("T.reject(0, 'Projector did not answer')")
    assert shown(page)["source"] == "HDMI"
    assert shown(page)["pending"] == []
    assert page.js("T.notifications") == ["Projector did not answer"]


# --- lanes ------------------------------------------------------------------------------------------


def test_burst_sends_first_and_last(page: Browser) -> None:
    mount(page, manual=True)
    for source in ["Weather", "Clock 1", "Clock 2", "Window"]:
        page.js(f"T.click('[data-source=\"{source}\"]')")
    assert shown(page)["source"] == "Window"
    assert calls(page) == [("media_player.select_source", "pending", {"source": "Weather"})]
    page.js("T.resolve(0)")
    page.js("T.resolve(1)")
    assert calls(page) == [
        ("media_player.select_source", "resolved", {"source": "Weather"}),
        ("media_player.select_source", "resolved", {"source": "Window"}),
    ]
    assert shown(page)["source"] == "Window"


def test_auto_off_goes_before_the_value(page: Browser) -> None:
    """Unticking Auto focus and then moving focus sends the switch first; other lanes do not wait."""
    mount(page, manual=True)
    page.js("T.toggle('auto_focus', false)")
    page.js("T.slide('focus', 10)")
    page.js("T.slide('volume', 50)")
    state = shown(page)
    assert (state["auto_focus"], state["focus"], state["volume"]) == (False, "10", "50")
    assert calls(page) == [
        ("switch.turn_off", "pending", {}),
        ("media_player.volume_set", "pending", {"volume_level": 0.5}),
    ]
    page.js("T.resolve(0)")
    assert calls(page)[2:] == [("number.set_value", "pending", {"value": 10})]


def test_power_off_waits_and_drops_other_choices(page: Browser) -> None:
    mount(page, manual=True)
    page.js("T.slide('volume', 60)")
    page.js("T.slide('volume', 70)")  # waits behind the first volume call
    page.js("T.click('[data-source=\"Weather\"]')")  # in flight in its own lane
    page.js("T.click('[data-action=power]')")
    assert shown(page)["power"] is False
    assert shown(page)["source"] is None
    assert shown(page)["pending"] == ["power"]
    assert [c[0] for c in calls(page)] == ["media_player.volume_set", "media_player.select_source"]

    page.js("T.resolve(0)")
    assert len(calls(page)) == 2  # the source call is still in flight
    page.js("T.resolve(1)")
    assert calls(page)[2:] == [("media_player.turn_off", "pending", {})]
    page.js("T.resolve(2)")
    assert len(calls(page)) == 3  # the dropped volume (70) is never sent


def test_power_on_goes_first(page: Browser) -> None:
    mount(page, manual=True)
    page.js("T.set({'media_player.lspx_p1': {state: 'off'}})")
    page.js("T.click('[data-action=power]')")
    page.js("T.click('[data-source=\"Weather\"]')")
    assert shown(page)["source"] == "Weather"
    assert calls(page) == [("media_player.turn_on", "pending", {})]
    page.js("T.resolve(0)")
    assert calls(page)[1:] == [("media_player.select_source", "pending", {"source": "Weather"})]


# --- translations -----------------------------------------------------------------------------------


def test_every_language_has_every_word(page: Browser) -> None:
    words = page.js("WORDS")
    assert set(words) == {p.stem for p in TRANSLATIONS.glob("*.json")}
    posters = {"clock" if name.startswith("Clock ") else name for name in POSTER_NAMES.values()}
    for language, table in words.items():
        assert set(table) - {"posters"} == set(words["en"]), language
        if language != "en":
            assert set(table["posters"]) == posters, language
            assert "{n}" in table["posters"]["clock"], language
