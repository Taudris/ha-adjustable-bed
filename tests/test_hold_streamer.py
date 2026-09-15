"""Tests for the generic hold streamer.

Time is virtual: the streamer's clock is injected and its pump's sleeps are
fired by Home Assistant's time-changed helper, so a wake happens when a test
says it does. The bed is a stub too - a recording writer, a name-listing
encoder and a scripted feedback - so nothing here needs a bed module.
"""

from __future__ import annotations

import asyncio
from asyncio import Future
from collections.abc import Callable, Iterable
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.adjustable_bed.hold_operation import (
    CuedStage,
    CueRequest,
    OperationOutcome,
    OperationProgram,
    PressStage,
    StagedOperation,
)
from custom_components.adjustable_bed.hold_roster import (
    ActionKind,
    ActivateSupport,
    Control,
    ControlDeclaration,
    ControlRoster,
    HoldSupport,
    PressFloor,
)
from custom_components.adjustable_bed.hold_streamer import (
    PING,
    FramePlan,
    HoldStreamer,
    SendVerdict,
    StreamProfile,
)

HEAD_UP = Control("motor-head-up")
FEET_UP = Control("motor-feet-up")
LIGHT = Control("light-toggle")
STORE_1 = Control("store-preset-1")
PRESET_1 = Control("preset-1")
DUMMY = Control("preset-dummy")
# A control no roster here declares, which is what a bed module's program names
# when its stages and its declarations disagree.
UNDECLARED = Control("preset-nowhere")

FRAME_MS = 100
PRESS_MIN_MS = 223
PROFILE = StreamProfile(frame_interval_ms=FRAME_MS, sustain_window_ms=217.5, send_margin_ms=5)
RELEASE = b""


def _roster() -> ControlRoster:
    """Return a synthetic roster with the CU170's floors and no bed module."""
    holds = {ActionKind.HOLD}
    both = {ActionKind.HOLD, ActionKind.ACTIVATE}
    return ControlRoster(
        (
            _declare(HEAD_UP, both, 1000),
            _declare(FEET_UP, both, 1000),
            _declare(LIGHT, {ActionKind.ACTIVATE}, PRESS_MIN_MS),
            _declare(PRESET_1, holds, None),
            _declare(DUMMY, holds, None),
            # The streamer reads only a control's press floor, so the staging
            # controls need no action declaration of the CU170's shape here.
            _declare(STORE_1, holds, None),
            _declare(PING, holds, None, press_min_ms=0),
        )
    )


def _declare(
    control: Control,
    actions: set[ActionKind],
    activate_duration_ms: int | None,
    press_min_ms: int = PRESS_MIN_MS,
) -> ControlDeclaration:
    """Return one synthetic declaration carrying the CU170's press floor."""
    return ControlDeclaration(
        control=control,
        press_floor=PressFloor(frames=1, ms=press_min_ms),
        hold=HoldSupport(ttl_max_ms=30000) if ActionKind.HOLD in actions else None,
        activate=(
            ActivateSupport(duration_ms=activate_duration_ms)
            if activate_duration_ms is not None
            else None
        ),
    )


class _Clock:
    """A monotonic source a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Encoder:
    """Encodes a control set as its sorted names, so a frame reads as its bits."""

    def __init__(self) -> None:
        self.refusal: Exception | None = None

    def encode(self, controls: frozenset[Control]) -> bytes:
        """Return the control names, comma separated; the empty set is the release.

        Raises:
            Exception: Thrown when the test scripted a refusal, which is how the
                CU170's unresolved-revision encoder behaves.
        """
        if self.refusal is not None:
            raise self.refusal
        return ",".join(sorted(control.name for control in controls)).encode()


class _Writer:
    """Records every submission and hands out completions for confirmed writes."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.confirmed: list[bool] = []
        self.pending: list[Future[None]] = []
        self.hold_completions = False
        self.fail_completions = False

    def submit(self, frame: bytes) -> None:
        """Record one unconfirmed frame, whose fate nothing learns."""
        self.frames.append(frame)
        self.confirmed.append(False)

    def submit_confirmed(self, frame: bytes) -> Future[None]:
        """Record one confirmed frame and hand out its completion."""
        self.frames.append(frame)
        self.confirmed.append(True)
        future: Future[None] = asyncio.get_running_loop().create_future()
        self.pending.append(future)
        if self.fail_completions:
            future.set_exception(ConnectionError("the link refused the write"))
        elif not self.hold_completions:
            future.set_result(None)
        return future

    def complete_all(self) -> None:
        """Resolve every completion still outstanding."""
        for future in self.pending:
            if not future.done():
                future.set_result(None)


class _Feedback:
    """A scripted feedback: what it lets through, and what it says a cue met."""

    def __init__(self) -> None:
        self.verdict = SendVerdict.WRITE_COMMAND
        self.cue: CueRequest | None = None
        self.cues_begun: list[CueRequest] = []
        self.cue_is_met = False
        self.sick = False
        self.sends: list[bool] = []
        self.confirmations: list[int] = []
        self.lifecycles = 0

    def begin_lifecycle(self) -> None:
        """Count one lifecycle edge."""
        self.lifecycles += 1

    def before_send(self, now: float) -> SendVerdict:
        """Return the scripted verdict."""
        del now
        return self.verdict

    def after_send(self, now: float, *, confirmed: bool) -> None:
        """Record one submitted frame's write type."""
        del now
        self.sends.append(confirmed)

    def after_confirmation(self, now: float, *, sent_since: int) -> None:
        """Record one completion."""
        del now
        self.confirmations.append(sent_since)

    def is_sick(self, now: float) -> bool:
        """Return the scripted sickness."""
        del now
        return self.sick

    def begin_cue(self, cue: CueRequest) -> None:
        """Start a cue, forgetting whatever the previous stage saw."""
        self.cue = cue
        self.cues_begun.append(cue)
        self.cue_is_met = False

    def cue_met(self) -> bool:
        """Return whether the scripted cue has arrived."""
        return self.cue_is_met


class _Bench:
    """One streamer with its clock, writer, feedback and a hand-driven pump."""

    def __init__(self, hass: HomeAssistant, *, feedback: _Feedback | None) -> None:
        self.hass = hass
        self.clock = _Clock()
        self.writer = _Writer()
        self.encoder = _Encoder()
        self.feedback = feedback
        self.sick_exits = 0
        self.streamer = HoldStreamer(
            name="AA:BB:CC:DD:EE:FF",
            press_floor=_roster().press_floor,
            encoder=self.encoder,
            writer=self.writer,
            profile=PROFILE,
            clock=self.clock,
            feedback=feedback,
            on_sick=self._note_sick_exit,
        )

    def _note_sick_exit(self) -> None:
        """Count the controller's exit, which the streamer runs behind the release."""
        self.sick_exits += 1
        self.exit_frames = list(self.frames)

    def hold(self, *controls: Control, ttl_s: float = 10.0) -> None:
        """Push a held set holding each control for ttl_s from now."""
        self.streamer.hold(dict.fromkeys(controls, self.clock.now + ttl_s))

    @property
    def frames(self) -> list[str]:
        """Return every submitted frame as its comma-separated control names."""
        return [frame.decode() for frame in self.writer.frames]

    async def start(self) -> None:
        """Let the first wake run."""
        await _settle()

    async def tick(self, seconds: float = FRAME_MS / 1000) -> None:
        """Move the clock by seconds and let every wake it uncovers run."""
        self.clock.now += seconds
        async_fire_time_changed(self.hass, dt_util.utcnow() + timedelta(seconds=seconds + 1))
        await _settle()

    async def ticks(self, count: int) -> None:
        """Run count wakes at the emission floor."""
        for _ in range(count):
            await self.tick()

    async def run_until(self, reached: Callable[[], bool], limit: int = 60) -> None:
        """Tick at the emission floor until reached() answers, or give up loudly."""
        for _ in range(limit):
            if reached():
                return
            await self.tick()
        raise AssertionError(f"the streamer did not get there in {limit} wakes")


def _press_runs(frames: list[str], frame: str) -> int:
    """Return how many separate runs of the frame the stream carried."""
    return sum(
        current == frame and previous != frame
        for current, previous in zip(frames, ["", *frames], strict=False)
    )


async def _settle() -> None:
    """Let the pump run to its next sleep."""
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.fixture
async def bench(hass: HomeAssistant):
    """Return a streamer bench with a scripted feedback, released at the end."""
    made = _Bench(hass, feedback=_Feedback())
    yield made
    made.streamer.release_wire()


@pytest.fixture
async def bare_bench(hass: HomeAssistant):
    """Return a streamer bench with no feedback at all."""
    made = _Bench(hass, feedback=None)
    yield made
    made.streamer.release_wire()


class TestFeedbackIsOptional:
    """feedback-is-optional: what a bed declaring no evidence model gets."""

    async def test_a_bed_with_no_feedback_writes_confirmed_one_at_a_time(
        self, bare_bench: _Bench
    ):
        """feedback-is-optional: every frame is confirmed and awaited, and the release lands."""
        bare_bench.hold(HEAD_UP)
        await bare_bench.start()
        await bare_bench.ticks(3)

        assert bare_bench.frames == ["motor-head-up"] * 4
        assert bare_bench.writer.confirmed == [True] * 4

        bare_bench.streamer.hold({})
        await bare_bench.tick()

        assert bare_bench.frames[-1] == ""
        assert bare_bench.writer.confirmed[-1] is True

    async def test_a_bed_with_no_feedback_waits_on_no_cue(self, bare_bench: _Bench):
        """feedback-is-optional: a cue-bearing stage ends at its ceiling, never on a cue."""
        operation = asyncio.ensure_future(
            bare_bench.streamer.run_operation(
                OperationProgram(stages=(CuedStage(frozenset({STORE_1}), CueRequest(1), 500),))
            )
        )
        await bare_bench.start()
        await bare_bench.run_until(operation.done)

        assert await operation is OperationOutcome.CEILING


class TestHaApiOnly:
    """ha-api-only: what the streamer reads of a submitted frame's fate."""

    async def test_an_unconfirmed_frame_yields_no_completion_to_read(self, bench: _Bench):
        """ha-api-only: a Write Command returns nothing, and nothing waits on it."""
        bench.hold(HEAD_UP)
        await bench.start()
        await bench.ticks(2)

        assert bench.writer.confirmed[:3] == [False, False, False]
        assert bench.writer.pending == []

    async def test_only_the_barrier_and_the_release_read_a_completion(self, bench: _Bench):
        """ha-api-only: a confirmed write's completion is read only where the brief names it."""
        assert bench.feedback is not None
        bench.feedback.verdict = SendVerdict.WRITE_REQUEST_BARRIER
        bench.hold(HEAD_UP)
        await bench.start()

        assert bench.feedback.confirmations == [0]

        bench.streamer.release_wire()
        await _settle()

        assert len(bench.feedback.confirmations) == 2

    async def test_no_path_cancels_a_submitted_frame(self, bench: _Bench):
        """ha-api-only: a submitted frame is irrevocable, so no member withdraws one."""
        assert not hasattr(bench.streamer, "cancel")
        assert not any(
            hasattr(future, "revoke") for future in bench.writer.pending
        )


class TestPressRegistrationFloors:
    """press-registration-floors: a press whose expression begins runs its minimum."""

    async def test_a_de_assertion_under_the_minimum_drains_first(self, bench: _Bench):
        """press-registration-floors: the bit stays asserted until both cells are met."""
        bench.hold(HEAD_UP)
        await bench.start()

        bench.clock.now += 0.05
        bench.streamer.hold({})
        await bench.tick(0.05)
        await bench.tick()

        assert bench.frames == ["motor-head-up"] * 3

        await bench.tick()

        assert bench.frames[-1] == RELEASE.decode()

    async def test_expression_never_begins_after_de_assertion(self, bench: _Bench):
        """press-registration-floors: a press de-asserted before its first frame sends none."""
        bench.hold(HEAD_UP)
        bench.streamer.hold({})
        await bench.start()

        assert bench.frames == []

    async def test_the_frame_cell_counts_submissions(self, bench: _Bench):
        """press-registration-floors: floors count frames submitted, not delivered."""
        assert bench.feedback is not None
        bench.hold(LIGHT, ttl_s=0.223)
        await bench.start()

        # One frame is submitted and never confirmed; the time cell is what the
        # press is still waiting on, so the bit rides two more frames.
        assert bench.frames == ["light-toggle"]
        await bench.ticks(2)
        assert bench.frames == ["light-toggle"] * 3
        await bench.tick()
        assert bench.frames[-1] == RELEASE.decode()


class TestFloorsAreInterruptible:
    """floors-are-interruptible: a stop drops the press, floor met or not."""

    async def test_a_stop_mid_drain_drops_the_bit_at_once(self, bench: _Bench):
        """floors-are-interruptible: the next frame is the release, with the floor unmet."""
        bench.hold(HEAD_UP)
        await bench.start()
        bench.streamer.hold({})
        await bench.tick(0.05)

        assert bench.frames == ["motor-head-up"] * 2

        bench.streamer.release_wire()
        await _settle()

        assert bench.frames == ["motor-head-up", "motor-head-up", RELEASE.decode()]

    async def test_no_floor_delays_the_stop(self, bench: _Bench):
        """floors-are-interruptible: the release goes out inside the press minimum."""
        bench.hold(HEAD_UP)
        await bench.start()

        bench.streamer.release_wire()
        await _settle()

        assert bench.frames[-1] == RELEASE.decode()
        assert bench.clock.now - 1000.0 < PRESS_MIN_MS / 1000

    async def test_a_per_control_stop_drops_its_bit_with_the_floor_unmet(
        self, bench: _Bench
    ):
        """floors-are-interruptible: the stopped bit leaves the frame at once."""
        bench.hold(HEAD_UP, FEET_UP)
        await bench.start()

        assert bench.frames == ["motor-feet-up,motor-head-up"]

        bench.streamer.stop(frozenset({HEAD_UP}))
        bench.hold(FEET_UP)
        await bench.tick(0.05)

        assert bench.frames[-1] == "motor-feet-up"
        assert bench.clock.now - 1000.0 < PRESS_MIN_MS / 1000

    async def test_a_shrunken_push_alone_still_drains_the_floor(self, bench: _Bench):
        """floors-are-interruptible: the stop is what drops it, not the push."""
        bench.hold(HEAD_UP, FEET_UP)
        await bench.start()

        bench.hold(FEET_UP)
        await bench.tick(0.05)

        assert bench.frames[-1] == "motor-feet-up,motor-head-up"

    async def test_a_stopped_control_waits_out_its_clear_floor(self, bench: _Bench):
        """floors-are-interruptible: the stop earns the same gap a de-assertion does."""
        bench.hold(HEAD_UP)
        await bench.start()

        bench.streamer.stop(frozenset({HEAD_UP}))
        stopped_at = bench.clock.now
        bench.hold(HEAD_UP)
        await bench.tick()

        assert bench.frames[-1] == RELEASE.decode()

        while bench.clock.now - stopped_at < PRESS_MIN_MS / 1000:
            await bench.tick()

        assert bench.frames[-1] == "motor-head-up"


class TestKeyRegistration:
    """key-registration: a press that runs to completion is not cut short."""

    async def test_a_varying_cadence_does_not_cut_the_press_short(self, bench: _Bench):
        """key-registration: retiming inside the floors changes neither start nor end."""
        bench.hold(HEAD_UP, ttl_s=0.4)
        await bench.start()
        await bench.tick(0.02)
        await bench.tick(0.15)
        await bench.tick(0.1)

        assert bench.frames == ["motor-head-up"] * 4

        await bench.tick(0.2)

        assert bench.frames[-1] == RELEASE.decode()

    async def test_a_stop_before_the_first_frame_emits_none(self, bench: _Bench):
        """key-registration: an unregistered stopped press is the intended outcome."""
        bench.hold(HEAD_UP)
        bench.streamer.release_wire()
        await bench.start()

        assert bench.frames == []


class TestPressStateFidelity:
    """press-state-fidelity: order, count, and the causes a press is lost to."""

    async def test_each_press_is_expressed_once_in_the_order_it_was_held(self, bench: _Bench):
        """press-state-fidelity: two controls, each pressed once, in order."""
        bench.hold(HEAD_UP, ttl_s=0.25)
        await bench.start()
        await bench.ticks(3)
        bench.hold(FEET_UP, ttl_s=0.25)
        await bench.ticks(4)

        pressed = [frame for frame in bench.frames if frame]
        assert pressed[0] == "motor-head-up"
        assert pressed[-1] == "motor-feet-up"
        assert "motor-feet-up,motor-head-up" not in pressed

    async def test_a_stall_splits_a_press_and_it_resumes(self, bench: _Bench):
        """press-state-fidelity: a stalled press resumes rather than restarting."""
        assert bench.feedback is not None
        bench.hold(HEAD_UP, ttl_s=1.0)
        await bench.start()

        bench.feedback.verdict = SendVerdict.WITHHOLD
        await bench.ticks(3)

        assert bench.frames == ["motor-head-up"]
        assert bench.streamer.diagnostics["withheld_wakes"] == 3
        assert bench.streamer.diagnostics["expressed"] == ["motor-head-up"]

        bench.feedback.verdict = SendVerdict.WRITE_COMMAND
        await bench.tick()

        # The press kept its start, so its floor is long met and the next
        # de-assertion drops the bit at once rather than draining again.
        assert bench.frames == ["motor-head-up"] * 2
        bench.streamer.hold({})
        await bench.tick()
        assert bench.frames[-1] == RELEASE.decode()


class TestFrameIsTheOr:
    """frame-is-the-or: the expressed set is exactly what the encoder composes."""

    async def test_the_frame_carries_the_whole_expressed_set(self, bench: _Bench):
        """frame-is-the-or: no frame carries a bit outside the expressed set."""
        bench.hold(HEAD_UP, FEET_UP)
        await bench.start()

        assert bench.frames == ["motor-feet-up,motor-head-up"]

    async def test_a_clear_floor_gated_control_is_absent_from_the_frame(self, bench: _Bench):
        """frame-is-the-or: the expressed set is the held set minus a declared gate."""
        bench.hold(LIGHT, ttl_s=0.3)
        await bench.start()
        await bench.ticks(4)

        assert bench.frames[-1] == RELEASE.decode()

        bench.hold(LIGHT, HEAD_UP)
        await bench.tick()

        assert bench.frames[-1] == "motor-head-up"

    async def test_exhausted_credit_withholds_the_send_never_a_bit(self, bench: _Bench):
        """frame-is-the-or: the expressed set is unchanged at credit 0 and no release fires."""
        assert bench.feedback is not None
        bench.hold(HEAD_UP)
        await bench.start()
        bench.feedback.verdict = SendVerdict.WITHHOLD
        await bench.ticks(4)

        assert bench.frames == ["motor-head-up"]
        assert bench.streamer.diagnostics["releases"] == 0
        assert bench.streamer.diagnostics["lifecycle_open"] is True


class TestDeficitStopsTheStream:
    """deficit-stops-the-stream: the release, then the controller's own exit."""

    async def test_a_failed_barrier_takes_the_sickness_exit(self, bench: _Bench):
        """stop-on-lost-evidence: the completion that clears the gate never comes.

        Without the exit the gate stays shut for the rest of the lifecycle, so
        a held motor stops at the box's watchdog with no frame and no trip.
        """
        feedback = bench.feedback
        assert feedback is not None
        feedback.verdict = SendVerdict.WRITE_REQUEST_BARRIER
        bench.writer.fail_completions = True
        bench.hold(HEAD_UP)

        await bench.start()

        assert bench.sick_exits == 1
        assert bench.streamer.diagnostics["sick"] is True

    async def test_a_failed_benchmark_write_costs_only_its_measurement(
        self, bench: _Bench
    ):
        """stop-on-lost-evidence: the benchmark's round trip is not the gate's evidence."""
        bench.writer.fail_completions = True
        bench.hold(PING)

        await bench.start()
        await bench.ticks(2)

        assert bench.sick_exits == 0
        assert bench.streamer.diagnostics["sick"] is False

    async def test_a_sick_stream_releases_and_then_tells_the_controller(
        self, bench: _Bench
    ):
        """deficit-stops-the-stream: one exit, behind the release, once per link."""
        feedback = bench.feedback
        assert feedback is not None
        bench.hold(HEAD_UP)
        await bench.start()

        feedback.sick = True
        await bench.tick()

        assert bench.sick_exits == 1
        assert bench.exit_frames[-1] == RELEASE.decode()

        bench.hold(HEAD_UP)
        await bench.ticks(3)

        assert bench.sick_exits == 1


class TestLinkLostTeardown:
    """link-lost-teardown, release-before-disconnect: nothing more can leave."""

    async def test_a_lost_link_writes_no_further_frame(self, bench: _Bench):
        """link-lost-teardown, release-before-disconnect: no release is written."""
        bench.hold(HEAD_UP)
        await bench.start()
        frames_before = len(bench.frames)

        bench.streamer.link_lost()
        await bench.ticks(3)

        assert len(bench.frames) == frames_before
        assert bench.streamer.diagnostics["releases"] == 0
        assert bench.streamer.diagnostics["lifecycle_open"] is False

    async def test_a_lost_link_fails_the_staged_operation(self, bench: _Bench):
        """link-lost-teardown, release-before-disconnect: the caller wakes at the drop."""
        operation = asyncio.ensure_future(bench.streamer.run_operation(_store_program()))
        await bench.start()

        bench.streamer.link_lost()
        await _settle()

        assert operation.done()
        assert await operation is OperationOutcome.RELEASED

    async def test_a_lost_link_owes_the_next_operation_nothing(self, bench: _Bench):
        """cancellation-leaves-bed-ready: the detach cause, whose recovery is the next link's.

        A box the dead link may have left armed is cleared by the next link's
        connect-time press, so nothing is owed forward from a lifetime that ended.
        """
        first = asyncio.ensure_future(bench.streamer.run_operation(_store_program()))
        await bench.start()
        bench.streamer.link_lost()
        await _settle()
        assert await first is OperationOutcome.RELEASED

        second = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({PRESET_1})),))
            )
        )
        await bench.tick()

        assert bench.frames[-1] == "preset-1"
        await bench.run_until(second.done)
        assert await second is OperationOutcome.COMPLETED


class TestSingleReleasePerLifecycle:
    """single-release-per-lifecycle: one zero frame ends a lifecycle, and only one."""

    async def test_the_set_emptying_writes_exactly_one_confirmed_zero_frame(
        self, bench: _Bench
    ):
        """single-release-per-lifecycle: one release, as a Write Request, and no other."""
        bench.hold(HEAD_UP, ttl_s=0.25)
        await bench.start()
        await bench.ticks(5)

        assert bench.frames.count(RELEASE.decode()) == 1
        assert bench.writer.confirmed[-1] is True
        assert bench.streamer.diagnostics["releases"] == 1

    async def test_a_release_whose_completion_fails_is_not_counted(self, bench: _Bench):
        """release-is-a-write-request: the counter reads releases that reached the box.

        A completion the link refused proves nothing arrived, so counting the
        submission would report a release the bed never saw.
        """
        bench.writer.fail_completions = True
        bench.hold(HEAD_UP, ttl_s=0.25)
        await bench.start()
        await bench.ticks(5)

        assert bench.frames.count(RELEASE.decode()) == 1
        assert bench.streamer.diagnostics["releases"] == 0

    async def test_pings_zero_bit_frames_end_no_lifecycle(self, bench: _Bench):
        """single-release-per-lifecycle: a bit-empty benchmark frame is content."""
        bench.hold(PING)
        await bench.start()
        await bench.ticks(3)

        # The bench encoder names the controls a frame carries; on the CU170
        # ping carries no bit, so these frames go out with the release's bytes.
        assert bench.frames == ["ping"] * 4
        assert bench.streamer.diagnostics["releases"] == 0


class TestPing:
    """ping: the benchmark expresses only as the sole held control."""

    async def test_ping_alone_writes_confirmed_bit_empty_frames(self, bench: _Bench):
        """ping: the frames carry no bits and their round trips land in diagnostics."""
        bench.hold(PING)
        await bench.start()
        await bench.ticks(2)

        assert bench.writer.confirmed == [True] * 3
        benchmark = bench.streamer.diagnostics["benchmark"]
        assert benchmark["submissions"] == 3
        assert benchmark["completions"] == 3
        assert benchmark["ended"] is False

    async def test_another_control_beside_ping_ends_the_benchmark(self, bench: _Bench):
        """ping: the frames carry that control's bits and the end is recorded."""
        bench.hold(PING)
        await bench.start()
        bench.hold(PING, HEAD_UP)
        await bench.tick()

        assert bench.frames[-1] == "motor-head-up,ping"
        benchmark = bench.streamer.diagnostics["benchmark"]
        assert benchmark["ended"] is True
        assert benchmark["end_reason"] == "another control held"

    async def test_a_stop_ends_the_benchmark_like_any_hold(self, bench: _Bench):
        """ping: a stop ends it as it ends any hold."""
        bench.hold(PING)
        await bench.start()
        bench.streamer.release_wire()
        await _settle()

        assert bench.streamer.diagnostics["benchmark"]["end_reason"] == "released"


class TestDeadlinesCrossEveryTier:
    """deadlines-cross-every-tier: the streamer's clock is the backstop."""

    async def test_a_control_drops_on_the_streamers_own_clock(self, bench: _Bench):
        """deadlines-cross-every-tier: the deadline passes with no newer push."""
        bench.hold(HEAD_UP, ttl_s=0.25)
        await bench.start()
        await bench.ticks(3)

        assert bench.frames[-1] == RELEASE.decode()

    async def test_the_lapse_push_drops_it_first_when_it_arrives_first(self, bench: _Bench):
        """deadlines-cross-every-tier: the reconstructor's push is the fast path."""
        bench.hold(HEAD_UP, ttl_s=10.0)
        await bench.start()
        await bench.ticks(3)
        bench.streamer.hold({})
        await bench.tick()

        assert bench.frames[-1] == RELEASE.decode()


class TestPacing:
    """paced-within-the-sustain-window: the emission floor, and no burst."""

    def test_a_profile_pacing_past_the_sustain_window_is_refused(self):
        """paced-within-the-sustain-window: the ceiling is sustain less the margin.

        The CU170's is 217.5 - 5 = 212.5 ms; a profile pacing wider streams a
        motor the box stops between frames, which is the whole point of F.
        """
        assert PROFILE.frame_interval_ms <= PROFILE.sustain_window_ms - PROFILE.send_margin_ms

        with pytest.raises(ValueError, match="paces wider"):
            StreamProfile(frame_interval_ms=213, sustain_window_ms=217.5, send_margin_ms=5)

        StreamProfile(frame_interval_ms=212, sustain_window_ms=217.5, send_margin_ms=5)

    async def test_the_release_leaves_whatever_the_gate_says(self, bench: _Bench):
        """single-release-per-lifecycle: the release is never credit-gated.

        A stop under a stall is exactly when a bed must stop, so the release
        goes out with credit at 0 and the barrier outstanding.
        """
        feedback = bench.feedback
        assert feedback is not None
        bench.hold(HEAD_UP)
        await bench.start()
        feedback.verdict = SendVerdict.WITHHOLD
        await bench.tick()

        assert bench.frames == ["motor-head-up"]

        bench.streamer.release_wire()
        await _settle()

        assert bench.frames[-1] == RELEASE.decode()
        assert bench.writer.confirmed[-1] is True

    async def test_no_two_frames_leave_closer_than_the_emission_floor(self, bench: _Bench):
        """paced-within-the-sustain-window: one frame per wake at F."""
        sends: list[float] = []
        original = bench.writer.submit

        def record(frame: bytes) -> None:
            sends.append(bench.clock.now)
            original(frame)

        bench.writer.submit = record  # type: ignore[method-assign]
        bench.hold(HEAD_UP)
        await bench.start()
        await bench.ticks(4)

        gaps = [later - earlier for earlier, later in zip(sends, sends[1:], strict=False)]
        assert gaps == pytest.approx([FRAME_MS / 1000] * 4)

    async def test_a_missed_wake_does_not_burst(self, bench: _Bench):
        """paced-within-the-sustain-window: a late wake sends one frame, not a catch-up run."""
        bench.hold(HEAD_UP)
        await bench.start()
        await bench.tick(0.5)

        assert bench.frames == ["motor-head-up"] * 2


class TestClearFloorSpacing:
    """clear-floor-spacing: a re-press waits out the control's clear floor."""

    async def test_a_re_press_waits_for_the_clear_floor(self, bench: _Bench):
        """clear-floor-spacing: the bits stay absent for the clear floor first."""
        bench.hold(LIGHT, ttl_s=0.25)
        await bench.start()
        await bench.ticks(3)

        assert bench.frames[-1] == RELEASE.decode()
        dropped_at = bench.clock.now

        bench.hold(LIGHT)
        await bench.tick()

        assert bench.frames[-1] == RELEASE.decode()

        while bench.clock.now - dropped_at < PRESS_MIN_MS / 1000:
            await bench.tick()

        assert bench.frames[-1] == "light-toggle"


class TestOptionsLatchPerLifecycle:
    """options-latch-per-lifecycle: the edge the feedback latches at."""

    async def test_one_lifecycle_edge_per_wire_lifecycle(self, bench: _Bench):
        """options-latch-per-lifecycle: signalled once, at the lifecycle's first frame.

        What a feedback reads at the edge is its own; the streamer's part is
        that the edge arrives exactly once per lifecycle, so nothing a bed
        latches can change under a press that is already running.
        """
        feedback = bench.feedback
        assert feedback is not None
        bench.hold(HEAD_UP, ttl_s=0.25)
        await bench.start()
        await bench.ticks(3)

        assert feedback.lifecycles == 1

        bench.hold(HEAD_UP, ttl_s=0.5)
        await bench.run_until(lambda: feedback.lifecycles == 2)

        assert feedback.lifecycles == 2


class TestCueOrCeiling:
    """cue-or-ceiling: a staged operation advances on its cue, never on time."""

    async def test_a_stage_advances_at_once_on_its_cue(self, bench: _Bench):
        """cue-or-ceiling: the cue arrives and the next stage takes over."""
        assert bench.feedback is not None
        operation = asyncio.ensure_future(bench.streamer.run_operation(_store_program(3000)))
        await bench.start()
        await bench.ticks(3)

        assert set(bench.frames) == {"store-preset-1"}

        bench.feedback.cue_is_met = True
        await bench.tick()

        assert bench.frames[-1] == RELEASE.decode()

        await bench.run_until(lambda: bench.frames[-1] == "preset-1")
        bench.feedback.cue_is_met = True
        await bench.run_until(operation.done)

        assert await operation is OperationOutcome.COMPLETED
        assert [cue.pulses for cue in bench.feedback.cues_begun] == [1, 3]

    async def test_a_ceiling_with_no_cue_fails_the_operation(self, bench: _Bench):
        """cue-or-ceiling: no later stage runs, and no stage advances on elapsed time."""
        operation = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(CuedStage(frozenset({STORE_1}), CueRequest(1), 500),))
            )
        )
        await bench.start()
        await bench.run_until(operation.done)

        assert await operation is OperationOutcome.CEILING
        assert "preset-1" not in bench.frames

    async def test_a_cue_less_stage_completes_at_its_press_floor(self, bench: _Bench):
        """cue-or-ceiling: the disarming press ends on its floor, not on elapsed time."""
        operation = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({DUMMY})),))
            )
        )
        await bench.start()
        await bench.ticks(2)

        assert bench.frames == ["preset-dummy"] * 3

        await bench.tick()

        assert bench.frames[-1] == RELEASE.decode()
        assert await operation is OperationOutcome.COMPLETED


class TestOperationsExpressAlone:
    """The withhold an operation puts on every held control."""

    async def test_a_held_control_is_withheld_and_pressed_again_after(self, bench: _Bench):
        """holds-withheld-during-operations: withheld, never refused or ended.

        The frame carries the operation's bits alone, and the hold resumes at
        the operation's end - which is also cancellation-leaves-bed-ready for
        the preemption cause, the hold being what preempted nothing.
        """
        bench.hold(HEAD_UP, ttl_s=30.0)
        await bench.start()
        await bench.ticks(3)

        operation = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({DUMMY})),))
            )
        )
        await bench.tick()

        assert bench.frames[-1] == "preset-dummy"

        await bench.run_until(operation.done)
        assert await operation is OperationOutcome.COMPLETED

        await bench.run_until(lambda: bench.frames[-1] == "motor-head-up")

    async def test_an_operation_opens_once_an_unmet_press_floor_has_drained(
        self, bench: _Bench
    ):
        """operations-express-alone: never refused, and at most one press floor later."""
        bench.hold(HEAD_UP, ttl_s=30.0)
        await bench.start()

        operation = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({DUMMY})),))
            )
        )
        await bench.tick()

        assert bench.frames[-1] == "motor-head-up"

        await bench.ticks(2)

        assert bench.frames[-1] == "preset-dummy"
        await bench.run_until(operation.done)
        assert await operation is OperationOutcome.COMPLETED

    async def test_a_preempting_operation_runs_the_owed_recovery_first(self, bench: _Bench):
        """cancellation-leaves-bed-ready: a preempted store still disarms the box.

        A second request preempts the first and leads with what it owes.
        """
        first = asyncio.ensure_future(bench.streamer.run_operation(_store_program()))
        await bench.start()

        assert bench.frames == ["store-preset-1"]

        second = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({DUMMY})),))
            )
        )
        await bench.run_until(lambda: bench.frames[-1] == "preset-dummy")

        assert await first is OperationOutcome.PREEMPTED

        await bench.run_until(second.done)
        assert await second is OperationOutcome.COMPLETED
        # The preempted store's disarming press, then the one asked for.
        assert bench.frames.count("preset-dummy") >= 2
        assert "preset-1" not in bench.frames

    async def test_an_incoming_first_stage_supersedes_the_same_owed_one(
        self, bench: _Bench
    ):
        """latched-travel-stop: the box is cleared once per stop, not twice.

        A bed-wide stop that cancels a store composes the store's owed
        disarming press and the stop's own; both press the same key, so running
        both would press it twice with a release edge between.
        """
        first = asyncio.ensure_future(bench.streamer.run_operation(_store_program()))
        await bench.start()

        bench.streamer.release_wire()
        await _settle()
        assert await first is OperationOutcome.RELEASED

        second = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({DUMMY})),))
            )
        )
        await bench.run_until(second.done)
        assert await second is OperationOutcome.COMPLETED

        pressed = [frame for frame in bench.frames if frame == "preset-dummy"]
        assert bench.frames.count(RELEASE.decode()) == 2
        assert len(pressed) >= 1
        assert _press_runs(bench.frames, "preset-dummy") == 1

    async def test_a_different_incoming_first_stage_keeps_what_is_owed(
        self, bench: _Bench
    ):
        """The supersession is equality, not a blanket drop of the recovery."""
        first = asyncio.ensure_future(bench.streamer.run_operation(_store_program()))
        await bench.start()

        bench.streamer.release_wire()
        await _settle()
        assert await first is OperationOutcome.RELEASED

        second = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({PRESET_1})),))
            )
        )
        await bench.run_until(second.done)
        assert await second is OperationOutcome.COMPLETED

        assert _press_runs(bench.frames, "preset-dummy") == 1
        assert _press_runs(bench.frames, "preset-1") == 1

    async def test_an_operation_ends_with_a_release_edge_before_a_hold_resumes(
        self, bench: _Bench
    ):
        """release-edges-end-lifecycles: the zero frame between the last stage and the hold.

        The operation's own end is a release edge, so the withheld press is
        pressed again a clear floor later rather than following the last stage's
        bits with no button-up between them.
        """
        bench.hold(HEAD_UP, ttl_s=30.0)
        await bench.start()
        await bench.ticks(3)

        operation = asyncio.ensure_future(
            bench.streamer.run_operation(
                OperationProgram(stages=(PressStage(frozenset({DUMMY})),))
            )
        )
        await bench.run_until(operation.done)
        assert await operation is OperationOutcome.COMPLETED

        await bench.run_until(lambda: bench.frames[-1] == "motor-head-up")

        pressed = bench.frames.index("preset-dummy")
        resumed = len(bench.frames) - 1
        between = bench.frames[pressed + 1 : resumed + 1]

        assert RELEASE.decode() in between
        assert between.index(RELEASE.decode()) < between.index("motor-head-up")

    async def test_a_failed_stage_runs_the_recovery_stage_last(self, bench: _Bench):
        """cancellation-leaves-bed-ready: the disarming press is the operation's last frame."""
        operation = asyncio.ensure_future(bench.streamer.run_operation(_store_program()))
        await bench.start()
        await bench.run_until(operation.done)

        assert await operation is OperationOutcome.CEILING
        assert "preset-1" not in bench.frames
        assert bench.frames[-1] == RELEASE.decode()
        assert bench.frames[-2] == "preset-dummy"


class TestAStreamThatCannotRunSaysSo:
    """What a caller hears when the pump refuses, rather than waiting forever."""

    @pytest.mark.parametrize(
        "program",
        [
            OperationProgram(stages=(PressStage(frozenset({UNDECLARED})),)),
            OperationProgram(
                stages=(PressStage(frozenset({DUMMY})),),
                recovery=(PressStage(frozenset({UNDECLARED})),),
            ),
        ],
        ids=["stage", "recovery"],
    )
    async def test_an_operation_naming_an_undeclared_control_is_refused(
        self, bench: _Bench, program: OperationProgram
    ):
        """The streamer prices a whole program at submission, not one wake at a time."""
        with pytest.raises(ValueError, match="preset-nowhere"):
            bench.streamer.stage(program)

        await bench.start()

        assert bench.frames == []

    async def test_a_pump_failure_answers_the_operation_it_was_running(
        self, bench: _Bench, caplog
    ):
        """A frame the bed cannot compose ends the stream, and its caller hears why."""
        bench.encoder.refusal = ConnectionError("key characteristic is not resolved")
        outcome = bench.streamer.stage(_store_program())

        await bench.start()

        assert bench.frames == []
        assert isinstance(outcome.exception(), ConnectionError)
        assert "AA:BB:CC:DD:EE:FF" in caplog.text
        assert (
            bench.streamer.diagnostics["failure"] == "key characteristic is not resolved"
        )

    async def test_a_failed_pump_starts_no_second_one(self, bench: _Bench, caplog):
        """One failure per link: a later push re-runs what the first push proved dead."""
        bench.encoder.refusal = ConnectionError("key characteristic is not resolved")
        bench.hold(HEAD_UP)
        await bench.start()

        bench.hold(HEAD_UP, FEET_UP)
        await bench.start()

        assert bench.frames == []
        assert caplog.text.count("Hold stream on") == 1


def _store_program(ceiling_ms: int = 500) -> OperationProgram:
    """Return a store's program: arm to one pulse, slot to three, disarm on failure."""
    return OperationProgram(
        stages=(
            CuedStage(frozenset({STORE_1}), CueRequest(1), ceiling_ms),
            CuedStage(frozenset({PRESET_1}), CueRequest(3), ceiling_ms),
        ),
        recovery=(PressStage(frozenset({DUMMY})),),
    )


def _names(controls: Iterable[Control]) -> list[str]:
    """Return control names, sorted, the way a frame reads."""
    return sorted(control.name for control in controls)


async def test_a_completed_operation_owes_no_recovery():
    """cancellation-leaves-bed-ready: the recovery answers a failure, not an end.

    Between the last stage advancing to done and the release edge that settles
    the operation, the box is already in the state that stage left it in. A
    preempting operation or a wire release in that window carries no recovery
    press.
    """
    operation = StagedOperation(
        stages=(PressStage(frozenset({PRESET_1})),),
        recovery_stages=(PressStage(frozenset({DUMMY})),),
        outcome=asyncio.get_running_loop().create_future(),
    )

    assert operation.owed_recovery() == (PressStage(frozenset({DUMMY})),)

    operation.advance(now=0.0, gap_s=0.0)

    assert operation.done
    assert operation.owed_recovery() == ()


def test_a_frame_plan_names_the_fact_the_pump_branches_on():
    """frame-is-the-or: a bit-empty plan and a release are different facts."""
    ping_only = FramePlan(controls=frozenset({PING}), is_release=False, attributable=True)
    release = FramePlan(controls=frozenset(), is_release=True, attributable=True)

    assert _names(ping_only.controls) == ["ping"]
    assert ping_only.is_release is False
    assert release.is_release is True
