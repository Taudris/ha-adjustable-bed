"""Reconstructs the held set from intent samples and direct submissions.

Zero-order hold: every intent holds its control until its deadline, so a client
that stops sending stops holding without having to say so, and a lost message
costs a refresh rather than a press. Absence therefore carries no meaning - an
omitted intent lapses on its own deadline - which is why no signal for "the
client went away" exists or needs to.

The reconstructor knows nothing about any bed beyond what the roster declares,
and it outlives every controller: it reaches one only between an attach and a
detach, and a submission arriving with no link is an ordinary intent.
"""

from __future__ import annotations

import logging
from asyncio import Future
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from math import inf
from types import MappingProxyType
from typing import Any, Protocol

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .hold_capability import HoldCapable
from .hold_intent import (
    Deadline,
    Hold,
    HoldIntent,
    HoldOutcome,
    IntentAction,
    IntentId,
    ResolvedSample,
    ResolvedSampleSet,
    SenderId,
)
from .hold_roster import ActionKind, Control, ControlRoster

_LOGGER = logging.getLogger(__name__)

# A sender's seq high-water outlives its last intent by this much, so a message
# still in flight when its intents end cannot re-assert them under a high-water
# that has already been forgotten. Longer than any plausible websocket delay,
# short enough that a page reload's fresh sender id does not accumulate.
_SENDER_QUIET_HORIZON_S = 60.0

# Direct submissions are a degenerate sender: one intent each, no refreshes, and
# no seq gate. Reserving one id keeps them in the same record space as sampled
# intents without colliding with a client's random one. Exported because the
# reservation only holds if the wire door refuses the name: a client sending it
# would share these records' seq gate, quiet horizon and intent ids.
SUBMISSION_SENDER = SenderId("direct-submission")


class HoldTarget(Protocol):
    """What the reconstructor can ask of the object it pushes at."""

    def connect_on_demand(self) -> None:
        """Start a connect so a held set has a controller to reach."""


class _IntentOrigin(StrEnum):
    """Whether an intent has a sender that keeps refreshing it."""

    SAMPLED_HOLD = "sampled_hold"
    ONE_SHOT = "one_shot"


@dataclass(slots=True)
class _IntentRecord:
    """One (sender, intent) pair's state, live or retained.

    A record outlives its hold. A live record has no eviction instant; one whose
    hold has ended - by a ttl-0 sample, by a lapse, or by a stop - is kept for
    its retention span measured from the instant it ended, holding nothing. So a
    refresh that arrives late finds the record rather than creating a second
    press under the same id, and a press still draining its floor at the
    controller is one a bed-wide stop still names.

    The retention span is the newest sample's own clamped ttl, so a record is
    kept for as long as its client asked to hold it and no longer.
    """

    intent: HoldIntent
    origin: _IntentOrigin
    retention_s: float
    last_refresh: float
    evict_at: float = inf
    ended: bool = False
    expressed: bool = False
    expression_lost: bool = False


@dataclass(slots=True)
class _SenderState:
    """One sender's message ordering and last sign of life."""

    high_water: int
    last_activity: float


class HoldReconstructor:
    """Turns samples and submissions into the held set, and pushes it.

    Owns the intent records, the per-sender high-waters, the outcome futures,
    the subscriber set, the derived held set and one pending-event timer. It
    borrows the roster, the clock and the target it can ask to connect.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        roster: ControlRoster,
        target: HoldTarget,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the reconstructor for one bed device.

        ``clock`` defaults to the event loop's monotonic time, the same epoch
        ``async_call_later`` schedules against, so the pending-event timer and
        the deadlines it wakes for cannot disagree.
        """
        self.hass = hass
        self._roster = roster
        self._target = target
        self._clock = clock if clock is not None else hass.loop.time
        self._records: dict[tuple[SenderId, IntentId], _IntentRecord] = {}
        self._senders: dict[SenderId, _SenderState] = {}
        self._outcomes: dict[tuple[SenderId, IntentId], Future[HoldOutcome]] = {}
        self._listeners: set[Callable[[], None]] = set()
        self._held: dict[Control, HoldIntent] = {}
        self._controller: HoldCapable | None = None
        self._cancel_pending: Callable[[], None] | None = None
        self._closed = False
        self._submissions = 0
        self._rejected_messages = 0
        self._dropped_renewals = 0
        self._rejected_submissions = 0

    @callback
    def use_roster(self, roster: ControlRoster) -> None:
        """Adopt a replacement roster.

        Live intents keep the deadlines the previous roster clamped: a deadline
        is a value already taken, not a re-derivation.
        """
        self._roster = roster

    @callback
    def handle_samples(self, message: ResolvedSampleSet) -> None:
        """Apply one sender's complete active set.

        A message at or below its sender's high-water drops whole rather than
        being partly applied, and nothing queues waiting for a later one.

        Raises:
            RuntimeError: Thrown once ``quiesce`` has closed intake. The entry
                is going away, so a caller that still holds this reconstructor
                is told rather than answered with a silent drop.
        """
        self._require_open()
        sender = self._senders.get(message.sender)
        if sender is not None and message.seq <= sender.high_water:
            self._rejected_messages += 1
            return

        now = self._clock()
        if sender is None:
            self._senders[message.sender] = _SenderState(message.seq, now)
        else:
            sender.high_water = message.seq
            sender.last_activity = now

        # Lapse and evict first, so a sample naming a timed-out intent finds a
        # record that already knows it holds nothing.
        self._sweep(now)
        for sample in message.samples:
            self._apply(message.sender, sample, now)
        self._settle(now)

    @callback
    def submit(self, control: Control, action: IntentAction) -> Future[HoldOutcome]:
        """Assert control as a degenerate sender, returning the intent's outcome.

        The same door the wire's samples take: the control must declare the
        action and must not be a staged operation, and a ``Hold``'s ttl is
        clamped exactly as a sample's is.

        The future resolves ``COMPLETED`` when the intent ends after reaching an
        attached controller, ``INTERRUPTED`` on a stop or a quiesce, and
        ``FAILED`` when it was never expressed or a detach fell inside its span.

        Raises:
            RuntimeError: Thrown once ``quiesce`` has closed intake, as
                ``handle_samples`` is. An outcome would say the submission was
                taken and then lost; it never arrived.
            HomeAssistantError: Thrown when the control refuses the action.
                Every caller here is internal, so a refusal is this
                integration's own invariant failure rather than bad input:
                the bed module's declarations disagree with the code pressing
                them.
        """
        self._require_open()
        self._refuse_an_undeclared_submission(control, action)
        future: Future[HoldOutcome] = self.hass.loop.create_future()
        self._submissions += 1
        key = (SUBMISSION_SENDER, IntentId(f"submission-{self._submissions}"))
        now = self._clock()
        ttl_s = self._sample_ttl_s(control, action)
        self._outcomes[key] = future
        self._records[key] = _record(control, ttl_s, _IntentOrigin.ONE_SHOT, now)
        self._settle(now)
        return future

    @callback
    def stop(self, controls: Iterable[Control]) -> None:
        """End every hold intent on each control and fence it.

        The unconditional safety verb: a per-motor stop passes
        ``MotorControls.both``, which the roster composes so no caller pairs
        two directions itself. Each ended record is retained, so the stopped
        id's in-flight samples refresh it and hold nothing.
        """
        self._stop(set(controls))

    @callback
    def stop_all(self) -> None:
        """End every hold intent on the bed and fence every control."""
        self._stop(None)

    @callback
    def quiesce(self) -> None:
        """End every intent, push the empty set once, and close intake.

        Closed is the reconstructor's last phase: both intake doors raise
        afterwards, and only the entry's shutdown reaches this.
        """
        for key, record in self._records.items():
            if not record.ended:
                record.ended = True
                self._resolve(key, HoldOutcome.INTERRUPTED)
        self._records.clear()
        self._senders.clear()
        self._cancel_pending_event()
        self._closed = True
        if self._held:
            self._held = {}
            self._notify_listeners()
        self._push()

    @callback
    def attach(self, controller: HoldCapable) -> None:
        """Store the link's controller and push what it must express now.

        Only the live intents with no refreshing sender that were never
        expressed: an Activate or a direct submission that arrived while the
        link was down. A sample-borne hold waits for its sender's next sample,
        so a client that has gone away never replays.

        State-idempotent, because the coordinator publishes connection state
        rather than edges: re-reporting the controller already attached is a
        no-op, and pushes nothing a second time.

        This is where the controller learns its link is live, and it learns
        ahead of the push: a bed that owes its box a connect-time gesture runs
        it with nothing waiting behind it, rather than withholding the first
        command that needs it.
        """
        if self._controller is controller:
            return

        self._controller = controller
        controller.link_up()
        now = self._clock()
        self._sweep(now)
        arriving = _newest_per_control(
            record
            for record in self._records.values()
            if record.origin is _IntentOrigin.ONE_SHOT
            and not record.ended
            and not record.expressed
            and record.intent.deadline > now
        )
        controller.hold({control: intent.deadline for control, intent in arriving.items()})
        self._mark_expressed(arriving)
        self._settle(now, push=False)

    @callback
    def detach(self) -> None:
        """Drop the link's controller and end what that link was carrying.

        Every live intent's expression is lost, and every one-shot ends there
        rather than resuming after a reconnect: a caller awaiting one wakes with
        ``FAILED``. An ended record is retained for its own span, the way every
        other end path retains one, so the link's dead intents leave rather than
        staying nameable for the life of the entry. Sample-borne holds stay live
        to lapse or be refreshed. Nothing is pushed, so a link death starts no
        reconnect of its own.

        State-idempotent, for the same reason ``attach`` is: the coordinator
        reports a lost connection at the start of a connect attempt as well as
        at a link's end, so a detach with nothing attached is a no-op and ends
        no intent.

        This is also where the controller learns its link is gone. The
        coordinator's own drop paths null their controller reference at five
        sites and fan the disconnected state from three, and the reconstructor
        is the one holder that still has the controller when the fan-out
        arrives, so telling it here is what makes the edge single-authored.
        """
        controller = self._controller
        if controller is None:
            return

        controller.link_lost()
        self._controller = None
        now = self._clock()
        for key, record in self._records.items():
            if record.ended:
                continue
            record.expression_lost = True
            if record.origin is _IntentOrigin.ONE_SHOT:
                self._end(key, record, now)
        self._settle(now, push=False)

    @property
    def held(self) -> Mapping[Control, HoldIntent]:
        """Return the held set: the newest live contributor's intent per control."""
        return MappingProxyType(self._held)

    @property
    def holds_anything(self) -> bool:
        """Return True while any control is held."""
        return bool(self._held)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a held-set listener and return its unregister callback.

        Replay-latest: the listener runs once on registration, so a subscriber
        starts from the current set rather than from the next change.
        """
        self._listeners.add(listener)
        listener()

        def unregister() -> None:
            self._listeners.discard(listener)

        return unregister

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the reconstructor's state and counters for the diagnostics download."""
        return {
            "held": sorted(control.name for control in self._held),
            "records": len(self._records),
            "senders": len(self._senders),
            "attached": self._controller is not None,
            "rejected_messages": self._rejected_messages,
            "dropped_renewals": self._dropped_renewals,
            "rejected_submissions": self._rejected_submissions,
        }

    def _require_open(self) -> None:
        """Refuse an intake call the closed phase cannot honour."""
        if self._closed:
            raise RuntimeError("intake closed")

    def _refuse_an_undeclared_submission(
        self, control: Control, action: IntentAction
    ) -> None:
        """Refuse a submission the wire's sample door would have refused.

        Raises:
            HomeAssistantError: Thrown when the control stages bed-side or does
                not declare the action.
        """
        if self._roster.is_operation(control):
            raise HomeAssistantError(
                f"Control '{control.name}' stages bed-side and cannot be submitted as "
                "an intent"
            )
        kind = ActionKind.HOLD if isinstance(action, Hold) else ActionKind.ACTIVATE
        if not self._roster.supports(control, kind):
            raise HomeAssistantError(
                f"Control '{control.name}' does not support '{kind.value}'"
            )

    def _apply(self, sender: SenderId, sample: ResolvedSample, now: float) -> None:
        """Reconcile one sample against the record it names."""
        key = (sender, sample.intent_id)
        record = self._records.get(key)

        if isinstance(sample.action, Hold) and sample.action.ttl_ms == 0:
            self._release(key, record, now)
        elif record is None:
            self._records[key] = _record(
                sample.control,
                self._sample_ttl_s(sample.control, sample.action),
                _IntentOrigin.SAMPLED_HOLD
                if isinstance(sample.action, Hold)
                else _IntentOrigin.ONE_SHOT,
                now,
            )
        else:
            self._refresh(record, sample, now)

    def _release(
        self, key: tuple[SenderId, IntentId], record: _IntentRecord | None, now: float
    ) -> None:
        """End the record a ttl-0 sample names and retain it.

        An id with no record is one already evicted or never seen, and a hold
        of zero length holds nothing either way. The record is retained rather
        than deleted, so the press the controller is still draining stays
        nameable by a stop for as long as the client's own ttl says.
        """
        if record is None:
            return
        if record.ended:
            record.evict_at = now + record.retention_s
            return
        self._end(key, record, now)

    def _refresh(self, record: _IntentRecord, sample: ResolvedSample, now: float) -> None:
        """Move a record's retention, and its deadline while it still holds.

        A refresh replaces the deadline outright rather than extending it, so a
        caller can shorten a hold by asking for less. An Activate's deadline is
        fixed at its press start plus the roster duration and never moves. A
        refresh of a record whose hold has ended re-asserts nothing and only
        keeps the record, which is what fences a stopped or released press.
        """
        record.retention_s = self._sample_ttl_s(sample.control, sample.action)
        if record.ended:
            record.evict_at = now + record.retention_s
            self._dropped_renewals += 1
            return

        record.last_refresh = now
        if isinstance(sample.action, Hold):
            record.intent = replace(
                record.intent,
                deadline=self._bounded_deadline(record.intent, now + record.retention_s),
            )

    def _sample_ttl_s(self, control: Control, action: IntentAction) -> float:
        """Return one sample's hold length: a clamped ttl, or the roster's duration."""
        if isinstance(action, Hold):
            return self._roster.clamp_ttl_ms(control, action.ttl_ms) / 1000
        return self._roster.activate_duration_ms(control) / 1000

    def _bounded_deadline(self, intent: HoldIntent, deadline: float) -> Deadline:
        """Return a deadline capped at the press start plus the control's lifetime."""
        lifetime_s = self._roster.ttl_max_ms(intent.control) / 1000
        return Deadline(min(deadline, intent.began + lifetime_s))

    def _stop(self, controls: set[Control] | None) -> None:
        """End and fence the named controls, or every control when None.

        The controller hears the stop before the push that shrinks the set: a
        shrunken push alone reads as an intent that ended, which drains its
        press floor, and a stop drops the press with the floor unmet.

        The bed-wide case names every control a record still retains, not the
        derived held set: a press whose intent ended within the last press
        floor - a ttl-0 sample, or a lapse - is out of the held set and still
        draining at the controller, so naming the held set alone leaves the
        drain the stop was raised to end. A record outlives its hold by its own
        clamped ttl, which covers the floor for any client whose ttl exceeds
        it: the card's 750 ms against the CU170's 223 ms floor does.
        """
        now = self._clock()
        for key, record in self._records.items():
            if record.ended or (controls is not None and record.intent.control not in controls):
                continue
            record.ended = True
            record.evict_at = now + record.retention_s
            self._resolve(key, HoldOutcome.INTERRUPTED)
        controller = self._controller
        if controller is not None:
            controller.stop(
                frozenset(record.intent.control for record in self._records.values())
                if controls is None
                else frozenset(controls)
            )
        self._settle(now)

    def _settle(self, now: float, *, push: bool = True) -> None:
        """Sweep, re-derive the held set, publish any change, push, and re-arm."""
        self._sweep(now)
        held = self._derive(now)
        changed = held != self._held
        self._held = held
        if changed:
            self._notify_listeners()
        if push:
            self._push()
        self._arm_pending_event(now)

    def _sweep(self, now: float) -> None:
        """End lapsed holds, evict spent records, and forget quiet senders."""
        for key, record in list(self._records.items()):
            if not record.ended and record.intent.deadline <= now:
                self._lapse(key, record, now)
            if record.evict_at <= now:
                del self._records[key]

        for sender_id, state in list(self._senders.items()):
            if now - state.last_activity < _SENDER_QUIET_HORIZON_S:
                continue
            if any(key[0] == sender_id for key in self._records):
                continue
            del self._senders[sender_id]

    def _lapse(
        self, key: tuple[SenderId, IntentId], record: _IntentRecord, now: float
    ) -> None:
        """End a record whose deadline has passed, counting one never expressed."""
        if not record.expressed:
            self._rejected_submissions += 1
            _LOGGER.debug(
                "Hold intent on %s lapsed before it could be expressed",
                record.intent.control.name,
            )
        self._end(key, record, now)

    def _end(
        self, key: tuple[SenderId, IntentId], record: _IntentRecord, now: float
    ) -> None:
        """End a record's hold, start its retention, and resolve its outcome."""
        record.ended = True
        record.evict_at = now + record.retention_s
        outcome = (
            HoldOutcome.COMPLETED
            if record.expressed and not record.expression_lost
            else HoldOutcome.FAILED
        )
        self._resolve(key, outcome)

    def _derive(self, now: float) -> dict[Control, HoldIntent]:
        """Return the newest live contributor's intent per control."""
        return _newest_per_control(
            record
            for record in self._records.values()
            if not record.ended and record.intent.deadline > now
        )

    def _push(self) -> None:
        """Hand the held set to the controller, or summon one for it."""
        controller = self._controller
        if controller is None:
            if self._held:
                self._target.connect_on_demand()
            return

        controller.hold({control: intent.deadline for control, intent in self._held.items()})
        self._mark_expressed(self._held)

    def _mark_expressed(self, held: Mapping[Control, HoldIntent]) -> None:
        """Record that every live contributor to a pushed control reached the wire."""
        for record in self._records.values():
            if not record.ended and record.intent.control in held:
                record.expressed = True

    def _resolve(self, key: tuple[SenderId, IntentId], outcome: HoldOutcome) -> None:
        """Answer the submission awaiting this intent, if one is."""
        future = self._outcomes.pop(key, None)
        if future is not None and not future.done():
            # A caller Home Assistant cancelled has cancelled its own future.
            future.set_result(outcome)

    def _arm_pending_event(self, now: float) -> None:
        """Schedule one wake at the earliest lapse, eviction or sender horizon.

        One timer rather than one per record, so the wake count follows the
        change rate rather than the record count.
        """
        self._cancel_pending_event()
        instants = [
            record.evict_at for record in self._records.values() if record.ended
        ]
        instants += [
            record.intent.deadline for record in self._records.values() if not record.ended
        ]
        instants += [
            state.last_activity + _SENDER_QUIET_HORIZON_S
            for sender_id, state in self._senders.items()
            if not any(key[0] == sender_id for key in self._records)
        ]
        if not instants:
            return

        self._cancel_pending = async_call_later(
            self.hass, max(0.0, min(instants) - now), self._async_pending_event
        )

    def _cancel_pending_event(self) -> None:
        """Cancel any scheduled wake."""
        if self._cancel_pending is not None:
            self._cancel_pending()
            self._cancel_pending = None

    @callback
    def _async_pending_event(self, _fired_at: datetime) -> None:
        """Apply every lapse and eviction the wake was armed for."""
        self._cancel_pending = None
        self._settle(self._clock())

    def _notify_listeners(self) -> None:
        """Publish the held set to every subscriber.

        One listener's failure never costs the others their publication, which
        is how every fan-out in this integration behaves:
        ``BleAvailabilityTracker._async_notify_listeners`` and the coordinator's
        position, controller-state and connection-state fan-outs all log the
        error and continue.
        """
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as err:
                _LOGGER.warning("Held set listener error: %s", err)


def _record(
    control: Control, ttl_s: float, origin: _IntentOrigin, now: float
) -> _IntentRecord:
    """Return the record for a press beginning now and lasting ttl_s."""
    return _IntentRecord(
        intent=HoldIntent(control=control, began=now, deadline=Deadline(now + ttl_s)),
        origin=origin,
        retention_s=ttl_s,
        last_refresh=now,
    )


def _newest_per_control(records: Iterable[_IntentRecord]) -> dict[Control, HoldIntent]:
    """Return one intent per control: the contributor whose press began last.

    The published pair is one contributor's own, never a began from one and a
    deadline from another, so a de-assertion reverts both together.
    """
    newest: dict[Control, HoldIntent] = {}
    for record in records:
        current = newest.get(record.intent.control)
        if current is None or record.intent.began > current.began:
            newest[record.intent.control] = record.intent
    return newest
