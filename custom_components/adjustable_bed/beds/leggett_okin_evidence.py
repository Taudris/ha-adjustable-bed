"""What the CU170 tells us about its own stream, and what we let it decide.

The box answers each frame it receives with a status notification. That receipt
proves delivery and its absence proves nothing, so it drives exactly three
things: how deep the stream may run ahead of itself (the credit gate), when
unacknowledged frames call for a confirmed write that proves the queue drained
(the deficit guard), and when a staged gesture has been acknowledged (the
light-pulse counter). Nothing else reads a receipt, and no elapsed time returns
credit.

``OkinStreamFeedback`` puts the three behind the streamer's contract; the
controller feeds it from its notification handler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
# The deficit leaks one per second. At the trip the next frame is a barrier, and
# any confirmed write's completion lowers the deficit to the frames sent after it.
DEFICIT_LEAK_S: Final = 1.0
DEFAULT_DEFICIT_TRIP: Final = 10
# The box flashes the under-bed light as feedback, and publishes each change of
# this bit as its own notification. A pulse is two transitions of it.
LIGHT_STATE_BIT: Final = 0x00020000
TRANSITIONS_PER_PULSE: Final = 2

# The first credit stall per bed device per Home Assistant start is worth a
# warning; every later one is a diagnostics counter. Keyed by address because
# the gate is rebuilt per link and a flapping link would otherwise warn per
# reconnect.
_WARNED_ADDRESSES: set[str] = set()


class ReceiptCreditGate:
    """Bounds how far the stream runs ahead of the box's receipts.

    A send spends one credit and a receipt returns one; every Kth receipt
    returns an extra, and credit never rises above W. At credit 0 the gate calls
    for the barrier: the next frame goes as a Write Request, whose completion
    proves every earlier frame reached the box's ATT layer.
    """

    def __init__(self) -> None:
        """Initialize the gate for one link."""
        self._credit = CREDIT_WINDOW
        self._receipts = 0
        self._stall_began: float | None = None
        self._stalled = False
        self._stalls = 0
        self._clears = 0
        self._last_clear_s = 0.0

    def before_send(self, now: float) -> SendVerdict:
        """Return a Write Command while credit lasts, and the barrier at zero."""
        if self._credit > 0:
            return SendVerdict.WRITE_COMMAND
        self._begin_stall(now)
        return SendVerdict.WRITE_REQUEST_BARRIER

    def after_send(self, now: float, *, confirmed: bool) -> None:
        """Spend one credit on the frame that just left."""
        del now, confirmed
        self._credit = max(0, self._credit - 1)

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Raise credit by what a confirmed write's completion proved.

        ATT delivers a link's PDUs in order, so the completion means every frame
        before it reached the box. Credit rises to the window less what has gone
        since the write was submitted, and never falls. The first completion
        after a stall also times it.
        """
        self._credit = max(self._credit, CREDIT_WINDOW - sent_since)
        if self._stall_began is not None:
            self._clears += 1
            self._last_clear_s = now - self._stall_began
            self._stall_began = None

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
            "stalls": self._stalls,
            "clears": self._clears,
            "last_clear_ms": round(self._last_clear_s * 1000, 1),
        }

    def _begin_stall(self, now: float) -> None:
        """Count a stall and start timing it."""
        self._stalls += 1
        self._stalled = self._stalls == 1
        self._stall_began = now


class ReceiptDeficitGuard:
    """Counts what the box has not acknowledged, and trips into a barrier.

    Every submitted frame adds one and every receipt clears one; one leaks per
    second, so a healthy stream's transient never accumulates. At the trip the
    guard calls for the barrier: the next frame goes as a Write Request. Any
    confirmed write's completion proves every earlier frame reached the box's
    ATT layer, so it lowers the deficit to the frames sent after that write.
    The stream goes on: a trip is flow control.
    """

    def __init__(self) -> None:
        """Initialize the guard for one link."""
        self._deficit = 0
        self._trip = DEFAULT_DEFICIT_TRIP
        self._last_leak: float | None = None
        self._trips = 0

    def use_trip(self, trip: int) -> None:
        """Adopt the trip the streamer latched for this wire lifecycle."""
        self._trip = trip

    def before_send(self, now: float) -> SendVerdict:
        """Return a Write Command below the trip, and the barrier at it."""
        self._leak(now)
        if self._deficit < self._trip:
            return SendVerdict.WRITE_COMMAND
        self._trips += 1
        return SendVerdict.WRITE_REQUEST_BARRIER

    def note_frame(self, now: float) -> None:
        """Count one frame the box has yet to acknowledge."""
        self._leak(now)
        self._deficit += 1

    def note_receipt(self, now: float) -> None:
        """Clear one frame from the deficit."""
        self._leak(now)
        self._deficit = max(0, self._deficit - 1)

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Lower the deficit to what a confirmed write's completion left unproven.

        Only the frames sent since the write was submitted stay unproven. Which
        write completed does not matter: a credit barrier, a trip barrier and
        the release each prove the same.
        """
        self._leak(now)
        self._deficit = min(self._deficit, sent_since)

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the guard's state and trip counter for the diagnostics download."""
        return {"deficit": self._deficit, "trip": self._trip, "trips": self._trips}

    def _leak(self, now: float) -> None:
        """Forgive one frame per elapsed second."""
        if self._last_leak is None:
            self._last_leak = now
            return
        while now - self._last_leak >= DEFICIT_LEAK_S:
            self._last_leak += DEFICIT_LEAK_S
            self._deficit = max(0, self._deficit - 1)


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

    It puts the credit gate, the deficit guard and the pulse counter behind the
    streamer's contract and lets the controller feed all three from one
    notification. It owns one fact of its own, the barrier both counts call
    for: once either does, nothing further leaves until a confirmed write
    completes or the streamer abandons the barrier's completion.
    """

    def __init__(
        self,
        address: str,
        read_trip: Callable[[], int] = lambda: DEFAULT_DEFICIT_TRIP,
    ) -> None:
        """Initialize the feedback for one link of the bed at address.

        ``read_trip`` is this feedback's own option, read at each lifecycle
        edge rather than handed down: the streamer owns the edge and knows
        nothing about what a bed reads at it.
        """
        self._address = address
        self._credit = ReceiptCreditGate()
        self._deficit = ReceiptDeficitGuard()
        self._pulses = LightPulseCounter()
        self._read_trip = read_trip
        self._barrier_outstanding = False

    def note_notification(self, led_mask: int, now: float) -> None:
        """Take one status notification: a receipt, and the light bit it carries."""
        self._credit.note_receipt()
        self._deficit.note_receipt(now)
        self._pulses.note_led_mask(led_mask)

    def begin_lifecycle(self) -> None:
        """Latch this lifecycle's deficit trip, read at the edge."""
        self._deficit.use_trip(self._read_trip())

    def before_send(self, now: float) -> SendVerdict:
        """Return whether this wake may write, and how.

        While a barrier is outstanding every wake is withheld. Otherwise the
        credit gate answers first, and a Write Command it allows goes as the
        barrier instead once the deficit guard has tripped.
        """
        if self._barrier_outstanding:
            return SendVerdict.WITHHOLD
        verdict = self._credit_verdict(now)
        if verdict is SendVerdict.WRITE_COMMAND:
            verdict = self._deficit_verdict(now)
        self._barrier_outstanding = verdict is SendVerdict.WRITE_REQUEST_BARRIER
        return verdict

    def _credit_verdict(self, now: float) -> SendVerdict:
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

    def _deficit_verdict(self, now: float) -> SendVerdict:
        """Return the guard's verdict, logging a trip at debug.

        A trip is routine flow control, so it is a diagnostics counter and a
        debug line, never a warning.
        """
        verdict = self._deficit.before_send(now)
        if verdict is SendVerdict.WRITE_REQUEST_BARRIER:
            _LOGGER.debug(
                "Leggett Okin stream on %s reached its deficit trip; this frame goes as "
                "a confirmed write, whose completion clears the deficit",
                self._address,
            )
        return verdict

    def after_send(self, now: float, *, confirmed: bool) -> None:
        """Account for one frame the streamer submitted."""
        self._credit.after_send(now, confirmed=confirmed)
        self._deficit.note_frame(now)

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Account for a confirmed write completing, sent_since frames later.

        Any completion retires the barrier, whichever count called for it and
        whichever write completed: each proves every earlier frame reached the
        box, which is all a barrier waits for.
        """
        self._credit.after_confirmation(now, sent_since=sent_since)
        self._deficit.after_confirmation(now, sent_since=sent_since)
        self._barrier_outstanding = False

    def abandon_barrier(self) -> None:
        """Retire the barrier whose completion the streamer stopped awaiting.

        An abandoned completion proves nothing, so neither count moves: the
        next wake asks both afresh, and a count still at its limit calls for a
        new barrier.
        """
        self._barrier_outstanding = False

    def begin_cue(self, cue: CueRequest) -> None:
        """Start counting toward a stage's cue."""
        self._pulses.begin_cue(cue)

    def cue_met(self) -> bool:
        """Return True once the current cue's pulses have arrived."""
        return self._pulses.met()

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the barrier state and every counter behind this feedback."""
        return {
            **self._credit.diagnostics,
            "barrier_outstanding": self._barrier_outstanding,
            **self._deficit.diagnostics,
            **self._pulses.diagnostics,
        }
