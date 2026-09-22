"""Tests for the control roster's declarations.

Covers what a hold-capable controller can say about a control and what the
roster answers about it: supported actions, the ttl clamp, the Activate
duration, the operation mark, and whether the bed is hold-capable at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adjustable_bed.beds.leggett_okin import LeggettOkinController
from custom_components.adjustable_bed.beds.leggett_okin_hold import (
    HOLD_TTL_MAX_MS,
    PING_TTL_MAX_MS,
    PRESS_FLOOR_MS,
)
from custom_components.adjustable_bed.const import (
    BED_TYPE_LEGGETT_OKIN,
    BED_TYPE_LEGGETT_PLATT,
    CONF_BED_TYPE,
    CONF_PROTOCOL_VARIANT,
    DOMAIN,
    VARIANT_AUTO,
)
from custom_components.adjustable_bed.coordinator import AdjustableBedCoordinator
from custom_components.adjustable_bed.hold_roster import (
    ActionKind,
    ActivateSupport,
    Control,
    ControlDeclaration,
    ControlDeclarationInputs,
    ControlMark,
    ControlRoster,
    HoldSupport,
    PressFloor,
    StagedActivate,
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
        press_floor=PressFloor(frames=1, ms=223),
        hold=HoldSupport(ttl_max_ms=30000),
        activate=ActivateSupport(duration_ms=1000),
    )


def _preset(control: Control = PRESET_1) -> ControlDeclaration:
    """Return a preset declaration: Hold only, no Activate."""
    return ControlDeclaration(
        control=control,
        press_floor=PressFloor(frames=1, ms=223),
        hold=HoldSupport(ttl_max_ms=30000),
    )


def _light() -> ControlDeclaration:
    """Return a light declaration: Activate only."""
    return ControlDeclaration(
        control=LIGHT,
        press_floor=PressFloor(frames=1, ms=223),
        activate=ActivateSupport(duration_ms=223),
    )


def _ping() -> ControlDeclaration:
    """Return the link benchmark's declaration: Hold only, no bits."""
    return ControlDeclaration(
        control=PING,
        press_floor=PressFloor(frames=1, ms=223),
        hold=HoldSupport(ttl_max_ms=30000),
    )


def _store() -> ControlDeclaration:
    """Return an operation control's declaration: a staged Activate, unpriced."""
    return ControlDeclaration(
        control=STORE_1,
        press_floor=PressFloor(frames=1, ms=223),
        activate=StagedActivate(),
    )


def _inputs(
    bed_type: str = BED_TYPE_LEGGETT_OKIN,
    *,
    pulse_count: int = 10,
    pulse_delay_ms: int = 100,
    has_massage: bool = True,
) -> ControlDeclarationInputs:
    """Return the entry values a bed module reads to declare its controls."""
    return ControlDeclarationInputs(
        motor_pulse_count=pulse_count,
        motor_pulse_delay_ms=pulse_delay_ms,
        has_massage=has_massage,
    )


def _declarations(app_profile: str = "prodigy4", **overrides):
    """Return what an Okin controller on one app profile declares for an entry."""
    controller = LeggettOkinController(MagicMock(), app_profile=app_profile)
    return controller.control_declarations(_inputs(**overrides))


def _cu170(app_profile: str = "prodigy4", **overrides) -> ControlRoster:
    """Return the roster an Okin controller on this profile declares."""
    return ControlRoster(_declarations(app_profile, **overrides))


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

    def test_the_roster_pairs_a_motors_two_directions(self):
        """The one author of which two controls form a motor, so none transposes them."""
        roster = _cu170()

        motor = roster.motor("head")

        assert motor is not None
        assert motor.up == Control("motor-head-up")
        assert motor.down == Control("motor-head-down")
        assert motor.both == frozenset({motor.up, motor.down})
        assert roster.motor("tv_lift") is None


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

        with pytest.raises(ValueError, match="no timed Activate"):
            roster.activate_duration_ms(PRESET_1)

    def test_an_operation_control_prices_no_activate_duration(self):
        """operation-controls-command-path-only: its Activate stages bed-side.

        The type is the check: a staged Activate has no duration cell to fill,
        so no declaration can carry one beside it.
        """
        roster = ControlRoster((_store(),))

        with pytest.raises(ValueError, match="no timed Activate"):
            roster.activate_duration_ms(STORE_1)

    def test_a_declaration_carries_only_the_cells_its_actions_need(self):
        """roster-declares-actions: one sub-descriptor per action, or none.

        A control nothing can hold has no ttl cap to read wrongly, and one that
        never activates has no duration - so no invariant pairs two cells that
        could disagree.
        """
        preset = _preset()
        light = _light()

        assert preset.actions == frozenset({ActionKind.HOLD})
        assert preset.activate is None
        assert light.actions == frozenset({ActionKind.ACTIVATE})
        assert light.hold is None

        roster = ControlRoster((preset, light))

        with pytest.raises(ValueError, match="declares no Hold"):
            roster.ttl_max_ms(LIGHT)

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

    async def test_a_controller_that_is_not_hold_capable_gets_an_empty_roster(
        self, hass: HomeAssistant, mock_config_entry
    ):
        """roster-declares-actions: Hold needs a HoldCapable controller class.

        Every controller but the CU170's declares no control, so the roster a
        coordinator adopts from one declares no Hold and that bed keeps the
        pulse path on every door.
        """
        coordinator = AdjustableBedCoordinator(hass, mock_config_entry)
        coordinator._controller = MagicMock()

        coordinator.adopt_control_roster()

        assert coordinator.control_roster.controls == ()
        assert coordinator.control_roster.declares_hold is False

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

    def test_deliberate_only_is_a_mark_beside_a_staged_activate(self):
        """operation-controls-command-path-only: consent gating rides beside staging."""
        reset = Control("factory-reset")
        roster = ControlRoster(
            (
                ControlDeclaration(
                    control=reset,
                    press_floor=PressFloor(frames=1, ms=223),
                    activate=StagedActivate(),
                    marks=frozenset({ControlMark.DELIBERATE_ONLY}),
                ),
            )
        )

        assert roster.is_operation(reset) is True
        assert ControlMark.DELIBERATE_ONLY in roster.declaration(reset).marks


class TestCu170Declarations:
    """roster-declares-actions: the CU170's own rows, built from its bed module."""

    def test_a_motor_holds_and_activates_and_a_preset_only_holds(self):
        """roster-declares-actions: each control's actions, as the bed declares them."""
        roster = _cu170()

        assert roster.supports(HEAD_UP, ActionKind.HOLD)
        assert roster.supports(HEAD_UP, ActionKind.ACTIVATE)
        for name in ("preset-1", "preset-flat", "preset-dummy", "ping"):
            control = roster.find(name)
            assert control is not None
            assert roster.supports(control, ActionKind.HOLD)
            assert not roster.supports(control, ActionKind.ACTIVATE)
        for name in ("light-toggle", "massage-toggle", "massage-wave-step"):
            control = roster.find(name)
            assert control is not None
            assert roster.supports(control, ActionKind.ACTIVATE)
            assert not roster.supports(control, ActionKind.HOLD)

    async def test_an_auto_variant_leggett_entry_takes_its_okin_controllers_roster(
        self, hass: HomeAssistant, mock_config_entry_data: dict
    ):
        """roster-declares-actions: the roster follows the controller, not the entry.

        A Leggett & Platt entry left on the default auto variant resolves to an
        Okin controller at connect, and its bed type never changes, so a roster
        keyed on the entry's own bed type and variant had no row for the pair
        and left this bed with an empty one under a hold-capable controller.
        """
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                **mock_config_entry_data,
                CONF_BED_TYPE: BED_TYPE_LEGGETT_PLATT,
                CONF_PROTOCOL_VARIANT: VARIANT_AUTO,
            },
            unique_id="AA:BB:CC:DD:EE:F1",
        )
        entry.add_to_hass(hass)
        coordinator = AdjustableBedCoordinator(hass, entry)
        assert coordinator.control_roster.controls == ()

        coordinator._controller = LeggettOkinController(MagicMock())
        coordinator.adopt_control_roster()

        assert coordinator.control_roster.find("motor-head-up") == HEAD_UP
        assert coordinator.control_roster.declares_hold is True

    def test_the_bed_declares_its_twenty_six_controls(self):
        """roster-declares-actions: the whole roster, and no control twice."""
        roster = _cu170()

        assert len(roster.controls) == 26
        assert len(set(roster.controls)) == 26
        assert roster.declares_hold is True

    def test_a_u_series_profile_declares_only_what_it_reaches(self):
        """roster-declares-actions: a profile's roster is the controls it has.

        Two memory slots, so no slot 3 - which is also this protocol's fixed
        anti-snore entry - and no slot 4; no settings, so neither mode chord and
        no store; no lumbar either, and the disarming key stays inside the
        profile its press is confirmed on.
        """
        roster = _cu170("useries")

        assert roster.find("preset-1") is not None
        assert roster.find("preset-2") is not None
        for name in ("preset-3", "preset-4", "preset-dummy"):
            assert roster.find(name) is None
        for name in ("factory-reset", "latch-mode-enable", "store-preset-1"):
            assert roster.find(name) is None
        assert roster.find("motor-lumbar-up") is None
        assert roster.find("motor-head-up") == HEAD_UP

    def test_a_profile_without_an_actuator_declares_neither_direction(self):
        """roster-declares-actions: a sample cannot drive a motor the bed lacks."""
        prodigy2 = _cu170("prodigy2")
        prodigy2l = _cu170("prodigy2l")

        assert prodigy2.find("motor-lumbar-up") is None
        assert prodigy2.find("motor-lumbar-down") is None
        assert prodigy2.find("motor-pillow-up") is not None
        assert prodigy2l.find("motor-pillow-up") is None
        assert prodigy2l.find("motor-lumbar-up") is not None

    def test_a_motors_activate_duration_is_the_configured_pulse(self):
        """roster-declares-actions: the entry's pulse as wall-clock time."""
        roster = _cu170()

        assert roster.activate_duration_ms(HEAD_UP) == 1000

    @pytest.mark.parametrize("stored_delay", [0, -50, 1])
    async def test_an_unsafe_pulse_delay_floors_at_the_proven_cadence(
        self, hass: HomeAssistant, stored_delay: int
    ):
        """roster-declares-actions: a zero or negative value yields a real press."""
        roster = _cu170(pulse_delay_ms=stored_delay)

        assert roster.activate_duration_ms(HEAD_UP) == 1000

    def test_a_pulse_shorter_than_the_press_floor_lifts_to_it(self):
        """roster-declares-actions: a one-frame pulse still registers at the bed."""
        roster = _cu170(pulse_count=1)

        assert roster.activate_duration_ms(HEAD_UP) == PRESS_FLOOR_MS

    def test_a_motor_and_a_preset_hold_for_thirty_seconds(self):
        """roster-declares-actions: a full timed_move or goto_preset is unclamped."""
        roster = _cu170()
        preset = roster.find("preset-1")
        assert preset is not None

        assert roster.clamp_ttl_ms(HEAD_UP, 30000) == HOLD_TTL_MAX_MS
        assert roster.clamp_ttl_ms(preset, 45000) == HOLD_TTL_MAX_MS

    def test_massage_controls_follow_the_entrys_massage_flag(self):
        """roster-declares-actions: the button platform's gate, on the roster too."""
        with_massage = _cu170(has_massage=True)
        without = _cu170(has_massage=False)

        assert with_massage.find("massage-head-down") is not None
        assert without.find("massage-head-down") is None
        assert len(without.controls) == 20

    def test_an_older_motor_name_resolves_to_the_same_control(self):
        """roster-declares-actions: an alias is a name, never a second bit."""
        roster = _cu170()

        assert roster.find("motor-back-up") == HEAD_UP
        assert roster.find("motor-legs-down") == Control("motor-feet-down")
        assert roster.find("motor-tilt-up") == Control("motor-pillow-up")
        assert Control("motor-back-up") not in roster.controls

    @pytest.mark.parametrize(
        ("entity_key", "control"),
        [
            ("preset_flat", "preset-flat"),
            ("preset_dummy", "preset-dummy"),
            ("preset_memory_1", "preset-1"),
            ("preset_memory_2", "preset-2"),
            ("preset_memory_3", "preset-3"),
            ("preset_memory_4", "preset-4"),
            ("preset_anti_snore", "preset-3"),
        ],
    )
    async def test_each_preset_button_key_resolves_its_control(
        self, hass: HomeAssistant, entity_key: str, control: str
    ):
        """roster-declares-actions: the roster is the sole author of a preset's name.

        Every preset control answers to the entity key of the button that
        renders it, so the button platform composes nothing. Anti-snore and
        memory 3 are one command at one value on this bed, so both keys resolve
        to slot 3 and no second control exists behind either.
        """
        roster = _cu170()

        assert roster.find(entity_key) == Control(control)
        assert Control(entity_key) not in roster.controls

    def test_every_momentary_and_latching_control_declares_the_same_floor(self):
        """press-floor-covers-debounce: one assumption, one constant, uniformly."""
        roster = _cu170()

        for control in roster.controls:
            expected = 0 if control.name == "ping" else PRESS_FLOOR_MS
            assert roster.press_floor(control) == PressFloor(frames=1, ms=expected)

    def test_the_staging_controls_carry_their_marks(self):
        """operation-controls-command-path-only: the marks the handler refuses on."""
        roster = _cu170()

        for name in ("store-preset-1", "store-preset-2", "store-preset-4"):
            control = roster.find(name)
            assert control is not None
            assert roster.is_operation(control) is True
            assert ControlMark.DELIBERATE_ONLY not in roster.declaration(control).marks
        # Prodigy CE refuses the press-and-hold chord, so it declares only the
        # other one; a profile that takes both declares both.
        assert roster.find("factory-reset") is None
        for control_roster, name in (
            (roster, "latch-mode-enable"),
            (_cu170("prodigy2"), "factory-reset"),
        ):
            control = control_roster.find(name)
            assert control is not None
            assert control_roster.is_operation(control) is True
            assert ControlMark.DELIBERATE_ONLY in control_roster.declaration(control).marks

    def test_slot_three_declares_no_store_control(self):
        """operation-controls-command-path-only: slot 3 is the fixed snore entry."""
        roster = _cu170()

        assert roster.find("store-preset-3") is None
        assert roster.find("preset-3") is not None

    def test_ping_holds_for_two_minutes_and_prices_no_press(self):
        """ping: cells declared rather than derived, for a control with no bits."""
        roster = _cu170()
        ping = roster.find("ping")
        assert ping is not None

        assert roster.clamp_ttl_ms(ping, 200000) == PING_TTL_MAX_MS
        assert roster.press_floor(ping) == PressFloor(frames=1, ms=0)


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
