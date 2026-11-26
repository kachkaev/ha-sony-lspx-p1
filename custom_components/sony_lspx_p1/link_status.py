"""Why Home Assistant cannot reach the projector, as far as it can tell from outside.

The projector never says that it is busy: a projector with another client connected (Sony's app, which may stay
connected from the background) stops advertising altogether and lets no one else connect, as one that is switched off
or carried away does too. So the status names what Home Assistant sees, and only guesses at the cause where the signal
points one way. Seen on a projector (firmware 3.1.15, 2026-11-26): while Sony's app was connected, no adapter or proxy
heard it for three minutes, at -72 dBm before and after; a poll meanwhile timed out connecting (four tries of 20 s),
and it was heard again a second after the app was closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LinkStatus(StrEnum):
    """The connection sensor's states."""

    OK = "ok"
    # No adapter or proxy hears it: switched off, battery flat, out of range, or another client connected.
    NOT_HEARD = "not_heard"
    # It went quiet while heard well, with a battery that was not running out, less than IN_USE_GUESS_FOR ago: most
    # likely another client is connected, such as Sony's app (an inference; a projector switched off or carried away
    # quickly looks the same).
    IN_USE_ELSEWHERE = "in_use_elsewhere"
    # Heard, but every connection fails (times out): maybe its "Home Assistant" entry is blocked in Sony's app
    # (seen from macOS only), or the link is held elsewhere and the advertisements Home Assistant keeps are not old yet.
    NOT_ACCEPTING = "not_accepting"
    # It rejects Home Assistant (access denied), or drops every link Home Assistant's adapter makes with the bond it
    # holds, as after its entry was deleted in Sony's app: pairing again is needed.
    NOT_PAIRED = "not_paired"
    # Connected, but it does not answer (every command times out): its internal link may be stuck.
    NOT_RESPONDING = "not_responding"
    # Anything else.
    ERROR = "error"


# An advertisement older than this means the projector has gone quiet: it advertises every second or so, and both
# kinds of scanner (an adapter, a proxy) passed on one at least every 4 s in the session above.
QUIET_AFTER = 20.0
# Heard at least this strongly before going quiet: not at the edge of range.
STRONG_SIGNAL = -85
# A battery at or below this, and not charging, may have run out.
LOW_BATTERY = 10
# Quiet for longer than this (seconds), it is more likely off or away than held by a phone all along: no more guessing.
IN_USE_GUESS_FOR = 12 * 3600.0


@dataclass(frozen=True, kw_only=True)
class Observation:
    """What a failed poll saw."""

    not_paired: bool = False  # the projector rejected Home Assistant (access denied)
    busy: bool = False  # connected, but commands timed out
    connect_failed: bool = False  # no link (including no advertisement to connect with)
    stale_bond: bool = False  # links dropped as they were made, while the host holds a bond
    heard_ago: float | None = None  # seconds since an adapter or proxy that can connect last passed one on
    last_signal: int | None = None  # dBm, of that advertisement
    battery_low: bool = False  # last read low and not charging


def classify(seen: Observation) -> LinkStatus:
    """The status for a failed poll."""
    if seen.not_paired or seen.stale_bond:
        return LinkStatus.NOT_PAIRED
    if seen.busy:
        return LinkStatus.NOT_RESPONDING
    if not seen.connect_failed:
        return LinkStatus.ERROR
    if seen.heard_ago is None or seen.heard_ago > QUIET_AFTER:
        strong = seen.last_signal is not None and seen.last_signal >= STRONG_SIGNAL
        recent = seen.heard_ago is not None and seen.heard_ago <= IN_USE_GUESS_FOR
        return LinkStatus.IN_USE_ELSEWHERE if strong and recent and not seen.battery_low else LinkStatus.NOT_HEARD
    return LinkStatus.NOT_ACCEPTING
