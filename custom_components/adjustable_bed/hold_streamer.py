"""Keeps a bed matching the held set the reconstructor pushes.

One implementation for every ``HoldCapable`` bed, parameterized by the
controller's encoder, its link-bound writer, its timing and - optionally - its
reading of its own notification channel. The streamer owns the wire lifecycle:
paced frames while anything is expressed, the press and clear floors, the
deadlines, the staged operations, and the single release that ends it.

A *wire lifecycle* runs from the first frame after an idle stream to the release
frame that empties it, and the streamer's press bookkeeping belongs to one
lifecycle.

The streamer never reaches back into its controller: it holds one press-floor
lookup, an encoder, a writer, a profile value, a clock and a feedback, and it
calls back into none of them for state. What a bed reports about its own stream -
what it lets the streamer send, and what it lets a stage advance on - arrives
through ``StreamFeedback``; a bed that supplies none gets confirmed writes, one
outstanding, and no cue.

A stop reaches the wire through ``stop`` for named controls and ``release_wire``
for the whole bed: either drops those bits at once, floors met or not. An intent
merely ending - a ttl-0 sample, a lapse - shrinks the target set instead, and the
press it leaves behind drains its unmet floor before its bit goes.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio import Future
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from typing import Any, Protocol

from .hold_intent import Deadline
from .hold_operation import (
    CuedStage,
    CueRequest,
    OperationOutcome,
    OperationProgram,
    OperationStage,
    StagedOperation,
)
from .hold_roster import Control, PressFloor

_LOGGER = logging.getLogger(__name__)

# The link benchmark every roster that wants one declares under this name. The
# streamer needs its identity to tell a bit-empty benchmark frame from a release.
PING = Control("ping")
_PING_ONLY = frozenset({PING})


@dataclass(frozen=True, slots=True)
class StreamProfile:
    """One bed's stream timing, in the units its evidence is quoted in.

    ``frame_interval_ms`` is the emission floor F; ``sustain_window_ms`` is how
    long the box carries a key past the frame that asserted it, and
    ``send_margin_ms`` the margin a sender keeps below that window.

    ``sustain_window_ms`` is a float where every other duration here is an int:
    the CU170's window is the midpoint of a 217-218 ms bracket, and rounding it
    would move a figure the hardware evidence fixes.
    """

    frame_interval_ms: int
    sustain_window_ms: float
    send_margin_ms: int

    def __post_init__(self) -> None:
        """Check that the emission floor stays inside the sustain window.

        ``paced-within-the-sustain-window`` binds send gaps to sustain minus
        margin, and a profile pacing wider than that streams a motor the box
        stops between frames. Making the check the type's own is what keeps all
        three cells load-bearing.

        Raises:
            ValueError: Thrown when the emission floor exceeds the ceiling.
        """
        ceiling_ms = self.sustain_window_ms - self.send_margin_ms
        if self.frame_interval_ms > ceiling_ms:
            raise ValueError(
                f"A frame interval of {self.frame_interval_ms} ms paces wider than the "
                f"{ceiling_ms} ms this bed's sustain window leaves"
            )


@dataclass(slots=True)
class PingRecord:
    """What the link benchmark measured while ``ping`` was the whole held set."""

    began: float
    submissions: int = 0
    completions: int = 0
    round_trip_total_s: float = 0.0
    round_trip_max_s: float = 0.0
    ended_at: float | None = None
    end_reason: str | None = None
    pending: dict[int, float] = field(default_factory=dict)

    def note_submission(self, key: int, now: float) -> None:
        """Record one benchmark frame leaving."""
        self.submissions += 1
        self.pending[key] = now

    def note_completion(self, key: int, now: float) -> None:
        """Record the round trip of the frame submitted under key."""
        submitted = self.pending.pop(key, None)
        if submitted is None:
            return
        round_trip = now - submitted
        self.completions += 1
        self.round_trip_total_s += round_trip
        self.round_trip_max_s = max(self.round_trip_max_s, round_trip)

    def end(self, now: float, reason: str) -> None:
        """Close the record, keeping the first reason it ended for."""
        if self.ended_at is not None:
            return
        self.ended_at = now
        self.end_reason = reason

    @property
    def summary(self) -> dict[str, object]:
        """Return the record as the diagnostics download carries it."""
        return {
            "submissions": self.submissions,
            "completions": self.completions,
            "round_trip_mean_ms": (
                round(self.round_trip_total_s * 1000 / self.completions, 1)
                if self.completions
                else None
            ),
            "round_trip_max_ms": round(self.round_trip_max_s * 1000, 1),
            "ended": self.ended_at is not None,
            "end_reason": self.end_reason,
        }


@dataclass(slots=True)
class PressState:
    """One expressed control's floor bookkeeping, for one press."""

    began: float
    frames: int = 0


@dataclass(frozen=True, slots=True)
class FramePlan:
    """What one wake decided before it acted.

    ``is_release`` and an empty ``controls`` are different facts: a ``ping``-only
    plan carries no bits either, and ends no lifecycle.

    ``attributable`` marks a frame the streamer wants a completion for - a
    release, or a benchmark frame whose round trip is the measurement. It is
    the streamer's own reason for a confirmed write, beside the feedback's.
    """

    controls: frozenset[Control]
    is_release: bool
    attributable: bool


class SendVerdict(StrEnum):
    """What a bed's feedback answers when asked whether this wake may write.

    Three states, not three flags: the eight a flag triple spells include
    combinations no feedback means - a barrier that is not confirmed, or a
    write type on a wake that sends nothing.
    """

    WITHHOLD = "withhold"
    WRITE_COMMAND = "write_command"
    WRITE_REQUEST_BARRIER = "write_request_barrier"


class FrameEncoder(Protocol):
    """Composes one bed's frame from the controls it expresses."""

    def encode(self, controls: frozenset[Control]) -> bytes:
        """Return the frame asserting exactly these controls.

        The empty set is the release frame: on a protocol whose frame is the OR
        of held keys, a release is not a different kind of frame.
        """
        ...


class FrameWriter(Protocol):
    """Submits one frame to one link."""

    def submit(self, frame: bytes) -> None:
        """Send frame as a write nothing learns the fate of."""
        ...

    def submit_confirmed(self, frame: bytes) -> Future[None]:
        """Send frame as a confirmed write, returning its completion.

        The streamer awaits that completion for the barrier and the benchmark,
        and attaches a callback to the release's, which nothing waits on. A
        stop cancels the pump, and with it the completion the pump awaits.
        """
        ...


class StreamFeedback(Protocol):
    """A bed's reading of its own notification channel."""

    def before_send(self, now: float) -> SendVerdict:
        """Return whether this wake may write, and how."""
        ...

    def after_send(self, now: float, *, confirmed: bool) -> None:
        """Account for one frame the streamer submitted."""
        ...

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Account for a confirmed write completing.

        ``sent_since`` counts the frames ``after_send`` reported after that
        write was submitted, whichever write it is: a barrier, a benchmark
        frame or a release.
        """
        ...

    def abandon_barrier(self) -> None:
        """Retire the barrier whose completion the streamer stopped awaiting."""
        ...

    def begin_cue(self, cue: CueRequest) -> None:
        """Start counting toward a stage's cue, discarding what came before."""
        ...

    def cue_met(self) -> bool:
        """Return True once the current cue's evidence has arrived."""
        ...


class _ConfirmedWrites:
    """The default feedback: confirmed writes, one outstanding, no cue.

    What an adopter inherits by declaring no evidence model of its own - the
    round trip is the depth bound, and no stage advances on a cue the bed never
    reports.
    """

    def before_send(self, now: float) -> SendVerdict:
        """Let every wake write, confirmed, one at a time."""
        del now
        return SendVerdict.WRITE_REQUEST_BARRIER

    def after_send(self, now: float, *, confirmed: bool) -> None:
        """Account for nothing: this feedback counts no evidence."""

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Account for nothing."""

    def abandon_barrier(self) -> None:
        """Retire nothing: every barrier here is the pump's own await."""

    def begin_cue(self, cue: CueRequest) -> None:
        """Start no cue: a bed reporting nothing acknowledges nothing."""

    def cue_met(self) -> bool:
        """Return False, so a cue-bearing stage ends at its ceiling."""
        return False


class HoldStreamer:
    """Expresses the held set on one link, for one link's lifetime.

    Owns the target set, the press states, the clear-floor marks, the staged
    operation, the pump task, the benchmark record and the failure that ended
    the stream. It borrows one lookup - a control's press floor - and the
    encoder, the writer, the feedback, the profile and the clock.
    """

    def __init__(
        self,
        *,
        name: str,
        press_floor: Callable[[Control], PressFloor],
        encoder: FrameEncoder,
        writer: FrameWriter,
        profile: StreamProfile,
        clock: Callable[[], float],
        feedback: StreamFeedback | None = None,
        on_sick: Callable[[], None] = lambda: None,
        on_lifecycle_open: Callable[[bytes], None] = lambda frame: None,
    ) -> None:
        """Initialize the streamer for one link.

        ``name`` identifies the bed device in the log, because a pump failure
        is the one thing here a user has to read about and one Home Assistant
        can carry several beds.

        ``on_sick`` is what the controller does once a failed barrier write has
        ended the stream - on this bed, order the disconnect. It runs behind
        the release, and at most once per link.

        ``on_lifecycle_open`` takes the frame that opened a wire lifecycle, for
        a controller that traces one entry per lifecycle rather than per frame.
        The streamer owns that edge, so nothing downstream re-derives it by
        comparing bytes.
        """
        self._name = name
        self._press_floor = press_floor
        self._encoder = encoder
        self._writer = writer
        self._profile = profile
        self._clock = clock
        self._feedback: StreamFeedback = feedback if feedback is not None else _ConfirmedWrites()
        self._on_sick = on_sick
        self._on_lifecycle_open = on_lifecycle_open
        self._target: dict[Control, Deadline] = {}
        self._presses: dict[Control, PressState] = {}
        self._cleared_at: dict[Control, float] = {}
        self._operation: StagedOperation | None = None
        self._owed_recovery: tuple[OperationStage, ...] = ()
        self._pump: asyncio.Task[None] | None = None
        self._benchmark: PingRecord | None = None
        self._failure: Exception | None = None
        self._open = False
        self._sick = False
        self._frames = 0
        self._releases = 0
        self._withheld = 0
        self._outcomes: dict[str, int] = dict.fromkeys(OperationOutcome, 0)

    def hold(self, held: Mapping[Control, Deadline]) -> None:
        """Replace the target set with held, each control mapped to its deadline.

        Synchronous and idempotent: the push replaces the set and returns, so a
        stop can end its intents and reach the wire with no suspension point
        between the two. The frame carrying the change is the next wake's,
        unless a failure has ended this link's stream - a pump that could not
        run, or a failed barrier write. Then the push only replaces the set: no
        wake follows, and no frame leaves until the link ends.
        """
        self._target = dict(held)
        self._start_pump()

    def stop(self, controls: frozenset[Control]) -> None:
        """Drop these controls' presses now, their press floors met or not.

        The per-control half of the stop, called ahead of the push that shrinks
        the target set: dropping the press bookkeeping is what keeps the bit off
        the next frame, because a press left behind drains its unmet floor
        first. Each dropped control takes a clear-floor mark, so a re-press
        waits the same gap a de-assertion would have earned it.
        """
        now = self._clock()
        for control in controls:
            if self._presses.pop(control, None) is not None:
                self._cleared_at[control] = now

    def release_wire(self) -> None:
        """End the wire lifecycle now: drop every bit, release, stop the pump.

        The bed-wide stop and the ordered disconnect both arrive here, so no
        floor delays either. A live operation ends failed and leaves its unrun
        recovery stages owed, which the next operation runs first.
        """
        self._target.clear()
        operation = self._operation
        if operation is not None:
            self._owed_recovery = operation.owed_recovery()
            self._finish_operation(OperationOutcome.RELEASED)
        if self._open:
            self._write_release(self._clock())
        self._stop_pump()

    def link_lost(self) -> None:
        """End the lifecycle because the link died: no frame can leave.

        Everything the streamer owns lives for one link, so it ends here. The
        target and the press bookkeeping go, a live operation ends failed, and
        the pump stops rather than waking against a dead client until every
        deadline lapses. Nothing is owed forward: a possibly-armed box is the
        next link's connect-time disarming press to clear, not this one's.
        """
        self._target.clear()
        self._presses.clear()
        self._open = False
        self._owed_recovery = ()
        self._end_benchmark(self._clock(), "link lost")
        self._finish_operation(OperationOutcome.RELEASED)
        self._stop_pump()

    def stage(self, program: OperationProgram) -> Future[OperationOutcome]:
        """Take up the operation now, returning whether it reaches its last cue.

        Synchronous, so a caller that must put an operation ahead of a push can
        do both without a suspension point between them.

        Held controls are withheld for the operation's span and pressed again
        after its last frame; an operation staged while a press is draining
        opens once that floor is met, at most one press floor later. A second
        request preempts the first, whose unrun recovery stages lead the new
        one, so no re-pressed preset key meets an armed box - unless the new
        operation opens with that same stage, which then runs once rather than
        twice.

        Raises:
            ValueError: Thrown when a stage names a control this bed does not
                declare, which is the bed module's own program disagreeing with
                its own declarations.
            Exception: Thrown at once when an earlier failure ended this link's
                stream - a pump that could not run, or a failed barrier write;
                it is the failure itself, because nothing this link can stage
                runs after it and no wake would ever answer the operation.
        """
        self._refuse_an_unrunnable_program(program)
        preempted = self._operation
        if preempted is not None:
            self._owed_recovery = preempted.owed_recovery()
            self._finish_operation(OperationOutcome.PREEMPTED)

        operation = StagedOperation(
            stages=self._supersede_owed_recovery(program.stages) + program.stages,
            recovery_stages=program.recovery,
            outcome=asyncio.get_running_loop().create_future(),
        )
        self._owed_recovery = ()
        self._operation = operation
        self._begin_stage_cue(operation)
        self._start_pump()
        return operation.outcome

    def _refuse_an_unrunnable_program(self, program: OperationProgram) -> None:
        """Refuse an operation this link cannot carry, before its first frame.

        The pump prices every control it presses on every wake, so a control
        the roster does not declare would end the pump part way through the
        operation, with the caller still waiting on an outcome. Pricing each
        stage here turns that into an answer the caller gets at once.

        Raises:
            ValueError: Thrown when a stage names an undeclared control.
            Exception: Thrown when an earlier failure ended the stream.
        """
        if self._failure is not None:
            raise self._failure
        for stage in (*program.stages, *program.recovery):
            for control in stage.controls:
                try:
                    self._press_floor(control)
                except KeyError as err:
                    raise ValueError(
                        f"Control '{control.name}' is not declared on this bed, so the "
                        "operation naming it cannot be staged"
                    ) from err

    def _supersede_owed_recovery(
        self, stages: tuple[OperationStage, ...]
    ) -> tuple[OperationStage, ...]:
        """Return the owed recovery, less what the incoming operation repeats.

        An incoming operation that opens with the stage the interrupted one
        owed performs it itself, so running both would press the same key twice
        with a release edge between - which is what a bed-wide stop composed
        when it cancelled a store.
        """
        owed = self._owed_recovery
        if owed and stages and owed[0].controls == stages[0].controls:
            return owed[1:]
        return owed

    async def run_operation(self, program: OperationProgram) -> OperationOutcome:
        """Stage the operation and wait for how it ended."""
        return await self.stage(program)

    def _note_sick(self, err: Exception) -> None:
        """End the stream because a failed barrier write lost the link's evidence.

        The write's error becomes the stream's failure, which fences it the way
        a pump failure does: no push restarts the pump, a staged operation is
        refused at once, and no frame follows the release before the link ends.
        The release goes out first, then the controller's exit: only the
        controller owns the link, so ordering the disconnect is its call, and
        the bed sees the button up while the link is still whole.
        """
        self._failure = err
        self._sick = True
        self.release_wire()
        self._on_sick()

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the streamer's state and counters for the diagnostics download."""
        return {
            "expressed": sorted(control.name for control in self._presses),
            "target": sorted(control.name for control in self._target),
            "lifecycle_open": self._open,
            "frames": self._frames,
            "releases": self._releases,
            "withheld_wakes": self._withheld,
            "sick": self._sick,
            "failure": None if self._failure is None else str(self._failure),
            "staging": self._operation is not None,
            "operations": dict(self._outcomes),
            "benchmark": None if self._benchmark is None else self._benchmark.summary,
        }

    def _start_pump(self) -> None:
        """Run the pump, unless one is already running or this link's stream ended.

        A pump that could not compose or submit a frame could not compose the
        next one either, so a later push starts no second pump: one failure per
        link, one log line, and every push after it a no-op until the link ends.
        A failed barrier write ends the stream the same way, because its release
        has gone out and the controller is ending the link.
        """
        if self._failure is not None:
            return
        if self._pump is not None and not self._pump.done():
            return
        self._pump = asyncio.get_running_loop().create_task(
            self._run(), name="adjustable_bed_hold_stream"
        )

    def _stop_pump(self) -> None:
        """Cancel the pump task, unless this call is itself the pump."""
        pump = self._pump
        self._pump = None
        if pump is not None and pump is not asyncio.current_task():
            pump.cancel()

    async def _run(self) -> None:
        """Drive the wire lifecycle, one frame per wake, until nothing is left.

        Anything the loop raises ends the stream here rather than in a task
        exception nothing retrieves: the frame composition and the writer are
        the bed's, so a refusal either of them makes is news, and a caller
        awaiting a staged operation hears it instead of waiting forever.
        """
        try:
            while True:
                now = self._clock()
                self._settle_operation(now)
                plan = self._plan(now)
                sent_at = await self._act(plan, now)
                if self._idle(self._clock()):
                    self._pump = None
                    return
                await self._wait(sent_at)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the error belongs to the bed's own pieces
            self._fail(err)

    def _fail(self, err: Exception) -> None:
        """End the stream because the pump could not run, and answer its caller."""
        self._pump = None
        self._failure = err
        _LOGGER.error("Hold stream on %s stopped: %s", self._name, err)
        operation = self._operation
        self._operation = None
        if operation is not None and not operation.outcome.done():
            operation.outcome.set_exception(err)

    async def _act(self, plan: FramePlan, now: float) -> float | None:
        """Carry out one wake's plan, returning the instant its frame left."""
        if plan.is_release:
            self._write_release(now)
            self._settle_operation(now)
            return now
        if not plan.controls:
            return None
        return await self._emit(plan, now)

    def _idle(self, now: float) -> bool:
        """Return True when the lifecycle is closed and nothing awaits a frame.

        A control still live but gated behind its clear floor keeps the pump
        running: it has a frame coming, just not this wake.
        """
        if self._open or self._operation is not None or self._expressed(now):
            return False
        return not any(deadline > now for deadline in self._target.values())

    async def _emit(self, plan: FramePlan, now: float) -> float | None:
        """Submit one frame, awaiting its completion where the plan is gated."""
        verdict = self._feedback.before_send(now)
        if verdict is SendVerdict.WITHHOLD:
            self._withheld += 1
            return None

        # One line decides the write type: the feedback's barrier, or a frame
        # the streamer itself wants a completion for.
        barrier = verdict is SendVerdict.WRITE_REQUEST_BARRIER
        confirmed = barrier or plan.attributable
        frame = self._encoder.encode(plan.controls)
        future = self._writer.submit_confirmed(frame) if confirmed else None
        if future is None:
            self._writer.submit(frame)
        sent_at = self._clock()
        self._frames += 1
        self._feedback.after_send(sent_at, confirmed=confirmed)
        self._record(plan, frame, sent_at)
        if future is not None:
            await self._settle_write(future, self._frames, barrier=barrier)
        return sent_at

    async def _settle_write(
        self, future: Future[None], frame_number: int, *, barrier: bool
    ) -> None:
        """Wait out a confirmed write, taking the sickness exit if it fails.

        ``frame_number`` is the write's place in this link's frame count, which
        is how its completion tells the feedback what went after it.

        A failed barrier is lost evidence, and lost evidence stops the stream
        (``stop-on-lost-evidence``): the completion that would have cleared the
        gate never arrives, so every later wake is withheld and a held motor
        stops at the box's watchdog with no frame. The failure of any other
        confirmed write - the benchmark's - costs only that measurement.

        A barrier the pump stops awaiting - a stop cancels the pump - is
        abandoned to the feedback: its completion can no longer reach the
        feedback, and a barrier nothing retires would withhold every later wake
        on this link.
        """
        try:
            await future
        except asyncio.CancelledError:
            if barrier:
                self._feedback.abandon_barrier()
            raise
        except Exception as err:  # noqa: BLE001 - the error type belongs to the writer
            _LOGGER.debug("Hold stream write failed: %s", err)
            if barrier:
                _LOGGER.warning(
                    "Hold stream barrier write failed, so the link's evidence is lost: %s",
                    err,
                )
                self._note_sick(err)
            return
        now = self._clock()
        self._feedback.after_confirmation(now, sent_since=self._frames - frame_number)
        if self._benchmark is not None:
            self._benchmark.note_completion(frame_number, now)

    def _write_release(self, now: float) -> None:
        """Write the one zero frame that ends a wire lifecycle."""
        future = self._writer.submit_confirmed(self._encoder.encode(frozenset()))
        self._open = False
        for control in self._presses:
            self._cleared_at[control] = now
        self._presses.clear()
        self._end_benchmark(now, "released")
        future.add_done_callback(partial(self._release_completed, self._frames))

    def _release_completed(self, frames_before: int, future: Future[None]) -> None:
        """Hand the feedback what the release proved, ignoring a completion that failed.

        ``frames_before`` is the frame count at the release's submission, so a
        completion that arrives after a later release still counts every frame
        sent after its own.

        The counter rises here rather than at submission, so the diagnostics
        number reads as the releases that reached the box rather than the ones
        this side handed to the transport.
        """
        if future.cancelled() or future.exception() is not None:
            return
        self._releases += 1
        self._feedback.after_confirmation(self._clock(), sent_since=self._frames - frames_before)

    def _plan(self, now: float) -> FramePlan:
        """Return what this wake expresses, before anything is written."""
        controls = self._expressed(now)
        if not controls:
            return FramePlan(controls=controls, is_release=self._open, attributable=True)
        return FramePlan(
            controls=controls, is_release=False, attributable=controls == _PING_ONLY
        )

    def _expressed(self, now: float) -> frozenset[Control]:
        """Return the controls this wake asserts.

        The target set minus what has lapsed and what a clear floor still gates,
        plus the presses draining an unmet floor - or, while an operation
        stages, the operation's own bits once every held press has drained.
        """
        draining = frozenset(
            control
            for control, press in self._presses.items()
            if not self._floor_met(control, press, now)
        )
        if self._operation is not None:
            return draining or self._operation.controls(now)

        live = {control for control, deadline in self._target.items() if deadline > now}
        pressable = frozenset(
            control
            for control in live
            if control in self._presses or self._clear_floor_met(control, now)
        )
        return pressable | (draining - live)

    def _floor_met(self, control: Control, press: PressState, now: float) -> bool:
        """Return True once both halves of the control's press floor are satisfied."""
        floor = self._press_floor(control)
        return press.frames >= floor.frames and now - press.began >= floor.ms / 1000

    def _clear_floor_met(self, control: Control, now: float) -> bool:
        """Return True once the control's bits have been absent for the clear floor."""
        cleared_at = self._cleared_at.get(control)
        if cleared_at is None:
            return True
        return now - cleared_at >= self._press_floor(control).ms / 1000

    def _record(self, plan: FramePlan, frame: bytes, now: float) -> None:
        """Book one submitted frame against the presses it carried."""
        if not self._open:
            self._open_lifecycle(frame)
        for control in plan.controls:
            press = self._presses.get(control)
            if press is None:
                press = self._presses[control] = PressState(began=now)
            press.frames += 1
        for control in tuple(self._presses):
            if control not in plan.controls:
                del self._presses[control]
                self._cleared_at[control] = now
        operation = self._operation
        if operation is not None and operation.current is not None:
            if plan.controls == operation.current.controls:
                operation.note_frame(now)
        self._track_benchmark(plan, now)

    def _open_lifecycle(self, frame: bytes) -> None:
        """Signal the lifecycle edge, and mark it open.

        The streamer owns the edge; what a controller records of it is the
        controller's.
        """
        self._on_lifecycle_open(frame)
        self._open = True

    def _track_benchmark(self, plan: FramePlan, now: float) -> None:
        """Open the benchmark while ping is the whole plan, and close it after."""
        if plan.controls != _PING_ONLY:
            self._end_benchmark(now, "another control held")
            return
        if self._benchmark is None or self._benchmark.ended_at is not None:
            self._benchmark = PingRecord(began=now)
        self._benchmark.note_submission(self._frames, now)

    def _end_benchmark(self, now: float, reason: str) -> None:
        """Close the benchmark record, if one is open."""
        if self._benchmark is not None:
            self._benchmark.end(now, reason)

    def _settle_operation(self, now: float) -> None:
        """Advance, fail or finish the staged operation before the wake acts."""
        operation = self._operation
        if operation is None:
            return
        if operation.done:
            if not self._open:
                self._finish_operation(
                    OperationOutcome.CEILING if operation.failed else OperationOutcome.COMPLETED
                )
            return
        if operation.frames == 0:
            return

        stage = operation.current
        if stage is None:
            return
        gap_s = self._clear_floor_s(stage)
        if not isinstance(stage, CuedStage):
            if self._stage_floor_met(operation, now):
                operation.advance(now, gap_s)
                self._begin_stage_cue(operation)
        elif self._feedback.cue_met():
            operation.advance(now, gap_s)
            self._begin_stage_cue(operation)
        elif operation.expired(now):
            _LOGGER.debug("Staged operation reached its stage ceiling with no cue")
            operation.fail(now, gap_s)
            self._begin_stage_cue(operation)

    def _begin_stage_cue(self, operation: StagedOperation) -> None:
        """Reset the cue counter before the stage's first frame is submitted."""
        stage = operation.current
        if isinstance(stage, CuedStage):
            self._feedback.begin_cue(stage.cue)

    def _stage_floor_met(self, operation: StagedOperation, now: float) -> bool:
        """Return True once every control of a press stage has met its floor."""
        stage = operation.current
        if stage is None:
            return False
        presses = [
            (control, self._presses[control])
            for control in stage.controls
            if control in self._presses
        ]
        return len(presses) == len(stage.controls) and all(
            self._floor_met(control, press, now) for control, press in presses
        )

    def _clear_floor_s(self, stage: OperationStage) -> float:
        """Return the gap the staging enforces after this stage's release edge."""
        widest = max(
            (self._press_floor(control).ms for control in stage.controls),
            default=0,
        )
        return widest / 1000

    def _finish_operation(self, outcome: OperationOutcome) -> None:
        """Answer the caller awaiting the operation and drop the phase object."""
        operation = self._operation
        self._operation = None
        if operation is None:
            return
        self._outcomes[outcome] += 1
        if not operation.outcome.done():
            operation.outcome.set_result(outcome)

    async def _wait(self, sent_at: float | None) -> None:
        """Sleep to the next wake: one emission floor past this frame."""
        interval = self._profile.frame_interval_ms / 1000
        now = self._clock()
        due = (sent_at if sent_at is not None else now) + interval
        await asyncio.sleep(max(0.0, due - now))
