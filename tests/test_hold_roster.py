"""Tests for the control roster's declarations.

Covers what a bed module can say about a control and what the roster answers
about it: supported actions, the ttl clamp, the Activate duration, the operation
mark, and whether the bed is hold-capable at all.
"""

from __future__ import annotations

import pytest

from custom_components.adjustable_bed.hold_roster import (
    ActionKind,
    Control,
    ControlDeclaration,
    ControlMark,
    ControlRoster,
    load_control_declarations,
    motor_control_name,
    preset_control_name,
)

HEAD_UP = Control("motor-head-up")
PRESET_1 = Control("preset-1")
LIGHT = Control("light-toggle")
PING = Control("ping")
STORE_1 = Control("store-preset-1")


def _motor(control: Control = HEAD_UP) -> ControlDeclaration:
    """Return a motor declaration: both actions, a 30 s cap, a pulse duration."""
    return ControlDeclaration(
        control=control,
        actions=frozenset({ActionKind.HOLD, ActionKind.ACTIVATE}),
        ttl_max_ms=30000,
        activate_duration_ms=1000,
        press_min_frames=1,
        press_min_ms=223,
    )


def _preset(control: Control = PRESET_1) -> ControlDeclaration:
    """Return a preset declaration: Hold only, no Activate duration."""
    return ControlDeclaration(
        control=control,
        actions=frozenset({ActionKind.HOLD}),
        ttl_max_ms=30000,
        activate_duration_ms=None,
        press_min_frames=1,
        press_min_ms=223,
    )


def _light() -> ControlDeclaration:
    """Return a light declaration: Activate only."""
    return ControlDeclaration(
        control=LIGHT,
        actions=frozenset({ActionKind.ACTIVATE}),
        ttl_max_ms=1000,
        activate_duration_ms=223,
        press_min_frames=1,
        press_min_ms=223,
    )


def _ping() -> ControlDeclaration:
    """Return the link benchmark's declaration: Hold only, no bits."""
    return ControlDeclaration(
        control=PING,
        actions=frozenset({ActionKind.HOLD}),
        ttl_max_ms=30000,
        activate_duration_ms=None,
        press_min_frames=1,
        press_min_ms=223,
    )


def _store() -> ControlDeclaration:
    """Return an operation control's declaration: Activate only, marked."""
    return ControlDeclaration(
        control=STORE_1,
        actions=frozenset({ActionKind.ACTIVATE}),
        ttl_max_ms=1000,
        activate_duration_ms=223,
        press_min_frames=1,
        press_min_ms=223,
        marks=frozenset({ControlMark.OPERATION}),
    )


class TestControl:
    """Test the identity handle every layer shares."""

    def test_controls_of_one_name_compare_equal(self):
        """Value equality is what lets a control key a dict across rosters."""
        assert Control("motor-head-up") == HEAD_UP
        assert {HEAD_UP: 1}[Control("motor-head-up")] == 1

    def test_control_names_carry_motor_direction_and_preset_slot(self):
        """Direction is part of a motor's identity, and a slot part of a preset's."""
        assert motor_control_name("head", "up") == "motor-head-up"
        assert motor_control_name("head", "down") == "motor-head-down"
        assert preset_control_name(2) == "preset-2"


class TestRosterDeclaresActions:
    """roster-declares-actions: what a control supports and for how long."""

    def test_a_control_declares_hold_activate_or_both(self):
        """roster-declares-actions: each control answers for each action."""
        roster = ControlRoster((_motor(), _preset(), _light(), _ping()))

        assert roster.supports(HEAD_UP, ActionKind.HOLD)
        assert roster.supports(HEAD_UP, ActionKind.ACTIVATE)
        assert roster.supports(PRESET_1, ActionKind.HOLD)
        assert not roster.supports(PRESET_1, ActionKind.ACTIVATE)
        assert roster.supports(LIGHT, ActionKind.ACTIVATE)
        assert not roster.supports(LIGHT, ActionKind.HOLD)

    def test_a_preset_declares_no_activate_duration(self):
        """roster-declares-actions: a preset holds and never activates."""
        roster = ControlRoster((_preset(),))

        with pytest.raises(ValueError, match="declares no Activate"):
            roster.activate_duration_ms(PRESET_1)

    def test_a_declaration_pairs_its_activate_duration_with_its_actions(self):
        """roster-declares-actions: one fact, so the two cells cannot disagree."""
        with pytest.raises(ValueError, match="if and only if"):
            ControlDeclaration(
                control=PRESET_1,
                actions=frozenset({ActionKind.HOLD}),
                ttl_max_ms=30000,
                activate_duration_ms=500,
                press_min_frames=1,
                press_min_ms=223,
            )
        with pytest.raises(ValueError, match="if and only if"):
            ControlDeclaration(
                control=LIGHT,
                actions=frozenset({ActionKind.ACTIVATE}),
                ttl_max_ms=1000,
                activate_duration_ms=None,
                press_min_frames=1,
                press_min_ms=223,
            )

    def test_a_ttl_past_the_cap_clamps_and_one_under_it_does_not(self):
        """roster-declares-actions: a 30 s cap leaves a full timed_move unclamped."""
        roster = ControlRoster((_motor(),))

        assert roster.clamp_ttl_ms(HEAD_UP, 30000) == 30000
        assert roster.clamp_ttl_ms(HEAD_UP, 45000) == 30000
        assert roster.clamp_ttl_ms(HEAD_UP, 800) == 800

    def test_a_motors_activate_duration_is_the_declared_pulse(self):
        """roster-declares-actions: an Activate's bound is the roster's."""
        roster = ControlRoster((_motor(),))

        assert roster.activate_duration_ms(HEAD_UP) == 1000

    async def test_no_bed_module_declares_a_control_yet(self):
        """roster-declares-actions: Hold needs a HoldCapable controller class.

        No bed module declares anything, so no roster declares Hold and no bed
        is hold-capable. The declarations land with the first controller class
        that opts in.
        """
        assert await load_control_declarations(None) == ()
        assert ControlRoster.empty().declares_hold is False

    def test_a_roster_declaring_any_hold_is_hold_capable(self):
        """roster-declares-actions: the roster answers the hold-capability question."""
        assert ControlRoster((_light(),)).declares_hold is False
        assert ControlRoster((_light(), _preset())).declares_hold is True


class TestRosterLookup:
    """Test the boundary form and the resolved form."""

    def test_an_undeclared_name_resolves_to_nothing(self):
        """A name off a service call either resolves or is refused by the caller."""
        roster = ControlRoster((_motor(),))

        assert roster.find("motor-head-up") == HEAD_UP
        assert roster.find("motor-nose-up") is None

    def test_reading_an_undeclared_control_raises(self):
        """Every accessor caller holds a declared control, so this is a defect."""
        roster = ControlRoster((_motor(),))

        with pytest.raises(KeyError):
            roster.declaration(PRESET_1)

    def test_the_roster_lists_every_declared_control(self):
        """The control list is the roster's and nobody else's."""
        roster = ControlRoster((_motor(), _preset(), _light()))

        assert set(roster.controls) == {HEAD_UP, PRESET_1, LIGHT}
        assert ControlRoster.empty().controls == ()


class TestOperationControls:
    """operation-controls-command-path-only: the mark the handler refuses on."""

    def test_a_staged_operation_is_marked(self):
        """operation-controls-command-path-only: the roster carries the mark."""
        roster = ControlRoster((_store(), _motor()))

        assert roster.is_operation(STORE_1) is True
        assert roster.is_operation(HEAD_UP) is False

    def test_deliberate_only_is_a_second_mark_on_the_same_control(self):
        """operation-controls-command-path-only: consent gating rides beside the mark."""
        reset = Control("factory-reset")
        roster = ControlRoster(
            (
                ControlDeclaration(
                    control=reset,
                    actions=frozenset({ActionKind.ACTIVATE}),
                    ttl_max_ms=1000,
                    activate_duration_ms=223,
                    press_min_frames=1,
                    press_min_ms=223,
                    marks=frozenset({ControlMark.OPERATION, ControlMark.DELIBERATE_ONLY}),
                ),
            )
        )

        assert roster.is_operation(reset) is True
        assert ControlMark.DELIBERATE_ONLY in roster.declaration(reset).marks


class TestPing:
    """ping: the link benchmark's declaration half."""

    def test_ping_holds_and_never_activates(self):
        """ping: a Hold-only control with no bits of its own."""
        roster = ControlRoster((_ping(),))

        assert roster.supports(PING, ActionKind.HOLD)
        assert not roster.supports(PING, ActionKind.ACTIVATE)
        assert roster.is_operation(PING) is False
        with pytest.raises(ValueError):
            roster.activate_duration_ms(PING)
