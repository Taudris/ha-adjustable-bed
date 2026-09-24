"""What the CU170 tells us about its own stream, and what we let it decide.

The box answers each frame it receives with a status notification. That receipt
proves delivery and its absence proves nothing, so it drives exactly two
things: how deep the stream may run ahead of itself (the credit gate), and when
a staged gesture has been acknowledged (the light-pulse counter). Nothing else
reads a receipt, and no elapsed time returns credit.

``OkinStreamFeedback`` puts the two behind the streamer's contract; the
controller feeds it from its notification handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from ..hold_operation import CueRequest
from ..hold_streamer import SendVerdict

_LOGGER = logging.getLogger(__name__)

# Credit never exceeds W, so in-flight depth does not either. At the 100 ms
# emission floor W spans 800 ms of receipt spacing against a healthy p99 of
# 200-310 ms; a window of 4 is marginal under the measured receipt loss.
CREDIT_WINDOW: Final = 8
# Every Kth receipt returns one extra credit, so sustained pace survives receipt
# loss up to 1/(K+1) = 10 %, about twice the measured 2.5-4.5 % band.
CREDIT_EXTRA_EVERY: Final = 9
# The box flashes the under-bed light as feedback, and publishes each change of
# this bit as its own notification. A pulse is two transitions of it.
LIGHT_STATE_BIT: Final = 0x00020000
TRANSITIONS_PER_PULSE: Final = 2

# The first credit stall per bed device per Home Assistant start is worth a
# warning; every later one is a diagnostics counter. Keyed by address because
# the gate is rebuilt per link and a flapping link would otherwise warn per
# reconnect.
_WARNED_ADDRESSES: set[str] = set()


@dataclass(frozen=True, slots=True)
class _Barrier:
    """The confirmed write a stall waits on, and when the stall began.

    ``frame_number`` is the barrier's place in the gate's count of frames sent.
    """

    frame_number: int
    began: float


class ReceiptCreditGate:
    """Bounds how far the stream runs ahead of the box's receipts.

    A send spends one credit and a receipt returns one; every Kth receipt
    returns an extra, and credit never rises above W. At credit 0 the gate calls
    for the barrier: the next frame goes as a Write Request, and nothing leaves
    after it until a confirmed write sent at or after it completes, or the
    streamer abandons it.
    """

    def __init__(self) -> None:
        """Initialize the gate for one link."""
        self._credit = CREDIT_WINDOW
        self._receipts = 0
        self._sent = 0
        self._barrier: _Barrier | None = None
        self._stalled = False
        self._stalls = 0
        self._clears = 0
        self._last_clear_s = 0.0

    def before_send(self, now: float) -> SendVerdict:
        """Return the verdict for this wake.

        While a barrier is outstanding every wake is withheld. Otherwise a
        frame goes as a Write Command while credit lasts, and as the barrier at
        zero.
        """
        if self._barrier is not None:
            return SendVerdict.WITHHOLD
        if self._credit > 0:
            return SendVerdict.WRITE_COMMAND
        self._begin_stall(now)
        return SendVerdict.WRITE_REQUEST_BARRIER

    def after_send(self, now: float, *, confirmed: bool) -> None:
        """Count the frame that just left, and spend one credit on it."""
        del now, confirmed
        self._sent += 1
        self._credit = max(0, self._credit - 1)

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Take what a confirmed write's completion proved.

        ATT delivers a link's PDUs in order, so the completion means that write
        and every frame before it reached the box. Credit rises to the window
        less the frames sent after that write, and never falls. A write sent at
        or after the outstanding barrier also retires it and times the clear. An
        earlier write's completion proves nothing about the barrier, so the
        barrier stands.
        """
        self._credit = max(self._credit, CREDIT_WINDOW - sent_since)
        barrier = self._barrier
        if barrier is not None and self._sent - sent_since >= barrier.frame_number:
            self._barrier = None
            self._clears += 1
            self._last_clear_s = now - barrier.began

    def abandon_barrier(self) -> None:
        """Retire the barrier whose completion the streamer stopped awaiting.

        An abandoned completion proves nothing, so credit does not move and no
        clear is counted: at credit 0 the next wake calls for a new barrier.
        """
        self._barrier = None

    def take_first_stall(self) -> bool:
        """Return True once, on this gate's first stall.

        The gate counts stalls; which bed device stalled, and whether that is
        worth a warning, belongs to the owner that knows its address.
        """
        if not self._stalled:
            return False
        self._stalled = False
        return True

    def note_receipt(self) -> None:
        """Return one credit for a frame the box acknowledged."""
        self._receipts += 1
        extra = 1 if self._receipts % CREDIT_EXTRA_EVERY == 0 else 0
        self._credit = min(CREDIT_WINDOW, self._credit + 1 + extra)

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the gate's state and counters for the diagnostics download."""
        return {
            "credit": self._credit,
            "receipts": self._receipts,
            "barrier_outstanding": self._barrier is not None,
            "stalls": self._stalls,
            "clears": self._clears,
            "last_clear_ms": round(self._last_clear_s * 1000, 1),
        }

    def _begin_stall(self, now: float) -> None:
        """Count a stall and make the next frame its barrier."""
        self._stalls += 1
        self._stalled = self._stalls == 1
        self._barrier = _Barrier(frame_number=self._sent + 1, began=now)


class LightPulseCounter:
    """Counts the light-bit transitions a staged gesture is acknowledged by.

    The box answers a gesture by flashing the under-bed light a set number of
    times: one pulse arms a memory store, two confirm the factory reset, three
    confirm a saved preset, four confirm latch mode. Counting to a threshold
    from each stage's entry is why no burst-idle constant is needed - the
    counter never has to decide when a burst closed, only when it has seen
    enough.
    """

    def __init__(self) -> None:
        """Initialize the counter with no cue waiting."""
        self._transitions = 0
        self._target = 0
        self._previous: bool | None = None

    def begin_cue(self, cue: CueRequest) -> None:
        """Start counting toward cue, discarding what the previous stage saw."""
        self._transitions = 0
        self._target = cue.pulses * TRANSITIONS_PER_PULSE

    def note_led_mask(self, led_mask: int) -> None:
        """Count a transition of the light bit, if this notification carries one."""
        lit = bool(led_mask & LIGHT_STATE_BIT)
        if self._previous is not None and lit != self._previous:
            self._transitions += 1
        self._previous = lit

    def met(self) -> bool:
        """Return True once the cue's transitions have arrived."""
        return self._target > 0 and self._transitions >= self._target

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the counter's state for the diagnostics download."""
        return {"cue_transitions": self._transitions, "cue_target": self._target}


class OkinStreamFeedback:
    """The CU170's reading of its own notification channel.

    It puts the credit gate and the pulse counter behind the streamer's
    contract and lets the controller feed both from one notification.
    """

    def __init__(self, address: str) -> None:
        """Initialize the feedback for one link of the bed at address."""
        self._address = address
        self._credit = ReceiptCreditGate()
        self._pulses = LightPulseCounter()

    def note_notification(self, led_mask: int) -> None:
        """Take one status notification: a receipt, and the light bit it carries."""
        self._credit.note_receipt()
        self._pulses.note_led_mask(led_mask)

    def before_send(self, now: float) -> SendVerdict:
        """Return the gate's verdict, warning at the first stall on a bed device.

        Every later stall is a diagnostics counter. The gate counts them and
        this owner, which knows the address, decides what to say about them.
        """
        verdict = self._credit.before_send(now)
        if self._credit.take_first_stall() and self._address not in _WARNED_ADDRESSES:
            _WARNED_ADDRESSES.add(self._address)
            _LOGGER.warning(
                "Leggett Okin stream ran out of receipt credit on %s; the frame stream "
                "pauses for one confirmed write. Repeated stalls mean the link is not "
                "carrying the motion cadence",
                self._address,
            )
        return verdict

    def after_send(self, now: float, *, confirmed: bool) -> None:
        """Account for one frame the streamer submitted."""
        self._credit.after_send(now, confirmed=confirmed)

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Account for a confirmed write completing, sent_since frames later."""
        self._credit.after_confirmation(now, sent_since=sent_since)

    def abandon_barrier(self) -> None:
        """Retire the barrier whose completion the streamer stopped awaiting."""
        self._credit.abandon_barrier()

    def begin_cue(self, cue: CueRequest) -> None:
        """Start counting toward a stage's cue."""
        self._pulses.begin_cue(cue)

    def cue_met(self) -> bool:
        """Return True once the current cue's pulses have arrived."""
        return self._pulses.met()

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the state and counters behind this feedback."""
        return {**self._credit.diagnostics, **self._pulses.diagnostics}
