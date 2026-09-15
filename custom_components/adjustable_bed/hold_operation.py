"""The stages a bed-side operation runs through, and the streamer's record of one.

An operation control expresses as a sequence of held bit sets rather than as a
hold intent: the store's arm and slot stages, a mode chord, the disarming press.
The controller declares the program; the streamer runs it and answers whether
the last stage reached its cue.

A program has two tracks. The stages are what a run that succeeds goes through;
the recovery stages are what a failure owes the box instead - which is how a
store that never armed still leaves the box disarmed. A run is on one track or
the other, never picking through a flat list.

A ``CuedStage`` advances on its cue and fails at its own ceiling; a
``PressStage`` is a press, and it advances once its press floor is met, so it
prices no ceiling.
"""

from __future__ import annotations

from asyncio import Future
from dataclasses import dataclass
from enum import StrEnum

from .hold_roster import Control


class OperationOutcome(StrEnum):
    """How one staged operation ended.

    A caller that only wants "did it work" compares against ``COMPLETED``; the
    three failures are distinguished because they call for different words to
    a user and count separately in diagnostics.
    """

    COMPLETED = "completed"
    CEILING = "ceiling"
    PREEMPTED = "preempted"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class CueRequest:
    """How many acknowledgement pulses a stage waits for.

    Bed-neutral in shape and bed-specific in meaning: the streamer never reads a
    pulse itself, it hands the request to the controller's feedback.
    """

    pulses: int


@dataclass(frozen=True, slots=True)
class PressStage:
    """One press inside an operation, which the box acknowledges with nothing.

    It ends when its press floor is met, so it needs no ceiling: there is
    nothing to wait for and so nothing to wait too long for.
    """

    controls: frozenset[Control]


@dataclass(frozen=True, slots=True)
class CuedStage:
    """One held bit set the operation leaves on the box's acknowledgement.

    ``ceiling_ms`` is the backstop for a cue that never arrives, which is the
    only reason a cue-bearing stage ever ends without one.
    """

    controls: frozenset[Control]
    cue: CueRequest
    ceiling_ms: int


OperationStage = PressStage | CuedStage


@dataclass(frozen=True, slots=True)
class OperationProgram:
    """The two tracks one staged operation can run.

    ``recovery`` runs in place of the stages a failure did not reach, so what a
    completed run does and what a failed one owes are two sequences rather than
    one list with a flag per element.
    """

    stages: tuple[OperationStage, ...]
    recovery: tuple[OperationStage, ...] = ()


@dataclass(slots=True)
class StagedOperation:
    """One operation's progress through its program.

    Lifetime is the operation: the streamer holds one while it stages and drops
    it once the last release edge is written.
    """

    stages: tuple[OperationStage, ...]
    recovery_stages: tuple[OperationStage, ...]
    outcome: Future[OperationOutcome]
    index: int = 0
    resume_at: float = 0.0
    ceiling_at: float | None = None
    frames: int = 0
    failed: bool = False

    @property
    def current(self) -> OperationStage | None:
        """Return the stage now expressing, or None once the operation is done."""
        track = self._track
        return track[self.index] if self.index < len(track) else None

    @property
    def done(self) -> bool:
        """Return True once no stage of either track is left to run."""
        return self.current is None

    def controls(self, now: float) -> frozenset[Control]:
        """Return the bits the frame carries, empty during a gap or once done."""
        stage = self.current
        if stage is None or now < self.resume_at:
            return frozenset()
        return stage.controls

    def owed_recovery(self) -> tuple[OperationStage, ...]:
        """Return the recovery stages this operation has not run.

        Carried into the next operation they lead its stages: they are what it
        owes the box, not something a further failure has to ask for.

        A run that reached the end of its own track owes nothing. Between its
        last stage advancing to done and the release edge that settles it, the
        box is already in the state that stage left it in, so a preempting
        operation or a wire release in that window carries no recovery.
        """
        if self.done and not self.failed:
            return ()
        if not self.failed:
            return self.recovery_stages
        return self.recovery_stages[self.index :]

    def note_frame(self, now: float) -> None:
        """Count one frame of the current stage and arm a cued stage's ceiling."""
        self.frames += 1
        stage = self.current
        if isinstance(stage, CuedStage) and self.ceiling_at is None:
            self.ceiling_at = now + stage.ceiling_ms / 1000

    def expired(self, now: float) -> bool:
        """Return True when the current stage passed its ceiling with no cue."""
        return self.ceiling_at is not None and now >= self.ceiling_at

    def advance(self, now: float, gap_s: float) -> None:
        """Leave the current stage for the next one on the track it is running."""
        self._enter(self.index + 1, now, gap_s)

    def fail(self, now: float, gap_s: float) -> None:
        """Mark the operation failed and switch to its recovery track."""
        self.failed = True
        self._enter(0, now, gap_s)

    def _enter(self, index: int, now: float, gap_s: float) -> None:
        """Take up the stage at index, one clear floor after this release edge."""
        self.index = index
        self.resume_at = now + gap_s
        self.ceiling_at = None
        self.frames = 0

    @property
    def _track(self) -> tuple[OperationStage, ...]:
        """Return the sequence this run is on: its stages, or its recovery."""
        return self.recovery_stages if self.failed else self.stages
