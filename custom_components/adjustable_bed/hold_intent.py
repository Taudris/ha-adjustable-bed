"""The values a hold intent is carried by, from the sample door inward.

A caller asserts a control by sampling an intent: a client-minted id, a control,
and an action. A ``Hold`` is asserted while samples refresh it and ends at ttl 0;
an ``Activate`` is one press whose duration the control's declaration carries,
so it takes no ttl of its own. The reconstructor turns either into a
``HoldIntent`` - a control with a deadline.

Units, stated once: a ttl is a duration in milliseconds as ``int``, matching the
integration's other ``duration_ms`` values. A press start and a deadline are
instants in seconds on the event loop's monotonic clock, and the deadline - the
one instant that crosses every tier - is a ``Deadline`` rather than a bare
float, because ``float`` alone no longer says which unit it carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

from .hold_roster import Control

# Two opaque client-minted ids that are never interchangeable and never parsed.
SenderId = NewType("SenderId", str)
IntentId = NewType("IntentId", str)

# The instant a hold ends, in seconds on the event loop's monotonic clock. Named
# because it is the value the reconstructor, the controller and the streamer all
# read, and the tier that mints one says so where it does.
Deadline = NewType("Deadline", float)


@dataclass(frozen=True, slots=True)
class Hold:
    """An assertion held while samples refresh it; a ttl of 0 releases it."""

    ttl_ms: int


@dataclass(frozen=True, slots=True)
class Activate:
    """One press, for the duration the control's declaration gives."""


IntentAction = Hold | Activate


@dataclass(frozen=True, slots=True)
class ResolvedSample:
    """One intent's state in one message, with its control already resolved.

    Named for what makes it different from the wire's sample, which the card
    calls an ``IntentSample`` and the handler takes as a dict: this one has
    passed the door, so its control is a ``Control`` the roster declared and
    its action is one of the two the control supports.
    """

    intent_id: IntentId
    control: Control
    action: IntentAction


@dataclass(frozen=True, slots=True)
class ResolvedSampleSet:
    """One sender's complete active set under one message seq, resolved."""

    sender: SenderId
    seq: int
    samples: tuple[ResolvedSample, ...]


@dataclass(frozen=True, slots=True)
class HoldIntent:
    """One live hold's server-side values, all on the reconstructor's clock.

    ``deadline`` is the effective one: the earlier of the last refresh plus the
    clamped ttl and the press start plus the control's lifetime cap. That is the
    instant the push and the publication carry.
    """

    control: Control
    began: float
    deadline: Deadline


class HoldOutcome(StrEnum):
    """How a direct submission's intent ended."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
