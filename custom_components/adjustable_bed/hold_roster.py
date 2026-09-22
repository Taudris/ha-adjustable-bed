"""Control declarations for beds that take hold intents.

A hold-capable controller declares one entry per control it can hold or
activate, and the coordinator instantiates a roster from those declarations when
that controller is built. The roster is the only holder of the control list: a
control name arriving from a service call either resolves here or is refused,
and every layer above shares the ``Control`` handles the declarations carry.

Beyond each control's press minimum, the roster carries no timing of its own.
Every other constant belongs to the controller, which knows its own bed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True, slots=True)
class Control:
    """One bed capability, with its direction part of its identity.

    Value equality is the point: the same name declared by two rosters compares
    equal, so a control survives a roster replacement as a dictionary key.
    """

    name: str


# Which way one motor is driven. Two controls, never a signed magnitude: the
# box takes one bit per direction and both together cancel out.
MotorDirection = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class MotorControls:
    """One motor's two direction controls.

    Named rather than positional, because which of a pair is which is the one
    thing a caller composing a tuple gets silently wrong.
    """

    up: Control
    down: Control

    @property
    def both(self) -> frozenset[Control]:
        """Return both directions, which is what a per-motor stop fences."""
        return frozenset({self.up, self.down})


class ActionKind(StrEnum):
    """A sample verb a control can declare support for."""

    HOLD = "hold"
    ACTIVATE = "activate"


class ControlMark(StrEnum):
    """A property of a control that changes which callers can reach it."""

    DELIBERATE_ONLY = "deliberate_only"


@dataclass(frozen=True, slots=True)
class PressFloor:
    """The minimum one press runs for: a frame count and a time, both met.

    One value rather than two cells, because neither half means anything
    alone - a press ends when both are satisfied, so nothing reads one without
    the other.
    """

    frames: int
    ms: int


@dataclass(frozen=True, slots=True)
class HoldSupport:
    """What a control a client can hold declares: how long it may hold it."""

    ttl_max_ms: int


@dataclass(frozen=True, slots=True)
class ActivateSupport:
    """What a control whose Activate is one press declares: how long the press is."""

    duration_ms: int


@dataclass(frozen=True, slots=True)
class StagedActivate:
    """An Activate the controller stages bed-side, so it prices no duration here.

    The stages and their ceilings are the controller's, because only it knows
    what its box acknowledges and when.
    """


@dataclass(frozen=True, slots=True)
class ControlDeclarationInputs:
    """What a controller reads from the config entry to declare its controls.

    The entry's own configurable values, handed to the controller rather than
    read off the coordinator behind it, so a declaration set can be built and
    asserted on without one. What the bed itself carries is the controller's:
    it holds its profile and needs nothing here to say so.
    """

    motor_pulse_count: int
    motor_pulse_delay_ms: int
    has_massage: bool


@dataclass(frozen=True, slots=True)
class ControlDeclaration:
    """What a bed module declares about one control.

    One sub-descriptor per action, present exactly when the control supports
    it, so the values an action needs arrive with it: there is no ttl cap on a
    control nothing can hold and no duration on one that stages. Which actions
    a control supports is read off them rather than declared a second time.

    ``aliases`` are further names the same control answers to: an older motor
    vocabulary a service call may carry, and the entity key of the button that
    renders the control, so a preset tile resolves its own control without
    composing a name. An alias resolves to this declaration's control, so no
    second control - and no second bit - exists behind it.
    """

    control: Control
    press_floor: PressFloor
    hold: HoldSupport | None = None
    activate: ActivateSupport | StagedActivate | None = None
    marks: frozenset[ControlMark] = field(default_factory=frozenset)
    aliases: frozenset[str] = field(default_factory=frozenset)

    @property
    def actions(self) -> frozenset[ActionKind]:
        """Return the sample verbs this control supports."""
        kinds = set()
        if self.hold is not None:
            kinds.add(ActionKind.HOLD)
        if self.activate is not None:
            kinds.add(ActionKind.ACTIVATE)
        return frozenset(kinds)

    @property
    def is_operation(self) -> bool:
        """Return True when this control's Activate stages bed-side."""
        return isinstance(self.activate, StagedActivate)


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
        self._aliases = {
            alias: declaration
            for declaration in self._declarations.values()
            for alias in declaration.aliases
        }

    @classmethod
    def empty(cls) -> ControlRoster:
        """Return a roster declaring no control."""
        return cls(())

    @property
    def controls(self) -> tuple[Control, ...]:
        """Return every declared control, once each and never under an alias."""
        return tuple(declaration.control for declaration in self._declarations.values())

    def find(self, name: str) -> Control | None:
        """Return the control declared or aliased under name, or None.

        The boundary form, for a name arriving from a service call. Callers
        holding a resolved control use the accessors below instead.
        """
        declaration = self._declarations.get(name) or self._aliases.get(name)
        return None if declaration is None else declaration.control

    def motor(self, motor: str) -> MotorControls | None:
        """Return the motor's two direction controls, or None off a hold bed.

        The one author of which two controls form a motor: a bed module
        declares a motor with both its directions or with neither, and a caller
        that composed the pair itself could transpose it silently.
        """
        up = self.find(motor_control_name(motor, "up"))
        down = self.find(motor_control_name(motor, "down"))
        return None if up is None or down is None else MotorControls(up=up, down=down)

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
        return self.declaration(control).is_operation

    def ttl_max_ms(self, control: Control) -> int:
        """Return how long the control may be held.

        Raises:
            ValueError: Thrown when the control declares no Hold.
        """
        return self._hold(control).ttl_max_ms

    def clamp_ttl_ms(self, control: Control, ttl_ms: int) -> int:
        """Return ttl_ms bounded by the control's declared maximum.

        Raises:
            ValueError: Thrown when the control declares no Hold.
        """
        return min(ttl_ms, self._hold(control).ttl_max_ms)

    def press_floor(self, control: Control) -> PressFloor:
        """Return the minimum one press of the control runs for."""
        return self.declaration(control).press_floor

    def minimum_press_ms(self, control: Control) -> int:
        """Return the shortest press the bed registers on the control.

        The one author of "how long a press with no duration asked for lasts",
        which a service call omitting its duration and a controller pressing a
        Hold-only key both need.
        """
        return self.declaration(control).press_floor.ms

    def activate_duration_ms(self, control: Control) -> int:
        """Return how long an Activate on the control holds it.

        Raises:
            ValueError: Thrown when the control's Activate is staged bed-side,
                or it declares none.
        """
        activate = self.declaration(control).activate
        if not isinstance(activate, ActivateSupport):
            raise ValueError(f"Control '{control.name}' declares no timed Activate")
        return activate.duration_ms

    @property
    def declares_hold(self) -> bool:
        """Return True when any control declares Hold, which makes the bed hold-capable."""
        return any(
            declaration.hold is not None for declaration in self._declarations.values()
        )

    def _hold(self, control: Control) -> HoldSupport:
        """Return what the control declares about being held.

        Raises:
            ValueError: Thrown when the control declares no Hold.
        """
        hold = self.declaration(control).hold
        if hold is None:
            raise ValueError(f"Control '{control.name}' declares no Hold")
        return hold


def motor_control_name(motor: str, direction: MotorDirection) -> str:
    """Return the control name for one motor driven one way."""
    return f"motor-{motor}-{direction}"


def preset_control_name(preset: int | str) -> str:
    """Return the control name for recalling one preset.

    A preset is a memory slot number or a bed's own name for a fixed position,
    and one convention names both, so the name has one author either way.
    """
    return f"preset-{preset}"
