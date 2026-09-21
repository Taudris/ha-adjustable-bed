"""Control declarations for beds that take hold intents.

A bed module declares one entry per control it can hold or activate, and the
coordinator instantiates a roster from those declarations at config-entry setup.
The roster is the only holder of the control list: a control name arriving from
a service call either resolves here or is refused, and every layer above shares
the ``Control`` handles the declarations carry.

Beyond each control's press minimum, the roster carries no timing of its own.
Every other constant belongs to the controller, which knows its own bed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import AdjustableBedCoordinator


@dataclass(frozen=True, slots=True)
class Control:
    """One bed capability, with its direction part of its identity.

    Value equality is the point: the same name declared by two rosters compares
    equal, so a control survives a roster replacement as a dictionary key.
    """

    name: str


class ActionKind(StrEnum):
    """A sample verb a control can declare support for."""

    HOLD = "hold"
    ACTIVATE = "activate"


class ControlMark(StrEnum):
    """A property of a control that changes which callers can reach it."""

    OPERATION = "operation"
    DELIBERATE_ONLY = "deliberate_only"


@dataclass(frozen=True, slots=True)
class ControlDeclaration:
    """What a bed module declares about one control."""

    control: Control
    actions: frozenset[ActionKind]
    ttl_max_ms: int
    activate_duration_ms: int | None
    press_min_frames: int
    press_min_ms: int
    marks: frozenset[ControlMark] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Check that the declared actions and the Activate duration agree."""
        if (ActionKind.ACTIVATE in self.actions) != (self.activate_duration_ms is not None):
            raise ValueError(
                f"Control '{self.control.name}' declares an Activate duration if and only if "
                "it supports Activate"
            )


class ControlRoster:
    """The controls one bed declares, keyed by name.

    Immutable: the coordinator builds one per config entry and replaces it whole
    when a connect-time protocol correction changes the bed type, so a holder
    never sees a roster change under it.
    """

    def __init__(self, declarations: Iterable[ControlDeclaration]) -> None:
        """Initialize the roster from a bed module's declarations."""
        self._declarations = {
            declaration.control.name: declaration for declaration in declarations
        }

    @classmethod
    def empty(cls) -> ControlRoster:
        """Return a roster declaring no control."""
        return cls(())

    @property
    def controls(self) -> tuple[Control, ...]:
        """Return every declared control."""
        return tuple(declaration.control for declaration in self._declarations.values())

    def find(self, name: str) -> Control | None:
        """Return the control declared under name, or None.

        The boundary form, for a name arriving from a service call. Callers
        holding a resolved control use the accessors below instead.
        """
        declaration = self._declarations.get(name)
        return None if declaration is None else declaration.control

    def declaration(self, control: Control) -> ControlDeclaration:
        """Return what the bed module declared about control.

        Raises:
            KeyError: Thrown when this roster did not declare the control.
        """
        return self._declarations[control.name]

    def supports(self, control: Control, action: ActionKind) -> bool:
        """Return True when the control declares support for the action."""
        return action in self.declaration(control).actions

    def is_operation(self, control: Control) -> bool:
        """Return True when the control's expression is staged bed-side."""
        return ControlMark.OPERATION in self.declaration(control).marks

    def clamp_ttl_ms(self, control: Control, ttl_ms: int) -> int:
        """Return ttl_ms bounded by the control's declared maximum."""
        return min(ttl_ms, self.declaration(control).ttl_max_ms)

    def activate_duration_ms(self, control: Control) -> int:
        """Return how long an Activate on the control holds it.

        Raises:
            ValueError: Thrown when the control declares no Activate.
        """
        duration_ms = self.declaration(control).activate_duration_ms
        if duration_ms is None:
            raise ValueError(f"Control '{control.name}' declares no Activate")
        return duration_ms

    @property
    def declares_hold(self) -> bool:
        """Return True when any control declares Hold, which makes the bed hold-capable."""
        return any(
            ActionKind.HOLD in declaration.actions for declaration in self._declarations.values()
        )


def motor_control_name(motor: str, direction: str) -> str:
    """Return the control name for one motor driven one way."""
    return f"motor-{motor}-{direction}"


def preset_control_name(slot: int) -> str:
    """Return the control name for recalling one memory slot."""
    return f"preset-{slot}"


async def load_control_declarations(
    coordinator: AdjustableBedCoordinator,
) -> tuple[ControlDeclaration, ...]:
    """Return the bed module's control declarations for this entry.

    Asynchronous because resolving a bed module imports it, which runs off the
    event loop. No bed module declares a control, so every bed gets an empty
    roster and no bed is hold-capable.
    """
    del coordinator  # The lookup lands with the first bed module that declares
    return ()
