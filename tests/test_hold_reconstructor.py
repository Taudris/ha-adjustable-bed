"""Tests for the hold reconstructor.

Every intent door, the held set it derives, the fences a stop puts up, and the
two link edges. Time is virtual: the reconstructor's clock is injected and its
pending-event timer is captured, so a deadline arrives when a test says it does.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.adjustable_bed.hold_capability import HoldCapable
from custom_components.adjustable_bed.hold_intent import (
    Activate,
    Hold,
    HoldOutcome,
    IntentAction,
    IntentId,
    IntentSample,
    IntentSampleSet,
    SenderId,
)
from custom_components.adjustable_bed.hold_reconstructor import (
    _SENDER_QUIET_HORIZON_S,
    HoldReconstructor,
)
from custom_components.adjustable_bed.hold_roster import (
    ActionKind,
    Control,
    ControlDeclaration,
    ControlRoster,
)

_CALL_LATER = "custom_components.adjustable_bed.hold_reconstructor.async_call_later"

HEAD_UP = Control("motor-head-up")
HEAD_DOWN = Control("motor-head-down")
PRESET_1 = Control("preset-1")
LIGHT = Control("light-toggle")

CARD = SenderId("card")
OTHER = SenderId("other-card")

# The motor's cap is deliberately short so a lifetime test needs no long clock
# walk; the roster is synthetic, not the CU170's.
MOTOR_TTL_MAX_MS = 8000
ACTIVATE_MS = 1000


def _roster() -> ControlRoster:
    """Return a synthetic roster: two motor directions, a preset, a light."""
    return ControlRoster(
        (
            _declare(HEAD_UP, {ActionKind.HOLD, ActionKind.ACTIVATE}, ACTIVATE_MS),
            _declare(HEAD_DOWN, {ActionKind.HOLD, ActionKind.ACTIVATE}, ACTIVATE_MS),
            _declare(PRESET_1, {ActionKind.HOLD}, None),
            _declare(LIGHT, {ActionKind.ACTIVATE}, 500),
        )
    )


def _declare(
    control: Control, actions: set[ActionKind], activate_duration_ms: int | None
) -> ControlDeclaration:
    """Return one synthetic declaration."""
    return ControlDeclaration(
        control=control,
        actions=frozenset(actions),
        ttl_max_ms=MOTOR_TTL_MAX_MS,
        activate_duration_ms=activate_duration_ms,
        press_min_frames=1,
        press_min_ms=223,
    )


class _Clock:
    """A monotonic source a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Timer:
    """Stands in for async_call_later, holding the single armed wake."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._action: Any = None
        self.due: float | None = None

    def __call__(self, hass: HomeAssistant, delay: float, action: Any) -> Any:
        del hass
        self._action = action
        self.due = self._clock.now + delay
        return self._cancel

    def _cancel(self) -> None:
        self._action = None
        self.due = None

    def fire(self) -> None:
        """Run the armed wake."""
        action = self._action
        self._action = None
        self.due = None
        action(None)


class _RecordingController(HoldCapable):
    """A hold-capable controller double that records every pushed set."""

    def __init__(self) -> None:
        self.pushes: list[dict[Control, float]] = []

    def hold(self, held: Any) -> None:
        self.pushes.append(dict(held))

    @property
    def latest(self) -> dict[Control, float]:
        """Return the most recently pushed set."""
        return self.pushes[-1]

    @property
    def controls(self) -> list[set[Control]]:
        """Return the control set of each push, in order."""
        return [set(push) for push in self.pushes]


class _RecordingTarget:
    """A hold target double that records every summons."""

    def __init__(self) -> None:
        self.connects = 0

    def connect_on_demand(self) -> None:
        self.connects += 1


class _Subscriber:
    """A held-set listener recording the set at each publication."""

    def __init__(self, reconstructor: HoldReconstructor) -> None:
        self._reconstructor = reconstructor
        self.sets: list[set[Control]] = []

    def __call__(self) -> None:
        self.sets.append(set(self._reconstructor.held))


@pytest.fixture
def clock() -> _Clock:
    """Return the injected monotonic clock."""
    return _Clock()


@pytest.fixture
def timer(clock: _Clock) -> Iterable[_Timer]:
    """Capture the reconstructor's pending-event wake instead of scheduling it."""
    armed = _Timer(clock)
    with patch(_CALL_LATER, armed):
        yield armed


@pytest.fixture
def target() -> _RecordingTarget:
    """Return the recording hold target."""
    return _RecordingTarget()


@pytest.fixture
def reconstructor(
    hass: HomeAssistant, clock: _Clock, timer: _Timer, target: _RecordingTarget
) -> HoldReconstructor:
    """Return a reconstructor over the synthetic roster and the virtual clock."""
    del timer  # Patched for the test's whole span by the fixture
    return HoldReconstructor(hass, _roster(), target, clock=clock)


def _advance(clock: _Clock, timer: _Timer, seconds: float) -> None:
    """Advance the clock, running the pending wake whenever it comes due."""
    target_time = clock.now + seconds
    while timer.due is not None and timer.due <= target_time:
        clock.now = timer.due
        timer.fire()
    clock.now = target_time


def _press_count(pushes: list[set[Control]], control: Control) -> int:
    """Return how many separate times the control rises into the pushed set."""
    return sum(
        control in push and control not in previous
        for push, previous in zip(pushes, [set(), *pushes], strict=False)
    )


def _samples(
    reconstructor: HoldReconstructor,
    seq: int,
    *samples: tuple[str, Control, IntentAction],
    sender: SenderId = CARD,
) -> None:
    """Hand one message to the reconstructor."""
    reconstructor.handle_samples(
        IntentSampleSet(
            sender=sender,
            seq=seq,
            samples=tuple(
                IntentSample(intent_id=IntentId(intent_id), control=control, action=action)
                for intent_id, control, action in samples
            ),
        )
    )


class TestIntentBounds:
    """intents-are-parameterized, intents-are-time-bounded, deadline-clamps-at-intake."""

    def test_every_door_bounds_its_assertion(self, reconstructor, clock):
        """intents-are-time-bounded: no assertion reaches the set without a deadline."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(2000)), ("b", LIGHT, Activate()))
        reconstructor.submit(HEAD_DOWN, 3000)

        for intent in reconstructor.held.values():
            assert intent.deadline > clock.now

        assert "ttl_ms" in inspect.signature(reconstructor.submit).parameters

    def test_an_activate_carries_no_ttl_and_reads_the_rosters(self, reconstructor, clock):
        """intents-are-parameterized: an Activate's duration is the roster's."""
        _samples(reconstructor, 1, ("a", LIGHT, Activate()))

        assert reconstructor.held[LIGHT].deadline == pytest.approx(clock.now + 0.5)
        assert Activate() == Activate()

    def test_a_ttl_past_the_cap_clamps_at_intake(self, reconstructor, clock):
        """deadline-clamps-at-intake: the deadline is last refresh plus the clamped ttl."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(30000)))

        assert reconstructor.held[HEAD_UP].deadline == pytest.approx(clock.now + 8.0)

    def test_a_vanished_client_holds_for_at_most_one_clamped_ttl(
        self, reconstructor, clock, timer
    ):
        """deadline-clamps-at-intake: refreshes do not compose into a longer bound."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(4000)))
        _advance(clock, timer, 1.0)
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(4000)))

        _advance(clock, timer, 3.9)
        assert reconstructor.holds_anything
        _advance(clock, timer, 0.2)
        assert not reconstructor.holds_anything

    def test_an_activate_born_deadline_does_not_move(self, reconstructor, clock, timer):
        """deadline-clamps-at-intake: an Activate's bound is fixed at its press start."""
        _samples(reconstructor, 1, ("a", LIGHT, Activate()))
        deadline = reconstructor.held[LIGHT].deadline

        _advance(clock, timer, 0.2)
        _samples(reconstructor, 2, ("a", LIGHT, Activate()))

        assert reconstructor.held[LIGHT].deadline == deadline


class TestSuccession:
    """succession-replaces-deadline and hold-lifetime-from-press-start."""

    def test_a_shorter_ttl_moves_the_deadline_earlier(self, reconstructor, clock, timer):
        """succession-replaces-deadline: the newest word governs, never a max."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(6000)))
        _advance(clock, timer, 1.0)
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(1000)))

        assert reconstructor.held[HEAD_UP].deadline == pytest.approx(clock.now + 1.0)

    def test_a_refreshed_hold_ends_at_its_press_start_plus_the_cap(
        self, reconstructor, clock, timer
    ):
        """hold-lifetime-from-press-start: refreshes cannot outrun the lifetime."""
        lifetime_end = 1000.0 + MOTOR_TTL_MAX_MS / 1000
        for seq in range(1, 9):
            _samples(reconstructor, seq, ("a", HEAD_UP, Hold(4000)))
            assert reconstructor.held[HEAD_UP].deadline <= lifetime_end
            _advance(clock, timer, 1.0)

        assert clock.now == lifetime_end
        assert not reconstructor.holds_anything

        _samples(reconstructor, 9, ("a", HEAD_UP, Hold(4000)))
        assert not reconstructor.holds_anything
        assert reconstructor.diagnostics["dropped_renewals"] == 1

    def test_an_overlapping_intent_id_holds_across_the_changeover(
        self, reconstructor, clock, timer
    ):
        """hold-lifetime-from-press-start: a new id is a new press, with no gap."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(2000)))
        _advance(clock, timer, 1.5)
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(2000)), ("b", HEAD_UP, Hold(2000)))

        _advance(clock, timer, 1.0)
        assert reconstructor.holds_anything
        assert reconstructor.held[HEAD_UP].began == pytest.approx(1001.5)


class TestIntakeIsMemoryless:
    """intent-intake-is-memoryless: the seq gate, retained records, eviction."""

    def test_a_stale_message_drops_whole(self, reconstructor):
        """intent-intake-is-memoryless: no part of a straggler is applied."""
        _samples(reconstructor, 5, ("a", HEAD_UP, Hold(2000)))
        _samples(reconstructor, 5, ("b", HEAD_DOWN, Hold(2000)))
        _samples(reconstructor, 4, ("c", PRESET_1, Hold(2000)))

        assert set(reconstructor.held) == {HEAD_UP}
        assert reconstructor.diagnostics["rejected_messages"] == 2

    def test_a_newer_message_creates_refreshes_and_ends(self, reconstructor, clock, timer):
        """intent-intake-is-memoryless: present refreshes, ttl 0 ends, unknown creates."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        _advance(clock, timer, 1.0)
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(3000)), ("b", HEAD_DOWN, Hold(3000)))

        assert set(reconstructor.held) == {HEAD_UP, HEAD_DOWN}
        assert reconstructor.held[HEAD_UP].last_refresh == pytest.approx(clock.now)

        _samples(reconstructor, 3, ("a", HEAD_UP, Hold(0)), ("b", HEAD_DOWN, Hold(3000)))
        assert set(reconstructor.held) == {HEAD_DOWN}

    def test_an_omitted_intent_lapses_rather_than_ending(self, reconstructor, clock, timer):
        """intent-intake-is-memoryless: absence is never a signal."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)), ("b", HEAD_DOWN, Hold(3000)))
        _advance(clock, timer, 1.0)
        _samples(reconstructor, 2, ("b", HEAD_DOWN, Hold(3000)))

        assert set(reconstructor.held) == {HEAD_UP, HEAD_DOWN}
        _advance(clock, timer, 2.1)
        assert set(reconstructor.held) == {HEAD_DOWN}

    def test_a_refresh_of_a_stopped_record_holds_nothing(self, reconstructor, clock, timer):
        """intent-intake-is-memoryless: the retained record refreshes and holds nothing."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        reconstructor.stop_all()
        _advance(clock, timer, 1.0)

        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(3000)))
        assert not reconstructor.holds_anything
        assert reconstructor.diagnostics["dropped_renewals"] == 1

    def test_a_late_refresh_of_a_lapsed_press_starts_no_second_press(
        self, reconstructor, clock, timer
    ):
        """intent-intake-is-memoryless: a record outlives the hold it lapsed out of."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(750)))
        _advance(clock, timer, 0.9)
        assert not reconstructor.holds_anything

        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(750)))

        assert not reconstructor.holds_anything
        assert reconstructor.diagnostics["dropped_renewals"] == 1

    def test_a_released_record_stays_for_its_own_ttl(self, reconstructor, clock, timer):
        """intent-intake-is-memoryless: a ttl-0 sample ends the hold and keeps the record."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(0)))

        assert not reconstructor.holds_anything
        assert reconstructor.diagnostics["records"] == 1

        _advance(clock, timer, 3.1)
        assert reconstructor.diagnostics["records"] == 0

    def test_an_evicted_id_is_unknown_and_creates(self, reconstructor, clock, timer):
        """intent-intake-is-memoryless: records are evicted, not tombstoned."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        reconstructor.stop_all()
        _advance(clock, timer, 3.5)

        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(1000)))
        assert reconstructor.holds_anything
        assert reconstructor.held[HEAD_UP].began == pytest.approx(clock.now)

    def test_a_quiet_sender_is_forgotten_after_the_horizon(self, reconstructor, clock, timer):
        """intent-intake-is-memoryless: a high-water outlives its last intent, not forever."""
        _samples(reconstructor, 7, ("a", HEAD_UP, Hold(1000)))
        _advance(clock, timer, _SENDER_QUIET_HORIZON_S + 1)

        assert reconstructor.diagnostics["senders"] == 0
        _samples(reconstructor, 1, ("b", HEAD_UP, Hold(1000)))
        assert reconstructor.holds_anything


class TestNewestContributorGoverns:
    """newest-contributor-governs: whose values the held set publishes."""

    def test_the_later_press_governs_and_the_survivor_reverts(
        self, reconstructor, clock, timer
    ):
        """newest-contributor-governs: began and deadline revert together."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(6000)))
        _advance(clock, timer, 1.0)
        _samples(reconstructor, 1, ("b", HEAD_UP, Hold(6000)), sender=OTHER)

        later = reconstructor.held[HEAD_UP]
        assert later.began == pytest.approx(1001.0)
        assert later.deadline == pytest.approx(1007.0)

        _samples(reconstructor, 2, ("b", HEAD_UP, Hold(0)), sender=OTHER)
        survivor = reconstructor.held[HEAD_UP]
        assert survivor.began == pytest.approx(1000.0)
        assert survivor.deadline == pytest.approx(1006.0)

    def test_the_published_pair_is_one_contributors_own(self, reconstructor, clock, timer):
        """newest-contributor-governs: never a began from one and a deadline from another."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(6000)))
        _advance(clock, timer, 2.0)
        _samples(reconstructor, 1, ("b", HEAD_UP, Hold(1000)), sender=OTHER)

        intent = reconstructor.held[HEAD_UP]
        assert (intent.began, intent.deadline) == pytest.approx((1002.0, 1003.0))


class TestEndIsNotStop:
    """end-is-not-stop: what a release ends, and what the safety verb ends."""

    def test_a_release_ends_only_its_own_intent(self, reconstructor):
        """end-is-not-stop: the other control stays held and nothing is fenced."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)), ("b", HEAD_DOWN, Hold(3000)))
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(0)), ("b", HEAD_DOWN, Hold(3000)))

        assert set(reconstructor.held) == {HEAD_DOWN}
        _samples(reconstructor, 3, ("c", HEAD_UP, Hold(3000)), ("b", HEAD_DOWN, Hold(3000)))
        assert set(reconstructor.held) == {HEAD_UP, HEAD_DOWN}

    def test_stop_takes_a_motors_two_controls_and_stop_all_takes_the_bed(
        self, reconstructor
    ):
        """end-is-not-stop: the per-control form and the bed-wide form."""
        _samples(
            reconstructor,
            1,
            ("a", HEAD_UP, Hold(3000)),
            ("b", HEAD_DOWN, Hold(3000)),
            ("c", PRESET_1, Hold(3000)),
        )

        reconstructor.stop((HEAD_UP, HEAD_DOWN))
        assert set(reconstructor.held) == {PRESET_1}

        reconstructor.stop_all()
        assert not reconstructor.holds_anything


class TestStopFencesTheControl:
    """stop-fences-the-control: the retained record is the fence."""

    def test_the_shrunken_set_reaches_the_controller_with_nothing_queued(
        self, reconstructor
    ):
        """stop-fences-the-control: the push lands inside the synchronous stop."""
        controller = _RecordingController()
        reconstructor.attach(controller)
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        pushes_before = len(controller.pushes)

        reconstructor.stop_all()

        assert not inspect.iscoroutinefunction(reconstructor.stop_all)
        assert len(controller.pushes) == pushes_before + 1
        assert controller.latest == {}

    def test_an_in_flight_refresh_of_a_stopped_press_holds_nothing(self, reconstructor):
        """stop-fences-the-control: the stopped id re-asserts nothing."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        reconstructor.stop((HEAD_UP,))

        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(3000)))
        assert not reconstructor.holds_anything

    def test_a_new_intent_id_after_the_stop_asserts_normally(self, reconstructor):
        """stop-fences-the-control: a contributor re-asserts under a new id."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        reconstructor.stop_all()

        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(3000)), ("b", HEAD_UP, Hold(3000)))
        assert set(reconstructor.held) == {HEAD_UP}

    def test_stop_all_fences_every_control(self, reconstructor):
        """stop-fences-the-control: the bed-wide form fences the same way."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)), ("b", PRESET_1, Hold(3000)))
        reconstructor.stop_all()

        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(3000)), ("b", PRESET_1, Hold(3000)))
        assert not reconstructor.holds_anything


class TestIntentSetPublication:
    """intent-set-publication: one complete set per change, replay-latest."""

    def test_a_subscriber_receives_the_latest_set_at_once(self, reconstructor):
        """intent-set-publication: replay-latest on subscribe."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        subscriber = _Subscriber(reconstructor)

        reconstructor.async_add_listener(subscriber)

        assert subscriber.sets == [{HEAD_UP}]

    def test_every_change_publishes_one_complete_set(self, reconstructor, clock, timer):
        """intent-set-publication: creation, refresh, expiry and end are one event each."""
        subscriber = _Subscriber(reconstructor)
        reconstructor.async_add_listener(subscriber)

        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        _advance(clock, timer, 1.0)
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(3000)), ("b", PRESET_1, Hold(3000)))
        _samples(reconstructor, 3, ("a", HEAD_UP, Hold(0)), ("b", PRESET_1, Hold(3000)))
        _advance(clock, timer, 4.0)

        assert subscriber.sets == [set(), {HEAD_UP}, {HEAD_UP, PRESET_1}, {PRESET_1}, set()]

    def test_unregistering_stops_the_publications(self, reconstructor):
        """intent-set-publication: the register call returns its own unregister."""
        subscriber = _Subscriber(reconstructor)
        unregister = reconstructor.async_add_listener(subscriber)

        unregister()
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))

        assert subscriber.sets == [set()]

    def test_a_control_no_controller_expressed_is_still_published(
        self, reconstructor, target
    ):
        """intent-set-publication: the published set is the intent side."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))

        assert set(reconstructor.held) == {HEAD_UP}
        assert target.connects == 1


class TestLinkEdges:
    """one-shots-persist-holds-do-not and one-shot-fails-at-detach."""

    def test_a_connect_pushes_the_one_shots_and_no_sample_borne_hold(
        self, reconstructor, clock, timer
    ):
        """one-shots-persist-holds-do-not: what a link's arrival expresses."""
        _samples(reconstructor, 1, ("a", LIGHT, Activate()))
        submission = reconstructor.submit(HEAD_DOWN, 4000)
        _samples(reconstructor, 2, ("b", HEAD_UP, Hold(4000)), ("c", PRESET_1, Hold(200)))
        _advance(clock, timer, 0.3)

        controller = _RecordingController()
        reconstructor.attach(controller)

        assert controller.controls == [{LIGHT, HEAD_DOWN}]
        assert not submission.done()

    def test_a_sample_borne_hold_reaches_the_controller_at_its_next_sample(
        self, reconstructor
    ):
        """one-shots-persist-holds-do-not: a card's hold resumes at its next sample."""
        _samples(reconstructor, 1, ("b", HEAD_UP, Hold(4000)))
        controller = _RecordingController()
        reconstructor.attach(controller)

        _samples(reconstructor, 2, ("b", HEAD_UP, Hold(4000)))

        assert controller.controls == [set(), {HEAD_UP}]

    def test_a_detach_fails_every_one_shot_at_once(self, reconstructor):
        """one-shot-fails-at-detach: an awaiting caller wakes there, not after a reconnect."""
        controller = _RecordingController()
        reconstructor.attach(controller)
        submission = reconstructor.submit(HEAD_UP, 4000)
        _samples(reconstructor, 1, ("b", PRESET_1, Hold(4000)))
        pushes_before = len(controller.pushes)

        reconstructor.detach()

        assert submission.done()
        assert submission.result() is HoldOutcome.FAILED
        assert len(controller.pushes) == pushes_before

    def test_a_detach_retains_the_failed_records_and_leaves_holds_live(
        self, reconstructor, target
    ):
        """one-shot-fails-at-detach: the sample-borne hold lapses on its own deadline."""
        reconstructor.attach(_RecordingController())
        _samples(reconstructor, 1, ("b", HEAD_UP, Hold(4000)))
        connects_before = target.connects

        reconstructor.detach()

        assert set(reconstructor.held) == {HEAD_UP}
        assert target.connects == connects_before

    def test_a_detach_evicts_what_it_ended_after_its_retention_span(
        self, reconstructor, clock, timer
    ):
        """one-shot-fails-at-detach: a detach ends a record, it does not keep it forever."""
        reconstructor.attach(_RecordingController())
        reconstructor.submit(HEAD_UP, 4000)

        reconstructor.detach()
        assert reconstructor.diagnostics["records"] == 1

        _advance(clock, timer, 4.1)
        assert reconstructor.diagnostics["records"] == 0

    def test_the_next_connect_pushes_nothing_the_dead_link_expressed(
        self, reconstructor
    ):
        """one-shot-fails-at-detach: an expressed one-shot does not come back."""
        first = _RecordingController()
        reconstructor.attach(first)
        reconstructor.submit(HEAD_UP, 4000)
        reconstructor.detach()

        second = _RecordingController()
        reconstructor.attach(second)

        assert second.controls == [set()]

    def test_a_link_start_report_ends_nothing(self, reconstructor):
        """one-shot-fails-at-detach: only a lost link is a detach."""
        submission = reconstructor.submit(HEAD_UP, 4000)

        reconstructor.detach()

        assert not submission.done()

    def test_a_second_report_of_the_same_link_pushes_nothing(self, reconstructor):
        """The connection state is a state, not an edge: re-reporting it is a no-op."""
        reconstructor.submit(HEAD_UP, 4000)
        controller = _RecordingController()
        reconstructor.attach(controller)
        pushes_before = len(controller.pushes)

        reconstructor.attach(controller)

        assert len(controller.pushes) == pushes_before


class TestConnectOnDemand:
    """connect-on-demand: a push with no controller summons one."""

    def test_a_non_empty_set_with_no_controller_starts_the_connect(
        self, reconstructor, target
    ):
        """connect-on-demand: the first press after idle still connects."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))

        assert target.connects == 1

    def test_an_empty_set_summons_nothing(self, reconstructor, target):
        """connect-on-demand: only a held set is worth a link."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(0)))

        assert target.connects == 1

    def test_a_deadline_passing_before_the_connect_counts_and_does_not_raise(
        self, reconstructor, clock, timer
    ):
        """connect-on-demand: an unexpressed hold surfaces in diagnostics, silently."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(1000)))

        _advance(clock, timer, 1.5)

        assert not reconstructor.holds_anything
        assert reconstructor.diagnostics["rejected_submissions"] == 1


class TestDeadlinesCrossEveryTier:
    """deadlines-cross-every-tier: one clock, and the lapse push on it."""

    def test_the_default_clock_is_the_event_loops_monotonic_time(self, hass, target):
        """deadlines-cross-every-tier: the same epoch async_call_later schedules against."""
        reconstructor = HoldReconstructor(hass, _roster(), target)

        assert reconstructor._clock is hass.loop.time

    def test_the_lapse_push_carries_the_shrunken_set(self, reconstructor, clock, timer):
        """deadlines-cross-every-tier: the reconstructor's lapse push is the fast path."""
        controller = _RecordingController()
        reconstructor.attach(controller)
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(1000)), ("b", PRESET_1, Hold(4000)))

        _advance(clock, timer, 1.2)

        assert controller.latest == {PRESET_1: pytest.approx(1004.0)}

    def test_the_pushed_deadline_is_the_effective_one(self, reconstructor, clock, timer):
        """deadlines-cross-every-tier: the push carries what the publication carries."""
        controller = _RecordingController()
        reconstructor.attach(controller)
        _advance(clock, timer, 7.0)
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(6000)))

        assert controller.latest[HEAD_UP] == reconstructor.held[HEAD_UP].deadline


class TestSubmissionOutcomes:
    """The direct-submission door's outcome vocabulary."""

    def test_a_submission_expressed_to_its_end_completes(self, reconstructor, clock, timer):
        """A one-shot that reached an attached controller and ran out completes."""
        reconstructor.attach(_RecordingController())
        submission = reconstructor.submit(HEAD_UP, 1000)

        _advance(clock, timer, 1.2)

        assert submission.result() is HoldOutcome.COMPLETED

    def test_a_stop_interrupts_a_live_submission(self, reconstructor):
        """A stop is an interruption, not a failure."""
        reconstructor.attach(_RecordingController())
        submission = reconstructor.submit(HEAD_UP, 4000)

        reconstructor.stop((HEAD_UP,))

        assert submission.result() is HoldOutcome.INTERRUPTED

    def test_a_submission_never_expressed_fails(self, reconstructor, clock, timer):
        """A hold no controller ever took fails when its deadline passes."""
        submission = reconstructor.submit(HEAD_UP, 1000)

        _advance(clock, timer, 1.2)

        assert submission.result() is HoldOutcome.FAILED

    def test_a_quiesce_interrupts_every_live_submission(self, reconstructor):
        """Quiesce ends intents the way a bed-wide stop does."""
        controller = _RecordingController()
        reconstructor.attach(controller)
        submission = reconstructor.submit(HEAD_UP, 4000)

        reconstructor.quiesce()

        assert submission.result() is HoldOutcome.INTERRUPTED
        assert controller.latest == {}
        assert not reconstructor.holds_anything

    def test_quiesce_closes_intake_and_cancels_the_wake(self, reconstructor, timer):
        """Nothing reaches the reconstructor once the entry is going away."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        reconstructor.quiesce()

        assert timer.due is None
        assert not reconstructor.holds_anything

    def test_both_doors_refuse_a_closed_intake(self, reconstructor):
        """A call after the close is told so, rather than silently answered."""
        reconstructor.quiesce()

        with pytest.raises(RuntimeError, match="intake closed"):
            _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        with pytest.raises(RuntimeError, match="intake closed"):
            reconstructor.submit(HEAD_UP, 1000)


class TestPressFidelity:
    """key-registration and press-state-fidelity, intent side."""

    def test_a_stop_obsoletes_a_press_before_any_push(self, reconstructor):
        """key-registration: an unregistered stopped press stays unregistered."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(3000)))
        reconstructor.stop_all()

        controller = _RecordingController()
        reconstructor.attach(controller)

        assert controller.controls == [set()]

    def test_each_press_is_expressed_once_in_the_order_it_was_held(
        self, reconstructor, clock, timer
    ):
        """press-state-fidelity: order preserved, nothing duplicated."""
        controller = _RecordingController()
        reconstructor.attach(controller)

        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(2000)))
        _advance(clock, timer, 0.5)
        _samples(reconstructor, 2, ("a", HEAD_UP, Hold(0)), ("b", PRESET_1, Hold(2000)))
        _advance(clock, timer, 0.5)
        _samples(reconstructor, 3, ("b", PRESET_1, Hold(0)), ("c", HEAD_DOWN, Hold(2000)))

        pressed = (HEAD_UP, PRESET_1, HEAD_DOWN)
        first_seen = [
            next(index for index, push in enumerate(controller.controls) if control in push)
            for control in pressed
        ]
        assert first_seen == sorted(first_seen)
        assert [_press_count(controller.controls, control) for control in pressed] == [1, 1, 1]

    def test_the_four_intent_side_loss_causes(self, reconstructor, clock, timer):
        """press-state-fidelity: a press is absent only for these.

        A stop; a press whose deadline passes with nothing expressing it, which
        is what an operation outliving it and a link staying down both look like
        from here; a de-assertion before expression began; and a link death
        inside the press's span.
        """
        controller = _RecordingController()
        reconstructor.attach(controller)
        stopped = reconstructor.submit(HEAD_UP, 4000)
        reconstructor.stop((HEAD_UP,))
        assert stopped.result() is HoldOutcome.INTERRUPTED

        reconstructor.detach()
        unexpressed = reconstructor.submit(HEAD_DOWN, 1000)
        _advance(clock, timer, 1.2)
        assert unexpressed.result() is HoldOutcome.FAILED

        _samples(reconstructor, 1, ("a", PRESET_1, Hold(3000)))
        _samples(reconstructor, 2, ("a", PRESET_1, Hold(0)))
        assert not reconstructor.holds_anything

        reconstructor.attach(_RecordingController())
        lost = reconstructor.submit(HEAD_UP, 4000)
        reconstructor.detach()
        assert lost.result() is HoldOutcome.FAILED


class TestRosterReplacement:
    """use_roster: the coordinator hands in a rebuilt roster."""

    def test_live_intents_keep_the_deadlines_the_old_roster_clamped(
        self, reconstructor, clock
    ):
        """A deadline is a value already taken, not a re-derivation."""
        _samples(reconstructor, 1, ("a", HEAD_UP, Hold(30000)))
        deadline = reconstructor.held[HEAD_UP].deadline

        reconstructor.use_roster(
            ControlRoster((_declare(HEAD_UP, {ActionKind.HOLD}, None),))
        )

        assert reconstructor.held[HEAD_UP].deadline == deadline
        assert deadline == pytest.approx(clock.now + 8.0)
