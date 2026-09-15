"""Tests for the hold service door.

The ``send_intents`` handler is the input boundary: garbage dies here, with a
validation error, and only a set the roster agrees with reaches the
reconstructor.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adjustable_bed.const import BED_TYPE_OKIN_CST, CONF_BED_TYPE, DOMAIN
from custom_components.adjustable_bed.coordinator import AdjustableBedCoordinator
from custom_components.adjustable_bed.hold_capability import HoldCapable
from custom_components.adjustable_bed.hold_intent import Activate, Hold, HoldOutcome
from custom_components.adjustable_bed.hold_reconstructor import SUBMISSION_SENDER
from custom_components.adjustable_bed.hold_roster import (
    ActionKind,
    ActivateSupport,
    Control,
    ControlDeclaration,
    HoldSupport,
    PressFloor,
    StagedActivate,
)
from custom_components.adjustable_bed.services import (
    SERVICE_GOTO_PRESET,
    SERVICE_SEND_INTENTS,
    SERVICE_TIMED_MOVE,
)

from .conftest import adopting_declarations

HEAD_UP = Control("motor-head-up")
PRESET_1 = Control("preset-1")
PRESET_FLAT = Control("preset-flat")
LIGHT = Control("light-toggle")
PING = Control("ping")
STORE_1 = Control("store-preset-1")


def _declare(
    control: Control,
    actions: set[ActionKind],
    activate_duration_ms: int | None,
    *,
    staged: bool = False,
) -> ControlDeclaration:
    """Return one synthetic declaration."""
    activate: ActivateSupport | StagedActivate | None = None
    if staged:
        activate = StagedActivate()
    elif activate_duration_ms is not None:
        activate = ActivateSupport(duration_ms=activate_duration_ms)
    return ControlDeclaration(
        control=control,
        press_floor=PressFloor(frames=1, ms=223),
        hold=HoldSupport(ttl_max_ms=30000) if ActionKind.HOLD in actions else None,
        activate=activate,
    )


def _declarations() -> tuple[ControlDeclaration, ...]:
    """Return the roster the boundary scenarios are stated against."""
    return (
        _declare(HEAD_UP, {ActionKind.HOLD, ActionKind.ACTIVATE}, 1000),
        _declare(PRESET_1, {ActionKind.HOLD}, None),
        _declare(PRESET_FLAT, {ActionKind.HOLD}, None),
        _declare(PING, {ActionKind.HOLD}, None),
        _declare(LIGHT, {ActionKind.ACTIVATE}, 500),
        _declare(STORE_1, {ActionKind.ACTIVATE}, None, staged=True),
    )


def _sample(
    control: str, action: str, ttl_ms: int | None = None, intent_id: str = "a"
) -> dict[str, Any]:
    """Return one sample as the service call carries it.

    A ttl rides the action variant, so the helper also composes the two shapes
    the wire has no variant for, which the schema tests send.
    """
    tagged: dict[str, Any] = {"kind": action}
    if ttl_ms is not None:
        tagged["ttl_ms"] = ttl_ms
    return {"intent_id": intent_id, "control": control, "action": tagged}


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    declarations: tuple[ControlDeclaration, ...],
) -> AdjustableBedCoordinator:
    """Set the entry up over a synthetic roster and return its coordinator."""
    with adopting_declarations(*declarations):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the device registry id of the entry's bed."""
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    return devices[0].id


def _card_target(*device_ids: str) -> dict[str, Any]:
    """Return a target block as it reaches a service handler.

    The card spells one device as a scalar, Home Assistant's websocket
    ``call_service`` validates the block with ``cv.ENTITY_SERVICE_FIELDS``,
    and ``ServiceRegistry.async_call`` merges the result into the service data
    before the service's own schema runs. Running that validation here is what
    makes the caller's shape the one the handler actually sees.
    """
    spelled: str | list[str] = device_ids[0] if len(device_ids) == 1 else list(device_ids)
    return vol.Schema(cv.ENTITY_SERVICE_FIELDS)({"device_id": spelled})


async def _send(
    hass: HomeAssistant,
    device_id: str,
    *samples: dict[str, Any],
    seq: int = 1,
    sender_id: str = "card-1",
) -> None:
    """Call send_intents with one sample set."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_INTENTS,
        {
            "device_id": device_id,
            "sender_id": sender_id,
            "seq": seq,
            "samples": list(samples),
        },
        blocking=True,
    )


class TestSendIntentsRegistration:
    """The service exists once the integration is set up."""

    async def test_the_service_registers(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """send-intents-service: the multi-intent door a card needs."""
        await _setup(hass, mock_config_entry, _declarations())

        assert hass.services.has_service(DOMAIN, SERVICE_SEND_INTENTS)

    async def test_the_cards_target_block_reaches_the_reconstructor(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """send-intents-service: the shape the card's own call arrives in.

        The card names its bed in the call's target block, so the id crosses
        cv.ENTITY_SERVICE_FIELDS before the service schema sees it and arrives
        as a one-element list. The helper runs that validation rather than
        writing the list out, so the test tracks Home Assistant's own spelling
        of a single-device target.
        """
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(coordinator.hold_reconstructor, "handle_samples") as handle:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SEND_INTENTS,
                {
                    "sender_id": "card-1",
                    "seq": 1,
                    "samples": [_sample("motor-head-up", "hold", 800)],
                },
                blocking=True,
                target=_card_target(device_id),
            )

        handle.assert_called_once()

    async def test_a_scalar_device_in_the_service_data_reaches_the_reconstructor(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """send-intents-service: the same one bed, spelled without a target block."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(coordinator.hold_reconstructor, "handle_samples") as handle:
            await _send(hass, device_id, _sample("motor-head-up", "hold", 800))

        handle.assert_called_once()

    async def test_two_devices_are_refused(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """send-intents-service: the bed is an attribute of the set, so there is one.

        A list would apply to the first device and raise on the second's
        undeclared control, which leaves the caller a half-applied message and
        one error.
        """
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with (
            patch.object(coordinator.hold_reconstructor, "handle_samples") as handle,
            pytest.raises(ServiceValidationError),
        ):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SEND_INTENTS,
                {
                    "sender_id": "card-1",
                    "seq": 1,
                    "samples": [_sample("motor-head-up", "hold", 800)],
                },
                blocking=True,
                target=_card_target(device_id, "0123456789abcdef0123456789abcdef"),
            )

        handle.assert_not_called()

    async def test_a_device_list_of_none_is_refused(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """send-intents-service: a target that matched no bed names no bed."""
        await _setup(hass, mock_config_entry, _declarations())

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SEND_INTENTS,
                {
                    "device_id": [],
                    "sender_id": "card-1",
                    "seq": 1,
                    "samples": [_sample("motor-head-up", "hold", 800)],
                },
                blocking=True,
            )


class TestActionSupportAtTheBoundary:
    """action-support-at-the-boundary: what the handler refuses, and why."""

    @pytest.mark.parametrize(
        "sample",
        [
            _sample("light-toggle", "hold", 800),
            _sample("ping", "activate"),
            _sample("preset-1", "activate"),
            _sample("store-preset-1", "activate"),
            _sample("store-preset-1", "hold", 800),
            _sample("motor-nose-up", "hold", 800),
        ],
    )
    async def test_a_refused_sample_reaches_nothing(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
        sample: dict[str, Any],
    ):
        """action-support-at-the-boundary: a validation error, never a bed outcome."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with (
            patch.object(coordinator.hold_reconstructor, "handle_samples") as handle,
            pytest.raises(ServiceValidationError),
        ):
            await _send(hass, device_id, sample)

        handle.assert_not_called()

    async def test_the_reserved_submission_sender_is_refused(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """A client cannot file into the space direct submissions are keyed by."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with (
            patch.object(coordinator.hold_reconstructor, "handle_samples") as handle,
            pytest.raises(ServiceValidationError),
        ):
            await _send(
                hass,
                device_id,
                _sample("motor-head-up", "hold", 800),
                sender_id=SUBMISSION_SENDER,
            )

        handle.assert_not_called()

    async def test_a_bed_declaring_no_hold_is_refused(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """action-support-at-the-boundary: the roster answers hold-capability."""
        await _setup(hass, mock_config_entry, (_declare(LIGHT, {ActionKind.ACTIVATE}, 500),))
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(ServiceValidationError):
            await _send(hass, device_id, _sample("light-toggle", "activate"))

    async def test_an_idle_bed_answers_as_a_connected_one_does(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """action-support-at-the-boundary: no controller instance is consulted."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)
        coordinator._controller = None
        coordinator._client = None

        await _send(hass, device_id, _sample("motor-head-up", "hold", 800))

        assert set(coordinator.hold_reconstructor.held) == {HEAD_UP}

    async def test_one_earlier_refusal_stops_the_whole_set(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """action-support-at-the-boundary: nothing reaches the reconstructor."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(ServiceValidationError):
            await _send(
                hass,
                device_id,
                _sample("motor-head-up", "hold", 800, intent_id="a"),
                _sample("ping", "activate", intent_id="b"),
            )

        assert not coordinator.hold_reconstructor.holds_anything

    async def test_a_valid_set_reaches_the_reconstructor_in_one_call(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """action-support-at-the-boundary: no ttl read, no duration attached."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(coordinator.hold_reconstructor, "handle_samples") as handle:
            await _send(
                hass,
                device_id,
                _sample("motor-head-up", "hold", 800, intent_id="a"),
                _sample("light-toggle", "activate", intent_id="b"),
            )

        handle.assert_called_once()
        message = handle.call_args.args[0]
        assert message.sender == "card-1"
        assert message.seq == 1
        assert [sample.control for sample in message.samples] == [HEAD_UP, LIGHT]
        assert message.samples[0].action == Hold(800)
        assert message.samples[1].action == Activate()


class TestOperationControlsCommandPathOnly:
    """operation-controls-command-path-only: no sample composes an operation bit."""

    async def test_an_operation_control_is_refused_before_any_bit(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """operation-controls-command-path-only: the mark is what the handler reads."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(ServiceValidationError):
            await _send(hass, device_id, _sample("store-preset-1", "activate"))

        assert not coordinator.hold_reconstructor.holds_anything


class TestClientSampleSets:
    """client-sample-sets, server half: a message is complete truth."""

    async def test_an_empty_set_is_refused_at_the_schema(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """client-sample-sets: no empty set exists."""
        await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(vol.Invalid):
            await _send(hass, device_id)

    @pytest.mark.parametrize(
        "sample",
        [
            _sample("motor-head-up", "hold"),
            _sample("motor-head-up", "activate", 800),
        ],
    )
    async def test_an_action_outside_both_variants_is_refused_at_the_schema(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
        sample: dict[str, Any],
    ):
        """intents-are-parameterized: a hold carries a ttl and an activate carries none."""
        await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(vol.Invalid):
            await _send(hass, device_id, sample)

    async def test_an_omitted_intent_lapses_rather_than_ending(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """client-sample-sets: absence is never a signal."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        await _send(
            hass,
            device_id,
            _sample("motor-head-up", "hold", 8000, intent_id="a"),
            _sample("preset-1", "hold", 8000, intent_id="b"),
            seq=1,
        )
        await _send(
            hass, device_id, _sample("preset-1", "hold", 8000, intent_id="b"), seq=2
        )

        assert set(coordinator.hold_reconstructor.held) == {HEAD_UP, PRESET_1}

    async def test_a_release_rides_the_next_message_at_ttl_zero(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """client-sample-sets: a hold ends by a ttl-0 sample."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        await _send(hass, device_id, _sample("motor-head-up", "hold", 8000), seq=1)
        await _send(hass, device_id, _sample("motor-head-up", "hold", 0), seq=2)

        assert not coordinator.hold_reconstructor.holds_anything


class TestIntentsAreTimeBounded:
    """intents-are-time-bounded: the sample door bounds what it accepts."""

    async def test_every_accepted_sample_becomes_a_bounded_intent(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """intents-are-time-bounded: an Activate's bound is the roster's."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        await _send(
            hass,
            device_id,
            _sample("motor-head-up", "hold", 8000, intent_id="a"),
            _sample("light-toggle", "activate", intent_id="b"),
        )

        now = hass.loop.time()
        held = coordinator.hold_reconstructor.held
        assert held[HEAD_UP].deadline == pytest.approx(now + 8.0, abs=0.5)
        assert held[LIGHT].deadline == pytest.approx(now + 0.5, abs=0.5)


class _HoldCapableController(HoldCapable):
    """A hold-capable stand-in exposing what the two handlers read."""

    supports_memory_presets = True
    memory_slot_count = 4

    def __init__(self) -> None:
        self.motor_control_specs = (
            SimpleNamespace(
                key="head",
                open_fn=AsyncMock(),
                close_fn=AsyncMock(),
                stop_fn=AsyncMock(),
            ),
        )

    def control_declarations(self, inputs: Any) -> tuple:
        """Declare nothing: the roster under test is installed directly."""
        del inputs
        return ()

    def link_up(self) -> None:
        """Take the live link: nothing here owes its box a gesture."""

    def hold(self, held: Any) -> None:
        """Take the pushed held set."""
        del held

    def stop(self, controls: Any) -> None:
        """Take the per-control stop."""
        del controls

    def link_lost(self) -> None:
        """Take the link's death: nothing here runs on a link."""

    def release_wire(self) -> None:
        """End the wire lifecycle: nothing here has one."""


def _completed() -> asyncio.Future[HoldOutcome]:
    """Return an outcome future that has already resolved."""
    future: asyncio.Future[HoldOutcome] = asyncio.get_running_loop().create_future()
    future.set_result(HoldOutcome.COMPLETED)
    return future


class TestDirectSubmissionDoors:
    """timed_move and goto_preset on a hold-capable bed."""

    async def test_timed_move_submits_a_hold_on_the_motors_control(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """reuse-not-duplicate: one direct submission, awaited to its end."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        coordinator._controller = _HoldCapableController()
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_TIMED_MOVE,
                {
                    "device_id": [device_id],
                    "motor": "head",
                    "direction": "up",
                    "duration_ms": 2500,
                },
                blocking=True,
            )

        submit.assert_called_once_with(HEAD_UP, Hold(2500))

    async def test_goto_preset_submits_a_hold_on_the_slots_control(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """presets-hold-only: the automation's door to a preset holds it."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        coordinator._controller = _HoldCapableController()
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": 1, "duration_ms": 4000},
                blocking=True,
            )

        submit.assert_called_once_with(PRESET_1, Hold(4000))

    async def test_goto_preset_without_a_duration_holds_for_the_press_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """presets-hold-only: the shortest press the bed registers."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        coordinator._controller = _HoldCapableController()
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": 1},
                blocking=True,
            )

        submit.assert_called_once_with(PRESET_1, Hold(223))

    async def test_goto_preset_submits_a_hold_on_a_named_presets_control(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """roster-declares-actions: a named preset is a control like any other.

        No controller is set: the roster answers hold-capability, and a named
        preset needs no memory-slot check to reach the reconstructor.
        """
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": "flat", "duration_ms": 4000},
                blocking=True,
            )

        submit.assert_called_once_with(PRESET_FLAT, Hold(4000))

    async def test_a_named_preset_without_a_duration_holds_for_the_press_minimum(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """roster-declares-actions: the shortest press the bed registers."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": "flat"},
                blocking=True,
            )

        submit.assert_called_once_with(PRESET_FLAT, Hold(223))

    async def test_a_slot_number_as_a_string_still_takes_the_slot_path(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """The integer branch is tried first, so "3" is slot 3 and not a name."""
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        coordinator._controller = _HoldCapableController()
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": "1", "duration_ms": 4000},
                blocking=True,
            )

        submit.assert_called_once_with(PRESET_1, Hold(4000))

    @pytest.mark.parametrize("preset", ["flat", "dummy", "zero-g"])
    async def test_a_named_preset_the_bed_does_not_declare_is_a_caller_error(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
        preset: str,
    ):
        """roster-declares-actions: no roster entry, no named preset.

        An empty roster is every bed without the hold primitive, and a named
        preset cannot fall through to preset_memory, which takes a slot number.
        """
        coordinator = await _setup(hass, mock_config_entry, ())
        device_id = _device_id(hass, mock_config_entry)

        with (
            patch.object(coordinator.hold_reconstructor, "submit") as submit,
            pytest.raises(ServiceValidationError),
        ):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": preset},
                blocking=True,
            )

        submit.assert_not_called()

    async def test_any_name_the_roster_declares_reaches_its_control(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """The service holds no list of names: a bed declaring a fixed position
        gets its automation door without an edit here."""
        zero_g = Control("preset-zero-g")
        coordinator = await _setup(
            hass, mock_config_entry, (_declare(zero_g, {ActionKind.HOLD}, None),)
        )
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": "zero-g"},
                blocking=True,
            )

        submit.assert_called_once_with(zero_g, Hold(223))

    async def test_an_empty_preset_is_refused_by_the_schema(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """The empty value is the only name the schema closes; every other one
        the roster answers for."""
        await _setup(hass, mock_config_entry, _declarations())
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": ""},
                blocking=True,
            )

    async def test_a_roster_missing_a_hold_capable_beds_control_is_no_caller_error(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """Declarations disagreeing with the controller class is a bed-module defect."""
        coordinator = await _setup(
            hass, mock_config_entry, (_declare(PRESET_1, {ActionKind.HOLD}, None),)
        )
        coordinator._controller = _HoldCapableController()
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_TIMED_MOVE,
                {
                    "device_id": [device_id],
                    "motor": "head",
                    "direction": "up",
                    "duration_ms": 2500,
                },
                blocking=True,
            )

        assert not isinstance(raised.value, ServiceValidationError)

    async def test_a_bed_that_is_not_hold_capable_keeps_its_baseline_paths(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
        enable_custom_integrations,
    ):
        """A bed whose roster declares nothing keeps today's pulses on both doors."""
        # A bed type whose baseline controller recalls a memory slot, so the
        # preset door reaches its command path rather than a capability refusal.
        hass.config_entries.async_update_entry(
            mock_config_entry,
            data={**mock_config_entry.data, CONF_BED_TYPE: BED_TYPE_OKIN_CST},
        )
        coordinator = await _setup(hass, mock_config_entry, ())
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(coordinator.hold_reconstructor, "submit") as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": 1},
                blocking=True,
            )
            await hass.services.async_call(
                DOMAIN,
                SERVICE_TIMED_MOVE,
                {
                    "device_id": [device_id],
                    "motor": "head",
                    "direction": "up",
                    "duration_ms": 200,
                },
                blocking=True,
            )

        submit.assert_not_called()
        assert mock_bleak_client.write_gatt_char.call_count >= 1

    async def test_a_duration_on_a_bed_that_pulses_a_preset_is_refused(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """presets-hold-only: an ignored parameter reads as honoured, so it is refused."""
        await _setup(hass, mock_config_entry, ())
        device_id = _device_id(hass, mock_config_entry)

        with pytest.raises(ServiceValidationError, match="cannot hold"):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": 1, "duration_ms": 4000},
                blocking=True,
            )

    async def test_a_declared_roster_takes_the_hold_path_off_an_idle_bed(
        self,
        hass: HomeAssistant,
        mock_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """action-support-at-the-boundary: the roster answers, not a controller instance.

        The slot branch and the name branch resolve the same way, so a recall on
        an idle hold-capable bed pays no connect before its intent exists.
        """
        coordinator = await _setup(hass, mock_config_entry, _declarations())
        coordinator._controller = None
        device_id = _device_id(hass, mock_config_entry)

        with patch.object(
            coordinator.hold_reconstructor, "submit", return_value=_completed()
        ) as submit:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GOTO_PRESET,
                {"device_id": [device_id], "preset": 1, "duration_ms": 4000},
                blocking=True,
            )

        submit.assert_called_once_with(PRESET_1, Hold(4000))
