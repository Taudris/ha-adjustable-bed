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

from .hold_roster import Control


class HoldCapable(ABC):
    """A controller that expresses a set of held controls."""

    @abstractmethod
    def hold(self, held: Mapping[Control, float]) -> None:
        """Replace the expressed set with held, each control mapped to its deadline.

        Synchronous: the push replaces the target set and returns without
        touching the wire, so a stop can end its intents and reach the
        controller with no suspension point between the two.
        """
