"""The capability a controller opts into to take a held set.

A controller class inherits ``HoldCapable`` beside ``BedController`` to declare
that it can express a set of held controls rather than a finite pulse per
command. The relationship is the whole declaration: ``isinstance`` is how every
call site asks whether this bed's author opted in, so no capability flag sits
beside it saying the same thing twice.

Nominal rather than structural on purpose. A ``runtime_checkable`` protocol
would answer yes for any controller that happened to grow a ``hold`` method,
which is not the question being asked.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from .hold_intent import Deadline
from .hold_roster import Control, ControlDeclaration, ControlDeclarationInputs


class HoldCapable(ABC):
    """A controller that expresses a set of held controls."""

    @abstractmethod
    def control_declarations(
        self, inputs: ControlDeclarationInputs
    ) -> tuple[ControlDeclaration, ...]:
        """Return the controls this bed has, for the profile this link resolved.

        The controller is the only holder of what its box actually carries: the
        app profile it was built with, and the bed type a connect-time
        correction settled. So the roster follows the controller that exists
        rather than a table keyed on what the entry was configured with, and a
        control the profile lacks is never declared for a caller to reach.

        ``inputs`` carry the entry's configurable values, which an Activate's
        duration is priced against; everything else is the controller's own.
        """

    @abstractmethod
    def link_up(self) -> None:
        """Take up what this link owes its bed before anything else is expressed.

        The opening counterpart of ``link_lost``, called once per link ahead of
        the first push, so a bed that owes its box a connect-time gesture runs
        it with nothing waiting behind it. Deferring the gesture to the first
        command that needs it withholds that command for the gesture's span,
        which on a card tap outlasts the tap.

        Synchronous, and a bed that owes its box nothing implements it as a
        no-op.
        """

    @abstractmethod
    def hold(self, held: Mapping[Control, Deadline]) -> None:
        """Replace the expressed set with held, each control mapped to its deadline.

        Synchronous: the push replaces the target set and returns without
        touching the wire, so a stop can end its intents and reach the
        controller with no suspension point between the two.
        """

    @abstractmethod
    def stop(self, controls: frozenset[Control]) -> None:
        """Drop these controls' presses now, their press floors met or not.

        Synchronous, and it arrives ahead of the push that shrinks the set: a
        shrunken push alone cannot tell a stop from an intent that merely
        ended, and the second of those still drains its floor. A control the
        stop drops waits out its clear floor before it can be pressed again.
        """

    @abstractmethod
    def release_wire(self) -> None:
        """Drop every expressed bit and submit the bed's release.

        Synchronous, and nothing waits on the release: a disconnect the
        controller orders follows immediately behind the submission. An
        unsolicited drop takes ``link_lost`` instead - ending that motion is
        the box's own watchdog.
        """

    @abstractmethod
    def link_lost(self) -> None:
        """End everything this link was carrying, writing nothing.

        The unsolicited counterpart of ``release_wire``: the link is already
        gone, so no frame can leave. Anything the controller runs on the link's
        own lifetime ends here - a paced stream stops and a staged operation
        fails - rather than idling out its deadlines against a dead client.
        Synchronous, and idempotent after a release.
        """
