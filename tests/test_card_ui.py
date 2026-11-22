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
from custom_components.sony_lspx_p1.select import ROTATION

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
    page.js("T.slide('volume', 60)")
    assert shown(page)["volume"] == "60"
    assert calls(page) == [("media_player.turn_on", "pending", {})]
    page.js("T.resolve(0)")
    assert calls(page)[1:] == [("media_player.volume_set", "pending", {"volume_level": 0.6})]


# --- sources and power ------------------------------------------------------------------------------


def chips_enabled(page: Browser) -> bool:
    """Whether the source chips can be clicked (all of them, or none: anything else fails the test)."""
    states = set(page.js("[...T.card.shadowRoot.querySelectorAll('.chips button')].map((b) => !b.disabled)"))
    assert len(states) == 1, states
    return states.pop()


def test_sources_disabled_in_standby(page: Browser) -> None:
    """In standby the chips are disabled, as volume is: choosing a source does not switch the projector on."""
    mount(page)
    page.js("T.set({'media_player.lspx_p1': {state: 'off'}})")
    assert not chips_enabled(page)
    assert page.js("T.q('input[data-slider=\"volume\"]').disabled")
    page.js("T.click('[data-source=\"Weather\"]')")
    assert calls(page) == []
    assert shown(page)["source"] is None


def test_sources_wait_for_the_projector_after_power_on(page: Browser) -> None:
    """After pressing power the chips stay disabled until the projector reports it is on."""
    mount(page, manual=True)
    page.js("T.set({'media_player.lspx_p1': {state: 'off'}})")
    page.js("T.click('[data-action=power]')")
    assert shown(page)["power"] is True
    assert not page.js("T.q('input[data-slider=\"volume\"]').disabled")  # other controls: at once, as before
    assert not chips_enabled(page)
    page.js("T.click('[data-source=\"Weather\"]')")
    assert calls(page) == [("media_player.turn_on", "pending", {})]

    page.js("T.resolve(0)")
    # Simulated gap: in Home Assistant the media player reports on before the call returns.
    assert not chips_enabled(page)
    page.js("T.set({'media_player.lspx_p1': {state: 'on'}})")
    assert chips_enabled(page)
    page.js("T.click('[data-source=\"Weather\"]')")
    assert calls(page)[1:] == [("media_player.select_source", "pending", {"source": "Weather"})]


def test_sources_disabled_at_once_on_power_off(page: Browser) -> None:
    mount(page, manual=True)
    assert chips_enabled(page)
    page.js("T.click('[data-action=power]')")
    assert calls(page) == [("media_player.turn_off", "pending", {})]
    assert not chips_enabled(page)


def test_sources_disabled_while_unavailable(page: Browser) -> None:
    mount(page)
    page.js("T.set({'media_player.lspx_p1': {state: 'unavailable'}})")
    assert not chips_enabled(page)


def test_source_choice_dropped_when_projector_goes_to_standby(page: Browser) -> None:
    """A source still waiting is not sent once the projector reports standby: it would switch it on."""
    mount(page, manual=True)
    page.js("T.click('[data-source=\"Weather\"]')")
    page.js("T.click('[data-source=\"Window\"]')")  # waits behind Weather
    page.js("T.set({'media_player.lspx_p1': {state: 'off'}})")  # switched off elsewhere
    assert shown(page)["pending"] == []
    page.js("T.resolve(0)")
    assert calls(page) == [("media_player.select_source", "resolved", {"source": "Weather"})]
    page.js("T.set({'media_player.lspx_p1': {state: 'on'}})")
    assert shown(page)["source"] == "HDMI"
    assert len(calls(page)) == 1


def test_source_choice_kept_while_unavailable(page: Browser) -> None:
    """A failed poll (unavailable) does not drop a source still waiting; only standby does."""
    mount(page, manual=True)
    page.js("T.click('[data-source=\"Weather\"]')")
    page.js("T.click('[data-source=\"Window\"]')")  # waits behind Weather
    page.js("T.set({'media_player.lspx_p1': {state: 'unavailable'}})")
    assert shown(page)["pending"] == ["source"]
    page.js("T.set({'media_player.lspx_p1': {state: 'on'}})")
    page.js("T.resolve(0)")
    assert calls(page)[1:] == [("media_player.select_source", "pending", {"source": "Window"})]


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


# --- screen rotation --------------------------------------------------------------------------------

ROTATIONS = ["normal", "rotate_180", "mirror_horizontal", "mirror_vertical"]  # the select's options, as in the harness
ROTATION_JS = """[...T.card.shadowRoot.querySelectorAll('[data-segments=rotation] button')].map((b) => {
  const icon = b.querySelector('ha-icon'), r = b.getBoundingClientRect(), g = icon.getBoundingClientRect();
  return {option: b.dataset.option, selected: b.classList.contains('selected'),
    pressed: b.getAttribute('aria-pressed'), title: b.title,
    label: b.getAttribute('aria-label'), left: r.left, right: r.right, icon: [g.left, g.right],
    name: icon.getAttribute('icon'), transform: getComputedStyle(icon).transform};
})"""


def rotation(page: Browser) -> list[dict]:
    return page.js(ROTATION_JS)


def set_rotation(page: Browser, option: str) -> None:
    page.js(f"T.set({{'select.lspx_p1_screen_rotation': {{state: {json.dumps(option)}}}}})")


def test_rotation_offers_every_option(page: Browser) -> None:
    """The select's four options in its order, each with its own icon, named by the entity's translations."""
    names = json.loads((TRANSLATIONS / "en.json").read_text())["entity"]["select"]["rotation"]["state"]
    page.js(f"const h = T.hass; T.hass = () => ({{...h(), formatEntityState: (s, o) => ({json.dumps(names)})[o]}})")
    mount(page)
    buttons = rotation(page)
    assert [b["option"] for b in buttons] == ROTATIONS == list(ROTATION)
    assert [b["title"] for b in buttons] == [names[o] for o in ROTATIONS]
    assert [b["label"] for b in buttons] == [names[o] for o in ROTATIONS]
    turned = "matrix(-1, 0, 0, -1, 0, 0)"  # rotate(180deg); MDI has no icon for a half turn
    assert [(b["name"], b["transform"]) for b in buttons] == [
        ("mdi:image-outline", "none"),
        ("mdi:image-outline", turned),
        ("mdi:flip-horizontal", "none"),
        ("mdi:flip-vertical", "none"),
    ]


def test_rotation_names_without_translations(page: Browser) -> None:
    mount(page)
    assert [b["title"] for b in rotation(page)] == ["Normal", "Rotate 180", "Mirror horizontal", "Mirror vertical"]


@pytest.mark.parametrize("option", ROTATIONS)
def test_rotation_selectable(page: Browser, option: str) -> None:
    mount(page, manual=True)
    set_rotation(page, "rotate_180" if option == "normal" else "normal")
    page.js(f"T.click('[data-segments=rotation] [data-option={option}]')")
    assert [b["option"] for b in rotation(page) if b["selected"]] == [option]
    assert calls(page) == [("select.select_option", "pending", {"option": option})]


@pytest.mark.parametrize("option", ROTATIONS)
def test_rotation_shows_state(page: Browser, option: str) -> None:
    """Including the mirrored states, which used to leave no button selected."""
    mount(page)
    set_rotation(page, option)
    buttons = rotation(page)
    assert [b["option"] for b in buttons if b["selected"]] == [option]
    assert [b["pressed"] for b in buttons] == ["true" if b["option"] == option else "false" for b in buttons]


@pytest.mark.parametrize("layout", ["standard", "full"])
@pytest.mark.parametrize("width", [280, 320, 420])
def test_rotation_fits(page: Browser, layout: str, width: int) -> None:
    """At phone width the four buttons share one row and each icon stays inside its button."""
    mount(page, layout=layout, width=width)
    buttons = rotation(page)
    left, right = page.js("(() => { const r = T.card.getBoundingClientRect(); return [r.left, r.right]; })()")
    assert buttons[0]["left"] >= left
    assert buttons[-1]["right"] <= right
    for b in buttons:
        assert b["right"] - b["left"] >= 44, b  # a comfortable touch target
        assert b["left"] < b["icon"][0] < b["icon"][1] < b["right"], b


def test_compact_has_no_rotation(page: Browser) -> None:
    mount(page, layout="compact")
    assert rotation(page) == []


# --- picture mode -----------------------------------------------------------------------------------

PICTURE_MODES = ["preset_1", "preset_2", "preset_3", "custom_1", "custom_2"]  # the select's options, as in the harness
PICTURE_MODES_JS = """[...T.card.shadowRoot.querySelectorAll('[data-segments=picture_mode] button')].map((b) => [
  b.dataset.option, b.classList.contains('selected'), b.getAttribute('aria-pressed')])"""


def picture_modes_with(selected: str) -> list[list]:
    return [[o, o == selected, str(o == selected).lower()] for o in PICTURE_MODES]


def test_picture_mode_selected_for_screen_readers(page: Browser) -> None:
    """Like the rotation buttons: the selected mode is pressed, including a choice still waiting."""
    mount(page, layout="full", manual=True)
    assert page.js(PICTURE_MODES_JS) == picture_modes_with("preset_1")
    page.js("T.set({'select.lspx_p1_picture_mode': {state: 'custom_1'}})")
    assert page.js(PICTURE_MODES_JS) == picture_modes_with("custom_1")
    page.js("T.click('[data-segments=picture_mode] [data-option=preset_2]')")
    assert calls(page) == [("select.select_option", "pending", {"option": "preset_2"})]
    assert page.js(PICTURE_MODES_JS) == picture_modes_with("preset_2")


# --- the media player's state arriving late ---------------------------------------------------------

PROJECTOR = "media_player.lspx_p1"
FULL_STATE = {"state": "on", "attributes": {"source": "HDMI", "source_list": SOURCES, "volume_level": 0.3}}
CHIPS_JS = "[...T.card.shadowRoot.querySelectorAll('.chips button')].map((b) => b.dataset.source)"


def set_projector(page: Browser, change: dict) -> None:
    page.js(f"T.set({json.dumps({PROJECTOR: change})})")


def warning(page: Browser) -> str | None:
    return page.js("T.q('.warning')?.textContent ?? null")


def mark(page: Browser) -> None:
    """Tag the rendered card, to tell whether it is built again (see rebuilt)."""
    page.js("T.q('ha-card').dataset.mark = '1'")


def rebuilt(page: Browser) -> bool:
    return page.js("T.q('ha-card').dataset.mark !== '1'")


def test_card_recovers_when_state_arrives(page: Browser) -> None:
    """While Home Assistant starts, the media player can have a registry entry but no state yet."""
    mount(page, states={PROJECTOR: None})
    assert warning(page) == f"Entity not found: {PROJECTOR}"
    set_projector(page, FULL_STATE)
    assert warning(page) is None
    assert page.js(CHIPS_JS) == SOURCES
    assert shown(page)["source"] == "HDMI"
    assert shown(page)["volume"] == "30"


def test_chips_appear_with_source_list(page: Browser) -> None:
    """A restored or unavailable state can lack the source list; the chips appear once it arrives."""
    mount(page, states={PROJECTOR: {"state": "unavailable", "attributes": {"restored": True}}})
    assert page.js(CHIPS_JS) == []
    assert page.js("T.q('.section.sources')") is None
    set_projector(page, FULL_STATE)
    assert page.js(CHIPS_JS) == SOURCES
    assert shown(page)["source"] == "HDMI"
    page.js("T.click('[data-source=\"Weather\"]')")
    assert calls(page) == [("media_player.select_source", "resolved", {"source": "Weather"})]


def test_chips_follow_source_list_change_and_keep_pending_choice(page: Browser) -> None:
    mount(page, manual=True)
    page.js("T.click('[data-source=\"Weather\"]')")
    page.js("T.click('[data-source=\"Window\"]')")  # waits behind Weather
    sources = ["HDMI", "Weather", "Window"]
    set_projector(page, {"attributes": {"source_list": sources}})
    assert page.js(CHIPS_JS) == sources
    assert shown(page)["source"] == "Window"
    assert shown(page)["pending"] == ["source"]
    page.js("T.resolve(0)")
    assert calls(page)[1:] == [("media_player.select_source", "pending", {"source": "Window"})]


def test_ordinary_updates_do_not_rebuild(page: Browser) -> None:
    """A rebuild resets scrolling and interaction, so state updates only refresh the card."""
    mount(page)
    mark(page)
    set_projector(page, {"attributes": {"volume_level": 0.5, "source": "Weather"}})
    set_projector(page, {"state": "unavailable"})
    set_projector(page, {"state": "on"})
    page.js("T.set({'sensor.lspx_p1_battery': {state: '79'}})")
    assert not rebuilt(page)
    set_projector(page, {"attributes": {"source_list": ["HDMI"]}})
    assert rebuilt(page)


def test_rebuild_lets_go_of_open_dialog(page: Browser) -> None:
    """The details dialog goes with a rebuild, and its card stops getting updates."""
    mount(page)
    page.js("T.click('[data-action=details]')")
    assert page.js("T.q('dialog').open && !!T.card._dialogCard")
    set_projector(page, {"attributes": {"source_list": ["HDMI"]}})
    assert page.js("T.q('dialog') === null && T.card._dialogCard === null")


def test_entity_id_shown_as_text(page: Browser) -> None:
    """The warning shows the configured entity id as it is, even one with markup in it."""
    mount(page)
    entity = 'media_player.x<img src=x onerror="window.ran = true">'
    page.js(f"T.card.setConfig({{entity: {json.dumps(entity)}}}); T.idle()")
    assert warning(page) == f"Entity not found: {entity}"
    assert page.js("T.q('img')") is None
    assert page.js("window.ran ?? false") is False


# --- state word and the details dialog's bar --------------------------------------------------------


def state_word(page: Browser, root: str = "T.card.shadowRoot") -> str:
    return page.js(f"{root}.querySelector('[data-ref=state]').textContent")


@pytest.mark.parametrize("layout", ["compact", "standard", "full"])
def test_state_word_after_name(page: Browser, layout: str) -> None:
    """Nothing while on; the card's own words in standby and while unreachable."""
    mount(page, layout=layout)
    assert state_word(page) == ""
    set_projector(page, {"state": "off"})
    assert state_word(page) == "Standby"
    set_projector(page, {"state": "unavailable"})
    assert state_word(page) == "Unavailable"
    set_projector(page, {"state": "on"})
    assert state_word(page) == ""
    # Anything else is not standby: Home Assistant's word for it (the harness has none, hence the raw state).
    set_projector(page, {"state": "unknown"})
    assert state_word(page) == "unknown"


def test_state_word_follows_chosen_power(page: Browser) -> None:
    """Like the controls, the word follows the power state the user chose, before the projector confirms."""
    mount(page, manual=True)
    page.js("T.click('[data-action=power]')")
    assert state_word(page) == "Standby"
    page.js("T.reject(0)")
    assert state_word(page) == ""
    set_projector(page, {"state": "off"})
    page.js("T.click('[data-action=power]')")
    assert state_word(page) == ""
    assert not chips_enabled(page)  # the chips still wait for the projector to report on


def test_state_word_while_starting_up(page: Browser) -> None:
    """While the main board starts up the word says so; a source chosen meanwhile stays chosen (and pulses) until
    the projector shows it, however long that takes, as the integration answers only then."""
    mount(page, manual=True)
    set_projector(page, {"attributes": {"source": None, "starting_up": True}})
    assert state_word(page) == "Starting up"
    assert page.js("T.q('[data-action=power]').title") == "Starting up"
    assert chips_enabled(page)
    page.js("T.click('[data-source=\"Clock 2\"]')")
    page.js("T.tick(60000)")
    assert shown(page)["source"] == "Clock 2"
    assert page.js("T.q('[data-source=\"Clock 2\"]').classList.contains('pending')")
    # Another source while waiting: the latest choice shows, and goes out once the first call is answered.
    page.js("T.click('[data-source=\"Window\"]')")
    assert shown(page)["source"] == "Window"
    assert calls(page) == [("media_player.select_source", "pending", {"source": "Clock 2"})]
    set_projector(page, {"attributes": {"source": "Clock 2", "starting_up": False}})
    page.js("T.resolve(0)")
    assert state_word(page) == ""
    assert shown(page)["source"] == "Window"
    assert calls(page)[1] == ("media_player.select_source", "pending", {"source": "Window"})
    # In standby the word is Standby, whatever starting_up says.
    set_projector(page, {"state": "off", "attributes": {"starting_up": True}})
    assert state_word(page) == "Standby"


def test_dialog_shares_the_cards_choices(page: Browser) -> None:
    """A source chosen on the card shows as chosen in the details dialog, and the other way round."""
    mount(page, manual=True)
    set_projector(page, {"attributes": {"source": None, "starting_up": True}})
    page.js("T.click('[data-source=\"Clock 2\"]')")
    open_dialog(page)
    inner = f"{INNER}.querySelector('.chips button.selected')?.dataset.source"
    assert page.js(inner) == "Clock 2"
    assert state_word(page, INNER) == "Starting up"
    page.js(f"{INNER}.querySelector('[data-source=\"Window\"]').click()")
    page.js("T.idle()")
    assert page.js(inner) == "Window"
    assert shown(page)["source"] == "Window"
    # One lane for both: the dialog's choice waits for the card's call.
    assert len(calls(page)) == 1


DIALOG_CARD = "T.q('dialog').querySelector('sony-lspx-p1-card')"


def close_dialog(page: Browser) -> None:
    page.js(f"{INNER}.querySelector('[data-dialog=close]').click(); T.idle()")
    assert page.js(f"{DIALOG}.open") is False


def test_choice_from_a_closed_dialog_still_reports_its_error(page: Browser) -> None:
    """A source chosen in the dialog during a start-up may wait a minute or two: closing the dialog meanwhile
    still shows why it failed, and so does a choice on the card queued behind it."""
    mount(page, manual=True)
    set_projector(page, {"attributes": {"source": None, "starting_up": True}})
    open_dialog(page)
    page.js(f"{INNER}.querySelector('[data-source=\"Clock 2\"]').click()")
    page.js("T.idle()")
    close_dialog(page)
    page.js("T.reject(0, 'Still starting up')")
    assert page.js("T.notifications") == ["Still starting up"]
    # Chosen in the dialog again, then on the card once it is closed: the card's choice goes out after the dialog's.
    open_dialog(page)
    page.js(f"{INNER}.querySelector('[data-source=\"Clock 2\"]').click()")
    page.js("T.idle()")
    close_dialog(page)
    page.js("T.click('[data-source=\"Window\"]')")
    page.js("T.resolve(1)")
    assert [c[2]["source"] for c in calls(page)] == ["Clock 2", "Clock 2", "Window"]
    page.js("T.reject(2, 'Did not answer')")
    assert page.js("T.notifications") == ["Still starting up", "Did not answer"]


def test_choice_from_a_closed_dialog_goes_out_with_the_latest_hass(page: Browser) -> None:
    """The dialog's card gets no new hass once closed: a choice made in it and sent later goes through the card's."""
    mount(page, manual=True)
    set_projector(page, {"attributes": {"source": None, "starting_up": True}})
    page.js("T.click('[data-source=\"Clock 2\"]')")
    open_dialog(page)
    page.js(f"{INNER}.querySelector('[data-source=\"Window\"]').click()")
    page.js("T.idle()")  # waits for the card's call
    close_dialog(page)
    set_projector(page, {"attributes": {"volume_level": 0.5}})  # a new hass for the card only
    page.js("T.resolve(0)")
    assert calls(page)[1] == ("media_player.select_source", "pending", {"source": "Window"})
    assert page.js("T.calls[1].hass") == page.js("T.card._hass.serial")


def test_closed_dialog_does_not_settle_with_an_old_state(page: Browser) -> None:
    """The dialog's card gets no new states once closed: a choice made later on the card is not dropped by it."""
    mount(page, manual=True)
    set_projector(page, {"attributes": {"volume_level": 0.3}})
    open_dialog(page)
    page.js(f"{DIALOG_CARD}._setValue('volume', 40); T.idle()")
    close_dialog(page)
    set_projector(page, {"attributes": {"volume_level": 0.5}})  # changed elsewhere
    page.js("T.card._setValue('volume', 30); T.idle()")  # waits for the dialog's call
    page.js("T.tick(1000)")
    page.js("T.resolve(0)")
    assert [c[2] for c in calls(page)] == [{"volume_level": 0.4}, {"volume_level": 0.3}]


@pytest.mark.parametrize(
    ("language", "word"), [("de", "Nicht verfügbar"), ("ru", "Недоступно"), ("sv", "Inte tillgänglig")]
)
def test_state_word_translated_and_never_cut(page: Browser, language: str, word: str) -> None:
    """The name gives way to the word: a long name is cut, the word is not."""
    mount(page, layout="compact", width=320, language=language)
    page.js("T.card.setConfig({...T.card._config, name: 'Living room projector by the big window'}); T.idle()")
    set_projector(page, {"state": "unavailable"})
    assert state_word(page) == word
    sizes = page.js(
        "(() => { const n = T.q('[data-ref=name]'), s = T.q('[data-ref=state]');"
        " return {name: [n.scrollWidth, n.clientWidth], state: [s.scrollWidth, s.clientWidth]}; })()"
    )
    assert sizes["name"][0] > sizes["name"][1], sizes  # the name is cut (ellipsis)
    assert sizes["state"][0] <= sizes["state"][1], sizes


@pytest.mark.parametrize("width", [240, 200])
def test_state_word_stays_clear_of_battery(page: Browser, width: int) -> None:
    """On a very narrow card the word is cut once the name is gone, instead of running over the battery."""
    mount(page, layout="compact", width=width, language="de")
    set_projector(page, {"state": "unavailable"})
    edges = page.js(
        "(() => { const s = T.q('[data-ref=state]').getBoundingClientRect(),"
        " b = T.q('[data-ref=battery]').getBoundingClientRect(); return [s.right, b.left]; })()"
    )
    assert edges[0] <= edges[1], edges


DIALOG = "T.q('dialog')"
INNER = f"{DIALOG}.querySelector('sony-lspx-p1-card').shadowRoot"


def open_dialog(page: Browser) -> None:
    page.js("window.moreInfo = []; document.addEventListener('hass-more-info', (e) => moreInfo.push(e.detail))")
    page.js("T.click('[data-action=details]')")
    assert page.js(f"{DIALOG}.open") is True


def test_dialog_bar_holds_battery_and_power(page: Browser) -> None:
    """In the dialog, the card's first line is the dialog's bar: no separate row for battery and power."""
    mount(page, layout="standard")
    open_dialog(page)
    bar = page.js(
        f"[...{INNER}.querySelector('.header').querySelectorAll('[data-dialog], [data-ref], [data-action]')]"
        ".map((el) => el.dataset.dialog || el.dataset.ref || el.dataset.action)"
    )
    assert bar == ["close", "details", "name", "state", "battery", "power", "history", "settings"]
    assert page.js(f"{INNER}.querySelector('[data-ref=name]').textContent") == "LSPX-P1"
    assert page.js(f"{INNER}.querySelector('[data-ref=icon]')") is None
    assert page.js(f"{INNER}.querySelectorAll('.header').length") == 1
    # The dialog draws nothing above the card inside.
    assert page.js(f"{DIALOG}.firstElementChild.className") == "dialog-content"
    set_projector(page, {"state": "off"})
    assert state_word(page, INNER) == "Standby"


def test_dialog_bar_stays_put(page: Browser) -> None:
    mount(page, layout="standard")
    open_dialog(page)
    page.js(f"{DIALOG}.style.maxHeight = '300px'")  # a short window
    content = f"{DIALOG}.querySelector('.dialog-content')"
    assert page.js(f"{content}.scrollHeight > {content}.clientHeight + 100")
    page.js(f"{content}.scrollTop = 100")
    assert page.js(f"{content}.scrollTop") == 100
    tops = page.js(
        f"[{content}.getBoundingClientRect().top, {INNER}.querySelector('.header').getBoundingClientRect().top]"
    )
    assert abs(tops[0] - tops[1]) < 1, tops
    assert page.js(f"{content}.scrollWidth <= {content}.clientWidth")  # nothing sticks out sideways


@pytest.mark.parametrize("width", [320, 360, 390])
@pytest.mark.parametrize(("state", "word"), [("off", "Standby"), ("unavailable", "Nicht verfügbar")])
def test_dialog_bar_fits_on_a_phone(page: Browser, width: int, state: str, word: str) -> None:
    """At phone width the word goes under the name, which keeps the bar's width for the name, and nothing
    spills out. Whether the name and word fit depends on the font: the harness's is wider than Home
    Assistant's (Roboto), which fits both at 320 px, so only the usual phone width is checked for that."""
    page.send("Emulation.setDeviceMetricsOverride", width=width, height=700, deviceScaleFactor=1, mobile=False)
    try:
        mount(page, layout="compact", language="de")
        set_projector(page, {"state": state})
        open_dialog(page)
        sizes = page.js(
            f"(() => {{ const r = {INNER}, rect = (s) => r.querySelector(s).getBoundingClientRect();"
            " const n = r.querySelector('[data-ref=name]'), s = r.querySelector('[data-ref=state]');"
            " return {name: [n.scrollWidth, n.clientWidth], state: [s.scrollWidth, s.clientWidth],"
            " nameBottom: rect('[data-ref=name]').bottom, stateTop: rect('[data-ref=state]').top,"
            " right: rect('[data-dialog=settings]').right, width: rect('.header').right}; })()"
        )
        assert sizes["stateTop"] >= sizes["nameBottom"] - 1, sizes
        assert sizes["right"] <= sizes["width"], sizes
        if width >= 390:
            assert sizes["name"][0] <= sizes["name"][1], sizes
            assert sizes["state"][0] <= sizes["state"][1], sizes
        assert state_word(page, INNER) == word
    finally:
        page.send("Emulation.clearDeviceMetricsOverride")


@pytest.mark.parametrize("button", ["close", "history", "settings"])
def test_dialog_bar_buttons(page: Browser, button: str) -> None:
    """Close closes the dialog; history and settings close it and open Home Assistant's dialog there."""
    mount(page, layout="compact")
    open_dialog(page)
    page.js(f"{INNER}.querySelector('[data-dialog={button}]').click()")
    assert page.js(f"{DIALOG}.open") is False
    expected = [] if button == "close" else [{"entityId": "media_player.lspx_p1", "view": button}]
    assert page.js("moreInfo") == expected


def test_dialog_opens_on_close_button(page: Browser) -> None:
    """showModal() does not look into the card's shadow root: the card puts focus on Close itself."""
    mount(page, layout="standard")
    open_dialog(page)
    assert page.js(f"{INNER}.activeElement?.dataset.dialog") == "close"
    assert page.js(f"{DIALOG}.getAttribute('aria-label')") == "LSPX-P1"
    assert page.js(f"{INNER}.querySelector('[data-dialog=close]').getAttribute('aria-label')") == "Close"


@pytest.mark.parametrize("how", ["click", "Enter"])
def test_dialog_name_opens_entity_dialog(page: Browser, how: str) -> None:
    mount(page, layout="standard")
    open_dialog(page)
    name = f"{INNER}.querySelector('[data-action=details]')"
    if how == "click":
        page.js(f"{name}.click()")
    else:
        page.js(f"{name}.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}}))")
    assert page.js(f"{DIALOG}.open") is False
    assert page.js("moreInfo") == [{"entityId": "media_player.lspx_p1", "view": "info"}]


def test_dialog_battery_opens_battery_dialog(page: Browser) -> None:
    mount(page, layout="standard")
    open_dialog(page)
    page.js(f"{INNER}.querySelector('[data-ref=battery]').click()")
    assert page.js(f"{DIALOG}.open") is False
    assert page.js("moreInfo") == [{"entityId": "sensor.lspx_p1_battery"}]


def test_dialog_power_button(page: Browser) -> None:
    """Power in the bar is the card's own (optimistic, through the lanes), as on the dashboard."""
    mount(page, layout="standard", manual=True)
    open_dialog(page)
    page.js(f"{INNER}.querySelector('[data-action=power]').click()")
    assert calls(page) == [("media_player.turn_off", "pending", {})]
    assert state_word(page, INNER) == "Standby"
    assert page.js(f"{INNER}.querySelector('input[data-slider=volume]').disabled")
