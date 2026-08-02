"""Tests for Leggett & Platt bed controller."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant.const import CONF_ADDRESS, CONF_NAME, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adjustable_bed import SERVICE_TIMED_MOVE
from custom_components.adjustable_bed.beds import base as base_module
from custom_components.adjustable_bed.beds import leggett_okin as leggett_okin_module
from custom_components.adjustable_bed.beds.base import caller_bounded_hold
from custom_components.adjustable_bed.beds.leggett_gen2 import (
    LeggettGen2Commands,
    LeggettGen2Controller,
)
from custom_components.adjustable_bed.beds.leggett_okin import (
    RELEASE_FRAME_COUNT,
    LeggettOkinCommands,
    LeggettOkinController,
    LeggettOkinStatus,
)
from custom_components.adjustable_bed.const import (
    BED_MOTOR_PULSE_DEFAULTS,
    BED_TYPE_LEGGETT_GEN2,
    BED_TYPE_LEGGETT_OKIN,
    BED_TYPE_LEGGETT_PLATT,
    BED_TYPE_OKIMAT,
    CONF_BED_TYPE,
    CONF_DISABLE_ANGLE_SENSING,
    CONF_HAS_MASSAGE,
    CONF_MOTOR_COUNT,
    CONF_PREFERRED_ADAPTER,
    CONF_PROTOCOL_VARIANT,
    DOMAIN,
    LEGGETT_GEN2_WRITE_CHAR_UUID,
    LEGGETT_OKIN_NOTIFY_CHAR_UUID,
    LEGGETT_VARIANT_GEN2,
    LEGGETT_VARIANT_MLRM,
    LEGGETT_VARIANT_OKIN,
    VARIANT_AUTO,
    connection_gated_by_bond,
    requires_pairing,
    requires_pairing_after_service_discovery,
)
from custom_components.adjustable_bed.controller_factory import _SIMPLE_CONTROLLERS
from custom_components.adjustable_bed.coordinator import AdjustableBedCoordinator


@pytest.fixture
def mock_leggett_gen2_config_entry_data() -> dict:
    """Return mock config entry data for Leggett & Platt Gen2 bed."""
    return {
        CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
        CONF_NAME: "Leggett Gen2 Test Bed",
        CONF_BED_TYPE: BED_TYPE_LEGGETT_PLATT,
        CONF_MOTOR_COUNT: 2,
        CONF_HAS_MASSAGE: True,
        CONF_DISABLE_ANGLE_SENSING: True,
        CONF_PREFERRED_ADAPTER: "auto",
    }


@pytest.fixture
def mock_leggett_gen2_config_entry(
    hass: HomeAssistant, mock_leggett_gen2_config_entry_data: dict
) -> MockConfigEntry:
    """Return a mock config entry for Leggett & Platt Gen2 bed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Leggett Gen2 Test Bed",
        data=mock_leggett_gen2_config_entry_data,
        unique_id="AA:BB:CC:DD:EE:FF",
        entry_id="leggett_gen2_test_entry",
    )
    entry.add_to_hass(hass)
    return entry


OKIN_TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"

ZERO_FRAME = bytes.fromhex("040200000000")


def _okin_controller(
    pulse_count: int = 10,
    pulse_delay_ms: int = 100,
    *,
    receipts: Sequence[bool] = (),
) -> LeggettOkinController:
    """Return an Okin controller on a stub coordinator, both write paths mocked.

    Every repeated-frame family on this bed is cadence-paced, so motor holds,
    the memory-store holds and release bursts all land on write_command_paced;
    the single frames - taps, and a latched recall - use write_command.

    The movement stream does cadence arithmetic on the pulse settings and reads
    the coordinator's cancel event to tell preemption from external
    cancellation, so both need concrete values rather than bare MagicMocks.

    A latched command waits for the state frame the box answers its frame with,
    so the fake single-frame write delivers that receipt exactly where the
    notification callback would - synchronously, with no timer in the loop.
    ``receipts`` says which of those writes are answered; writes past its end
    are answered. The channel counts as proven from the first receipt, so a
    test that wants an unproven channel simply never delivers one.
    """
    coordinator = MagicMock()
    coordinator.motor_pulse_count = pulse_count
    coordinator.motor_pulse_delay_ms = pulse_delay_ms
    coordinator.cancel_command.is_set.return_value = False
    controller = LeggettOkinController(coordinator)
    answers = iter(receipts)

    async def _write(*_args: object, **_kwargs: object) -> None:
        if next(answers, True):
            controller.forward_raw_notification(
                LEGGETT_OKIN_NOTIFY_CHAR_UUID, bytes.fromhex("040a00000000")
            )

    controller.write_command = AsyncMock(side_effect=_write)
    controller.write_command_paced = AsyncMock()
    return controller


@pytest.fixture
def mock_leggett_okin_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a mock config entry for a Leggett & Platt Okin bed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Leggett Okin Test Bed",
        data={
            CONF_ADDRESS: OKIN_TEST_ADDRESS,
            CONF_NAME: "Leggett Okin Test Bed",
            CONF_BED_TYPE: BED_TYPE_LEGGETT_OKIN,
            CONF_MOTOR_COUNT: 2,
            CONF_HAS_MASSAGE: True,
            CONF_DISABLE_ANGLE_SENSING: True,
            CONF_PREFERRED_ADAPTER: "auto",
        },
        unique_id=OKIN_TEST_ADDRESS,
        entry_id="leggett_okin_test_entry",
    )
    entry.add_to_hass(hass)
    return entry


class TestLeggettGen2Controller:
    """Test Leggett & Platt Gen2 controller."""

    async def test_control_characteristic_uuid(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
    ):
        """Test controller reports correct characteristic UUID."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        assert coordinator.controller.control_characteristic_uuid == LEGGETT_GEN2_WRITE_CHAR_UUID

    async def test_write_command(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test writing a command to the bed."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        command = LeggettGen2Commands.STOP
        await coordinator.controller.write_command(command)

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, command, response=True
        )


class TestLeggettOkinController:
    """Test Leggett & Platt Okin controller variant."""

    async def test_build_okin_command(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
    ):
        """Test Okin variant command format."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        # Create an Okin controller directly (using the new protocol-based class)
        controller = LeggettOkinController(coordinator)

        # Okin format: [0x04, 0x02, ...int_bytes]
        command = controller._build_command(LeggettOkinCommands.MOTOR_HEAD_UP)

        assert len(command) == 6
        assert command[:2] == bytes([0x04, 0x02])
        # Command 0x1 in big-endian
        assert command[2:] == bytes([0x00, 0x00, 0x00, 0x01])

    async def test_massage_off_is_not_advertised(self):
        """Massage power is a toggle, so no massage-off button should be offered.

        The capability is detected by checking whether the subclass overrides
        massage_off, so overriding it just to raise NotImplementedError created a
        button that could only ever fail (issue #368).
        """
        controller = LeggettOkinController(MagicMock())

        assert controller.supports_massage_off_control is False

    async def test_hold_streams_continuously_with_one_terminating_release(self):
        """A movement is one continuous keycode stream ending in one release burst.

        The box moves only while the keycode keeps arriving (the app's output
        thread writes the held keycode every 100ms until release), so a hold
        must not be a short burst that stops itself: the frame budget covers
        the whole safety cap, no zeros appear mid-stream, and exactly four
        keycode-0 frames follow when the hold ends (MaxZeroCount = 3 in the
        app; there is no distinct stop opcode). Both families are
        cadence-paced, so a write round trip widens no inter-frame gap and the
        release burst sheds its 300ms of pure sleep.
        """
        controller = _okin_controller()

        await controller.move_head_up()

        move_call, release_call = controller.write_command_paced.await_args_list
        assert move_call.args == (bytes.fromhex("040200000001"),)
        # 60s cap at the 100ms cadence.
        assert move_call.kwargs == {
            "repeat_count": 600,
            "cadence_ms": 100,
        }
        assert release_call.args == (ZERO_FRAME,)
        assert release_call.kwargs["repeat_count"] == 4
        assert release_call.kwargs["cadence_ms"] == 100
        assert release_call.kwargs["cancel_event"].is_set() is False
        controller.write_command.assert_not_awaited()

    async def test_an_unknown_motor_raises_before_any_frame(self):
        """An unroutable motor name must fail loudly, not stream release frames.

        Its combined keycode is 0, and 0 is the release frame on this protocol,
        so streaming it would push stop frames for the whole safety cap while
        reporting success. The name comes from this module's own move_* methods,
        so a miss is a programming error and belongs in the caller's face.
        """
        controller = _okin_controller()

        with pytest.raises(ValueError, match="Unknown Leggett Okin motor: 'neck'"):
            await controller._move_motor("neck", leggett_okin_module.MotorDirection.UP)

        controller.write_command_paced.assert_not_awaited()
        controller.write_command.assert_not_awaited()
        assert controller._motor_state == {}

    async def test_a_stop_carries_no_motor(self):
        """A stop asserts no keycode, so it names no motor and passes None.

        The release burst clears the whole key buffer: there is no per-motor
        stop to name, and a name here could only suggest one motor had been
        singled out.
        """
        controller = _okin_controller()

        await controller._move_motor(None, leggett_okin_module.MotorDirection.STOP)

        controller.write_command_paced.assert_awaited_once()
        assert controller.write_command_paced.await_args.args == (ZERO_FRAME,)

    async def test_every_stop_entry_point_passes_no_motor(self):
        """The contract's per-motor stops all resolve to the same motorless stop.

        Each one is reachable from a cover entity and from timed_move's stop
        callable, so a stop that still carried a label would put that label
        back into the log by the back door.
        """
        for method in (
            "move_head_stop",
            "move_back_stop",
            "move_legs_stop",
            "move_feet_stop",
            "move_pillow_stop",
            "move_lumbar_stop",
        ):
            controller = _okin_controller()
            with patch.object(
                controller, "_move_motor", new_callable=AsyncMock
            ) as mock_move:
                await getattr(controller, method)()

            assert mock_move.await_args.args == (
                None,
                leggett_okin_module.MotorDirection.STOP,
            ), method

    async def test_a_movement_without_a_motor_raises(self):
        """Absent is only valid for a stop; a movement has nothing to move.

        Making the motor optional is what lets a stop say "no motor" instead of
        naming one it does not act on, so the same signature has to keep
        rejecting a movement that names none.
        """
        controller = _okin_controller()

        with pytest.raises(ValueError, match="needs a motor"):
            await controller._move_motor(None, leggett_okin_module.MotorDirection.UP)

        controller.write_command.assert_not_awaited()
        controller.write_command_paced.assert_not_awaited()
        assert controller._motor_state == {}

    async def test_an_unknown_motor_is_rejected_on_a_stop_too(self):
        """Validation is total: a supplied motor is checked whatever it is for.

        The old stop path skipped the check because the name was only a log
        label. Now that a stop passes None, a name arriving here at all is a
        caller mistake worth the same refusal as on a movement.
        """
        controller = _okin_controller()

        with pytest.raises(ValueError, match="Unknown Leggett Okin motor: 'neck'"):
            await controller._move_motor("neck", leggett_okin_module.MotorDirection.STOP)

        controller.write_command.assert_not_awaited()
        controller.write_command_paced.assert_not_awaited()

    async def test_every_movement_method_names_a_known_motor(self):
        """The keycode table has to cover every motor the move_* methods use.

        The raise only helps if it can never fire for shipped code, so drive
        each movement entry point and assert none of them is unroutable.
        """
        for method in (
            "move_head_up",
            "move_head_down",
            "move_legs_up",
            "move_legs_down",
            "move_feet_up",
            "move_feet_down",
            "move_back_up",
            "move_back_down",
            "move_pillow_up",
            "move_pillow_down",
            "move_lumbar_up",
            "move_lumbar_down",
        ):
            controller = _okin_controller()
            controller._coordinator.cancel_command.is_set.return_value = True

            await getattr(controller, method)()

            frame = controller.write_command_paced.await_args.args[0]
            assert frame != ZERO_FRAME, f"{method} streamed the release frame"

    async def test_preempted_hold_hands_off_without_zeros(self):
        """A hold preempted by the next command must not send release frames.

        The coordinator sets its cancel event when the next serialized command
        arrives, which makes the stream's write loop return early. Zeros here
        would stop whatever the successor starts; the successor owns the
        release (a stop sends it, a movement streams its own keycode).
        """
        controller = _okin_controller()
        controller._coordinator.cancel_command.is_set.return_value = True

        await controller.move_head_down()

        controller.write_command_paced.assert_awaited_once()
        assert controller.write_command_paced.await_args.args == (bytes.fromhex("040200000002"),)

    async def test_stop_after_a_cancelled_hold_releases_exactly_once(self):
        """Stop during movement ends with one release burst, not two.

        The observed defect: the cancelled movement's cleanup sent four zeros
        and the stop handler sent four more. The cancelled stream now defers
        to the stop command, which sends the protocol's single release.
        """
        controller = _okin_controller()
        controller._coordinator.cancel_command.is_set.return_value = True
        controller.write_command_paced.side_effect = asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await controller.move_head_down()

        controller._coordinator.cancel_command.is_set.return_value = False
        controller.write_command_paced.side_effect = None
        await controller.move_head_stop()

        zero_calls = [
            call
            for call in controller.write_command_paced.await_args_list
            if call.args == (ZERO_FRAME,)
        ]
        assert len(zero_calls) == 1
        assert zero_calls[0].kwargs["repeat_count"] == 4

    async def test_retriggered_hold_streams_the_same_keycode_without_zeros(self):
        """A repeat of the same movement extends the hold instead of glitching.

        The card re-calls the movement service while the button stays held.
        The replacement stream sends the same keycode again with no zeros in
        between, so the box sees one uninterrupted hold.
        """
        controller = _okin_controller()
        controller._coordinator.cancel_command.is_set.return_value = True
        await controller.move_head_down()  # preempted by the re-trigger

        controller._coordinator.cancel_command.is_set.return_value = False
        await controller.move_head_down()  # the re-trigger, runs to its own end

        frames = [call.args[0] for call in controller.write_command_paced.await_args_list]
        assert frames == [
            bytes.fromhex("040200000002"),
            bytes.fromhex("040200000002"),
            ZERO_FRAME,
        ]

    async def test_externally_cancelled_hold_still_releases(self):
        """Cancellation with no successor command must still stop the bed.

        Shutdown or unload cancels the operation without setting the
        coordinator's cancel event, and nothing runs afterwards, so the
        stream sends the release itself before the cancellation propagates.
        """
        controller = _okin_controller()
        controller.write_command_paced.side_effect = [asyncio.CancelledError, None]

        with pytest.raises(asyncio.CancelledError):
            await controller.move_head_up()

        release_call = controller.write_command_paced.await_args_list[-1]
        assert release_call.args == (ZERO_FRAME,)
        assert release_call.kwargs["repeat_count"] == 4

    async def test_safety_cap_expiry_sends_release(self, monkeypatch: pytest.MonkeyPatch):
        """A lost stop must never run a motor forever: the cap ends the hold.

        The stream is wall-clock bounded rather than count bounded (proxy
        round trips stretch a frame count arbitrarily). Zeroing the cap makes
        the timeout fire at the stream's first suspension, and the hold must
        end with the release burst without raising.
        """
        monkeypatch.setattr(leggett_okin_module, "MOVEMENT_HOLD_CAP_S", 0)
        controller = _okin_controller()

        async def _write(command: bytes, **kwargs: object) -> None:
            if command != ZERO_FRAME:
                await asyncio.sleep(60)

        controller.write_command_paced = AsyncMock(side_effect=_write)

        await controller.move_head_up()

        release_call = controller.write_command_paced.await_args_list[-1]
        assert release_call.args == (ZERO_FRAME,)
        assert release_call.kwargs["repeat_count"] == 4

    async def test_caller_bounded_hold_streams_for_the_requested_duration(self):
        """A caller that asks for a duration gets that hold, not the safety cap.

        The pulse count cannot carry this: it means "frames in a burst" for
        every bed that sends one, and a hold deliberately ignores it. So the
        duration arrives as its own value and sizes the stream, here twenty
        frames of the 100ms cadence rather than the cap's six hundred.
        """
        controller = _okin_controller(pulse_count=99)

        with caller_bounded_hold(2000):
            await controller.move_head_up()

        controller.write_command_paced.assert_awaited_once()
        assert controller.write_command_paced.await_args.kwargs == {
            "repeat_count": 20,
            "cadence_ms": 100,
        }

    async def test_caller_bounded_hold_leaves_the_release_to_its_caller(self):
        """The caller's stop is the bounded hold's one release burst.

        A bounded caller always follows the movement with its own stop, so a
        release from the stream as well would be the double-release defect
        again - and the second burst would land after the coordinator had
        already handed the bed to whatever ran next.
        """
        controller = _okin_controller()

        with caller_bounded_hold(2000):
            await controller.move_head_up()
        await controller.move_head_stop()

        frames = [call.args[0] for call in controller.write_command_paced.await_args_list]
        assert frames == [bytes.fromhex("040200000001"), ZERO_FRAME]

    async def test_a_failed_bounded_hold_still_leaves_the_release_to_its_caller(self):
        """A write failure inside a bounded hold must not release either.

        The caller sends its stop from a finally, so it runs on the failure
        path too. Releasing here as cleanup would double it.
        """
        controller = _okin_controller()
        controller.write_command_paced.side_effect = BleakError("write failed")

        with pytest.raises(BleakError, match="write failed"), caller_bounded_hold(2000):
            await controller.move_head_up()

        controller.write_command_paced.assert_awaited_once()

    async def test_the_safety_cap_outranks_a_longer_caller_bound(self):
        """A caller may shorten a hold but never extend it past the cap.

        The cap exists so a lost stop can never run a motor forever, and a
        requested duration is not a stop.
        """
        controller = _okin_controller()

        with caller_bounded_hold(int(leggett_okin_module.MOVEMENT_HOLD_CAP_S * 1000) * 2):
            await controller.move_head_up()

        assert controller.write_command_paced.await_args.kwargs["repeat_count"] == 600

    async def test_an_ordinary_hold_after_a_bounded_one_runs_to_the_cap(self):
        """The bound covers one call and does not linger for the next.

        Cover open/close and the card's motor buttons hold until stopped; a
        duration left behind by an earlier timed move would silently cut them
        short.
        """
        controller = _okin_controller()
        with caller_bounded_hold(2000):
            await controller.move_head_up()
        controller.write_command_paced.reset_mock()

        await controller.move_head_up()

        move_call, release_call = controller.write_command_paced.await_args_list
        assert move_call.kwargs["repeat_count"] == 600
        assert release_call.args == (ZERO_FRAME,)

    @pytest.mark.parametrize(
        ("slot", "keycode"),
        [(1, "00001000"), (2, "00002000"), (3, "00004000"), (4, "00008000")],
    )
    async def test_memory_recall_ladder_and_single_frame(self, slot: int, keycode: str):
        """Recall is one frame of the slot bit, with no terminator and no repeat.

        The ladder previously started at 0x2000 and ran to 0x10000, so every slot
        recalled its neighbour and "memory 4" actually sent the store-arm keycode
        (issue #368). Recall is also autonomous: appending a release frame could
        cancel the move the box just started, and so could a second copy of the
        keycode once the box's hold watchdog has expired.
        """
        controller = _okin_controller()

        await controller.preset_memory(slot)

        controller.write_command.assert_awaited_once()
        call = controller.write_command.await_args
        assert call.args == (bytes.fromhex(f"0402{keycode}"),)
        assert call.kwargs == {}
        controller.write_command_paced.assert_not_awaited()

    async def test_store_keycode_is_never_a_recall(self):
        """0x10000 arms an overwrite and must not appear in the recall ladder."""
        assert LeggettOkinCommands.MEMORY_STORE == 0x10000
        assert LeggettOkinCommands.MEMORY_STORE not in {
            LeggettOkinCommands.PRESET_MEMORY_1,
            LeggettOkinCommands.PRESET_MEMORY_2,
            LeggettOkinCommands.PRESET_MEMORY_3,
            LeggettOkinCommands.PRESET_MEMORY_4,
            LeggettOkinCommands.PRESET_ZERO_G,
            LeggettOkinCommands.PRESET_ANTI_SNORE,
        }

    async def test_program_memory_is_a_two_stage_hold(self):
        """Programming streams the store keycode, then the slot, then one release.

        The app switches from store to slot directly - no release frames
        between the stages - and releases only after the slot hold
        (decompile-derived; hardware verification pending).

        Both holds are paced, so the frame counts are the durations the box
        measures its arm and record windows against: 50 frames on a 100ms
        cadence is the ~5s arm window and 20 is the ~2s record window. Unpaced,
        each frame also cost a write round trip, which stretched both stages
        past the windows and pushed gaps past the ~235ms keep-alive watchdog.
        """
        controller = _okin_controller()

        await controller.program_memory(2)

        arm, slot, release = controller.write_command_paced.await_args_list
        # ~5s of the store keycode...
        assert arm.args == (bytes.fromhex("040200010000"),)
        assert arm.kwargs == {"repeat_count": 50, "cadence_ms": 100}
        # ...then ~2s of the slot keycode with no release between the stages...
        assert slot.args == (bytes.fromhex("040200002000"),)
        assert slot.kwargs == {"repeat_count": 20, "cadence_ms": 100}
        # ...then exactly four zero frames.
        assert release.args == (bytes.fromhex("040200000000"),)
        assert release.kwargs["repeat_count"] == 4
        # 50 frames * 100ms + 20 * 100ms is the whole sequence's send schedule:
        # the durations no longer depend on link latency.
        assert arm.kwargs["repeat_count"] * arm.kwargs["cadence_ms"] == 5000
        assert slot.kwargs["repeat_count"] * slot.kwargs["cadence_ms"] == 2000

    async def test_stop_all_propagates_write_failures(self):
        """An explicit stop must not report success when it never reached the bed.

        The release burst is this protocol's only stop, and the shared helper
        logs and swallows BleakError for cleanup callers. stop_all opts out, or
        async_stop_command would log "Stop command sent" while the bed kept
        moving.
        """
        controller = _okin_controller()
        controller.write_command_paced.side_effect = BleakError("write failed")

        with pytest.raises(BleakError):
            await controller.stop_all()

    async def test_cleanup_release_failures_do_not_mask_the_real_error(self):
        """A failed release burst during cleanup stays logged, not raised.

        The two writes raise distinct errors so the assertion actually proves
        which one propagated: with a shared message it would pass even if the
        cleanup failure replaced the movement failure.
        """
        controller = _okin_controller()
        controller.write_command_paced.side_effect = [
            BleakError("movement failed"),
            BleakError("release failed"),
        ]

        with pytest.raises(BleakError, match="movement failed"):
            await controller.move_head_up()

        assert controller.write_command_paced.await_count == 2

    async def test_preset_flat_is_one_confirmed_frame(self):
        """Flat is latched by the box, so it is one frame and not a hold or burst.

        LP Control ships a press-and-release mode whose entire flat command is
        one frame, and never tells an Okin box which mode it is in, so the box
        completes the flatten itself. It takes no terminator either: zero frames
        could cancel the move the box just started, exactly as for a memory
        recall - and so could a repeat of the keycode, which the box reads as a
        second press once its hold watchdog has expired.
        """
        controller = _okin_controller()

        await controller.preset_flat()

        controller.write_command.assert_awaited_once()
        call = controller.write_command.await_args
        assert call.args == (bytes.fromhex("040208000000"),)
        assert call.kwargs == {}

    async def test_preset_flat_matches_the_memory_recall_path(self):
        """Flat and a memory recall are delivered identically.

        A parallel copy of the delivery rules would let the two drift apart the
        next time either is retuned, and a user has no reason to expect flat to
        behave differently from a memory slot.
        """
        controller = _okin_controller(pulse_count=3, pulse_delay_ms=250)

        await controller.preset_flat()
        await controller.preset_memory(1)

        flat_call, recall_call = controller.write_command.await_args_list
        assert flat_call.kwargs == recall_call.kwargs == {}
        assert controller.write_command.await_count == 2

    async def test_a_receipt_ends_the_recall_after_one_frame(self):
        """The box's state frame is the receipt, and it is the whole redundancy.

        Every frame after the one the box answered is pure downside: the box has
        already latched, and once its ~235ms hold watchdog expires the next copy
        of the keycode is a second press, which stops a preset mid-move.
        """
        controller = _okin_controller(pulse_count=10)

        await controller.preset_memory(2)

        controller.write_command.assert_awaited_once()

    async def test_an_unanswered_frame_is_retried_up_to_the_attempt_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Silence is the one safe moment to resend: nothing landed, so nothing cancels.

        The first frame here is answered, which proves the box answers at all;
        the second command's frame is not, so it is worth resending.
        """
        monkeypatch.setattr(leggett_okin_module, "RECALL_RECEIPT_TIMEOUT_S", 0)
        controller = _okin_controller(pulse_count=3, receipts=(True, False, True))

        await controller.preset_flat()
        controller.write_command.assert_awaited_once()

        await controller.preset_memory(1)

        assert controller.write_command.await_count == 3
        resent = controller.write_command.await_args_list[1:]
        assert [call.args for call in resent] == [(bytes.fromhex("040200001000"),)] * 2

    async def test_an_unanswered_recall_fails_once_the_attempts_run_out(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A recall that was never acknowledged is a failed command, not a silent one.

        Every other path in this controller that ends in an unconfirmed write -
        stop_all, the memory-store stage releases - reports the failure rather
        than logging it, because the caller's next decision depends on whether
        the bed moved.
        """
        monkeypatch.setattr(leggett_okin_module, "RECALL_RECEIPT_TIMEOUT_S", 0)
        controller = _okin_controller(pulse_count=3, receipts=(True,) + (False,) * 9)

        await controller.preset_flat()
        controller.write_command.reset_mock()

        with pytest.raises(BleakError, match="did not acknowledge"):
            await controller.preset_memory(1)

        assert controller.write_command.await_count == 3

    async def test_a_box_that_never_answers_is_sent_one_frame_and_trusted(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A box with no feedback characteristic must not be retried into a cancel.

        Its silence carries no information at all, so resending would be a blind
        second press on a bed that may well be mid-preset. One frame is what the
        physical remote sends, and a write with response already reached the
        box's GATT server.
        """
        monkeypatch.setattr(leggett_okin_module, "RECALL_RECEIPT_TIMEOUT_S", 0)
        controller = _okin_controller(pulse_count=10, receipts=(False,) * 10)

        await controller.preset_flat()

        controller.write_command.assert_awaited_once()

    @pytest.mark.parametrize(
        ("pulse_count", "expected_attempts"),
        [(10, 10), (25, 25), (3, 3), (1, 1), (0, 1), (-5, 1)],
    )
    async def test_the_pulse_count_bounds_the_attempts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pulse_count: int,
        expected_attempts: int,
    ):
        """motor_pulse_count now buys retries, not frames of one burst.

        The setup flows accept any integer, and a count of 0 would drop the
        command entirely, so it floors at 1. The delay is deliberately not read
        here: retries are paced by the receipt window, which belongs to the link
        rather than to a user preference.
        """
        monkeypatch.setattr(leggett_okin_module, "RECALL_RECEIPT_TIMEOUT_S", 0)
        controller = _okin_controller(
            pulse_count, pulse_delay_ms=250, receipts=(True,) + (False,) * 30
        )

        await controller.preset_flat()
        controller.write_command.reset_mock()

        with pytest.raises(BleakError):
            await controller.preset_memory(1)

        assert controller.write_command.await_count == expected_attempts

    async def test_this_bed_types_defaults_still_give_the_documented_budget(self):
        """A device nobody has tuned gets the documented ten attempts.

        Reading the shipped per-bed-type defaults rather than restating them is
        what makes a drifted default fail here instead of silently retuning
        every recall in the field.
        """
        pulse_count, pulse_delay_ms = BED_MOTOR_PULSE_DEFAULTS[BED_TYPE_LEGGETT_OKIN]
        assert (pulse_count, pulse_delay_ms) == (10, 100)

    async def test_no_flat_hold_duration_remains(self):
        """The fixed hold duration is gone, not merely bypassed.

        It existed only to stream flat as a held key, and leaving it behind
        would invite a future change to reinstate the hold.
        """
        assert not hasattr(leggett_okin_module, "FLAT_HOLD_S")

    async def test_flat_change_is_confined_to_the_okin_controller(self):
        """No other bed type can inherit this flat behaviour.

        The controller is subclassed by nothing and reachable from exactly one
        bed type, so a latched flat cannot leak into another protocol.
        """
        assert LeggettOkinController.__subclasses__() == []
        assert [
            bed_type
            for bed_type, spec in _SIMPLE_CONTROLLERS.items()
            if spec.class_name == "LeggettOkinController"
        ] == [BED_TYPE_LEGGETT_OKIN]

    async def test_program_memory_aborts_when_the_arm_hold_fails(self):
        """A failed arm hold must not be followed by the slot keycode.

        Streaming the slot keycode without a completed arm stage would be a
        plain recall of that slot, so the sequence stops and the failure
        surfaces instead of reporting a successful save.
        """
        controller = _okin_controller()
        # Stage 1 (arm hold) fails; the release burst in cleanup succeeds.
        controller.write_command_paced.side_effect = [BleakError("arm failed"), None]

        with pytest.raises(BleakError, match="arm failed"):
            await controller.program_memory(2)

        # The slot keycode must never have been sent.
        sent = [call.args[0] for call in controller.write_command_paced.await_args_list]
        assert bytes.fromhex("040200002000") not in sent

    async def test_light_and_massage_taps_end_with_a_release_burst(self):
        """Lights and massage are held keycodes, so a tap must release the key.

        Sending the frame alone can leave the key asserted, and the receiver may
        then not recognise the next press of the same control. A tap is a single
        frame, so it has no cadence to pace; only its release burst does.
        """
        controller = _okin_controller()

        await controller.lights_toggle()

        controller.write_command.assert_awaited_once()
        assert controller.write_command.await_args.args == (bytes.fromhex("040200020000"),)
        release = controller.write_command_paced.await_args
        assert release.args == (bytes.fromhex("040200000000"),)
        assert release.kwargs["repeat_count"] == 4
        assert release.kwargs["cadence_ms"] == 100

        controller.write_command.reset_mock()
        controller.write_command_paced.reset_mock()
        await controller.massage_toggle()

        assert controller.write_command.await_args.args == (bytes.fromhex("040200000100"),)
        assert controller.write_command_paced.await_args.args == (bytes.fromhex("040200000000"),)

    async def test_program_memory_surfaces_a_failed_final_release(self):
        """On the success path the closing release is the operation, not cleanup.

        If it fails the slot keycode may still be asserted, so the save must not
        report success.
        """
        controller = _okin_controller()
        # arm hold and slot hold succeed; the final release fails.
        controller.write_command_paced.side_effect = [
            None,
            None,
            BleakError("final release failed"),
        ]

        with pytest.raises(BleakError, match="final release failed"):
            await controller.program_memory(2)

    @pytest.mark.parametrize(
        ("action", "primary_frame", "primary_is_paced"),
        [
            ("move_head_up", "040200000001", True),
            ("lights_toggle", "040200020000", False),
            ("massage_toggle", "040200000100", False),
        ],
    )
    async def test_release_failure_surfaces_after_a_successful_command(
        self, action: str, primary_frame: str, primary_is_paced: bool
    ):
        """A lost release after a successful command must not report success.

        The release burst is this protocol's stop, so losing it can leave the
        bed moving or a keycode asserted. Every command family that ends in one
        has to surface that, not just log it. The primary frame is paced for
        the holds and plain for the single-frame taps; the release is always
        paced, so it is the failing call in both shapes.
        has to surface that, not just log it. Flat and the memory recalls are
        deliberately absent: they end in silence, not a release.
        """
        controller = _okin_controller()
        controller.write_command_paced.side_effect = (
            [None, BleakError("release failed")]
            if primary_is_paced
            else [BleakError("release failed")]
        )

        with pytest.raises(BleakError, match="release failed"):
            await getattr(controller, action)()

        primary = controller.write_command_paced if primary_is_paced else controller.write_command
        assert primary.await_args_list[0].args == (bytes.fromhex(primary_frame),)

    @pytest.mark.parametrize(
        "action",
        ["move_head_up", "move_head_stop", "stop_all", "lights_toggle"],
    )
    async def test_exactly_one_release_burst_ends_each_lifecycle(self, action: str):
        """Pacing must not have added or dropped a release anywhere.

        The zero frames are this protocol's stop, so a second burst can cut
        short whatever ran next and a missing one can leave the bed moving.
        Every path that ends a held keycode emits exactly one four-frame burst.
        A latched recall is deliberately absent: it ends in silence, not a
        release.
        """
        controller = _okin_controller()

        await getattr(controller, action)()

        bursts = [
            call
            for call in controller.write_command_paced.await_args_list
            if call.args == (bytes.fromhex("040200000000"),)
        ]
        assert len(bursts) == 1
        assert bursts[0].kwargs["repeat_count"] == 4

    async def test_recall_never_sends_a_release(self):
        """Recall is the one family with no terminator, paced or not.

        The box completes the move on its own, so zeros after the burst could
        cancel the motion the recall just started.
        """
        controller = _okin_controller()

        await controller.preset_memory(1)
        await controller.preset_zero_g()
        await controller.preset_anti_snore()

        sent = [call.args[0] for call in controller.write_command_paced.await_args_list]
        assert bytes.fromhex("040200000000") not in sent

    async def test_massage_timer_step_is_not_exposed(self):
        """0x200 is a constant the app never builds or writes, so it is not a command."""
        controller = LeggettOkinController(MagicMock())

        assert not hasattr(LeggettOkinCommands, "MASSAGE_TIMER_STEP")
        assert not hasattr(controller, "massage_timer_step")

    # key, translation_key, up keycode, down keycode. The physical remote labels
    # its four sections HEAD, PILLOW, LUMBAR, FOOT, in this order.
    _MOTORS = (
        ("head", "head", 0x1, 0x2),
        ("pillow", "pillow", 0x10, 0x20),
        ("lumbar", "lumbar", 0x40, 0x80),
        ("feet", "feet", 0x4, 0x8),
    )

    async def test_motor_specs_are_the_four_motors_on_the_remote(self):
        """Four motors, four covers, named as the remote names them.

        The base layout is Linak-shaped, and move_back_*/move_legs_* are pure
        aliases of move_head_*/move_feet_* here, so it produced six covers for
        four motors: back duplicating head, legs duplicating feet, and the
        pillow motor labelled "tilt".
        """
        controller = LeggettOkinController(MagicMock())

        specs = controller.motor_control_specs
        assert [(spec.key, spec.translation_key) for spec in specs] == [
            (key, translation_key) for key, translation_key, _, _ in self._MOTORS
        ]
        assert not {"back", "legs", "tilt"} & {spec.key for spec in specs}

    @pytest.mark.parametrize(("key", "translation_key", "up_code", "down_code"), _MOTORS)
    async def test_motor_spec_drives_its_documented_keycode(
        self, key: str, translation_key: str, up_code: int, down_code: int
    ):
        """Each cover's up/down reaches the keycode the app's slider tag carries."""
        controller = _okin_controller()
        spec = next(spec for spec in controller.motor_control_specs if spec.key == key)

        await spec.open_fn(controller)
        assert controller.write_command_paced.await_args_list[0].args == (
            bytes.fromhex(f"0402{up_code:08x}"),
        )

        controller.write_command_paced.reset_mock()
        await spec.close_fn(controller)
        assert controller.write_command_paced.await_args_list[0].args == (
            bytes.fromhex(f"0402{down_code:08x}"),
        )

    @pytest.mark.parametrize("motor_count", [2, 3, 4, 6])
    async def test_motor_specs_ignore_the_configured_motor_count(self, motor_count: int):
        """Every frame on this protocol drives all four keycodes.

        motor_count is user-entered and cannot tell us otherwise, so gating on
        it only ever hid a motor the bed has.
        """
        coordinator = MagicMock()
        coordinator.motor_count = motor_count
        controller = LeggettOkinController(coordinator)

        assert [spec.key for spec in controller.motor_control_specs] == [
            "head",
            "pillow",
            "lumbar",
            "feet",
        ]

    async def test_stale_motor_keys_cover_the_removed_and_renamed_entities(self):
        """The three orphaned covers are removed rather than left unavailable."""
        controller = LeggettOkinController(MagicMock())

        assert controller.stale_motor_entity_keys == frozenset({"back", "legs", "tilt"})

    async def test_tilt_surface_is_gone(self):
        """The 0x10/0x20 motor is the pillow, so it is not offered as a tilt."""
        controller = LeggettOkinController(MagicMock())

        assert controller.has_pillow_support is True
        assert controller.has_tilt_support is False


class TestLeggettOkinTimedMove:
    """The timed_move service against a hold that streams until it is stopped."""

    async def test_timed_move_streams_for_its_duration_and_releases_once(
        self,
        hass: HomeAssistant,
        mock_leggett_okin_config_entry: MockConfigEntry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
        enable_custom_integrations,
    ):
        """A bounded move must end at its duration with a single release burst.

        The service asks for a duration; the hold stream would otherwise run to
        the safety cap because it sizes its own budget and never reads the
        pulse count the service assigns. The service always sends its own stop,
        so that stop is the lifecycle's one release burst and the stream must
        not add a second.
        """
        entry = mock_leggett_okin_config_entry
        with patch(
            "custom_components.adjustable_bed.coordinator."
            "AdjustableBedCoordinator.async_read_initial_positions",
            new=AsyncMock(),
        ):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        from homeassistant.helpers import device_registry as dr

        device_registry = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        assert len(devices) == 1
        mock_bleak_client.write_gatt_char.reset_mock()

        await hass.services.async_call(
            DOMAIN,
            SERVICE_TIMED_MOVE,
            {
                "device_id": [devices[0].id],
                # This bed declares four motors, and "back" is not one of them:
                # it was an alias of head that the motor entities dropped.
                "motor": "head",
                "direction": "up",
                # One cadence interval, so the frame budget is a single frame
                # and no wall clock decides the outcome.
                "duration_ms": 100,
            },
            blocking=True,
        )

        payloads = [call.args[1] for call in mock_bleak_client.write_gatt_char.call_args_list]
        assert payloads == [bytes.fromhex("040200000001"), *[ZERO_FRAME] * RELEASE_FRAME_COUNT]


def _paced_link_controller() -> tuple[LeggettOkinController, MagicMock]:
    """Return a controller whose real base write loop runs against a fake client.

    The cadence-pacing tests exercise BedController._write_gatt_with_retry
    itself, so nothing on the write path is mocked except the BLE client. The
    cancel event is a real asyncio.Event because the loop calls is_set() on it.
    """
    coordinator = MagicMock()
    coordinator.address = "AA:BB:CC:DD:EE:FF"
    coordinator.cancel_command = asyncio.Event()
    client = MagicMock()
    client.is_connected = True
    client.services = []
    client.write_gatt_char = AsyncMock()
    coordinator.client = client
    return LeggettOkinController(coordinator), client


class TestLeggettOkinCadencePacing:
    """Cadence pacing of the movement stream's underlying write loop.

    The bed's control box halts motion when the inter-frame gap exceeds
    roughly 235ms - measured, at an achieved gap of p50 232 / max 237ms the
    bed still moved - and proxied write-with-response round trips reach that on
    their own, so the stream schedules frame k at stream start + k * cadence
    instead of sleeping the full delay after every response.
    """

    @staticmethod
    def _install_fake_clock(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[dict[str, float], list[float]]:
        """Drive the write loop from a fake clock.

        The loop reads the running loop's time and pauses via asyncio.sleep;
        both are redirected to the same fake clock so simulated round trips
        and the recorded sleep durations are exact, with no wall time spent.
        """
        clock = {"now": 0.0}
        sleeps: list[float] = []
        monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock["now"])

        async def _sleep(delay: float) -> None:
            sleeps.append(delay)
            clock["now"] += delay

        monkeypatch.setattr(base_module.asyncio, "sleep", _sleep)
        return clock, sleeps

    async def test_stream_sleeps_the_remainder_of_each_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Pacing sleeps cadence minus round trip, not cadence on top of it.

        With 40ms round trips on a 100ms cadence the old post-response sleep
        made every real gap 140ms; the paced loop must sleep only the 60ms
        remainder so frames stay on the 100ms grid.
        """
        controller, client = _paced_link_controller()
        clock, sleeps = self._install_fake_clock(monkeypatch)

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            clock["now"] += 0.040

        client.write_gatt_char = AsyncMock(side_effect=_write)

        await controller.write_command_paced(
            bytes.fromhex("040200000001"), repeat_count=4, cadence_ms=100
        )

        assert client.write_gatt_char.await_count == 4
        assert sleeps == pytest.approx([0.06, 0.06, 0.06])

    async def test_overrun_sends_immediately_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """A round trip past the tick must not add a sleep, and must be visible once.

        The next frame goes out immediately so the watchdog gap stops growing.
        Every frame of a stream this slow breaches, and a line each would bury
        the fact in its own volume, so the stream warns on the first breach and
        counts the rest.
        """
        controller, client = _paced_link_controller()
        clock, sleeps = self._install_fake_clock(monkeypatch)

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            clock["now"] += 0.250

        client.write_gatt_char = AsyncMock(side_effect=_write)

        with caplog.at_level(logging.DEBUG, logger="custom_components.adjustable_bed.beds.base"):
            await controller.write_command_paced(
                bytes.fromhex("040200000001"), repeat_count=4, cadence_ms=100
            )

        assert client.write_gatt_char.await_count == 4
        assert sleeps == []
        assert caplog.text.count("exceeded the 235 ms keep-alive window") == 1
        assert "Frame gap of 250 ms" in caplog.text

    async def test_gaps_are_measured_between_packets_not_against_the_schedule(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The only figure the box reacts to is the gap since the last frame it got.

        Drift from the schedule the stream started on accumulates whether or
        not the link recovers: a real capture read 82, 85, 292, 296, 336, 608,
        632, 714, 818ms of "overrun" while the gaps between packets were 104,
        306, 103, 140, 373, 205, 52, 253, 103ms - a runaway against two
        breaches in nine gaps. The watchdog retriggers on the last frame
        received, so the second list is the one that describes the bed.
        """
        controller, client = _paced_link_controller()
        clock, _ = self._install_fake_clock(monkeypatch)
        # Round trips chosen so the stream never catches back up to its grid.
        # By the last frame it is 900ms behind the schedule it started on,
        # while no gap between packets ever exceeded 360ms.
        round_trips = iter([0.04, 0.30, 0.30, 0.30, 0.30])

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            clock["now"] += next(round_trips)

        client.write_gatt_char = AsyncMock(side_effect=_write)

        await controller.write_command_paced(
            bytes.fromhex("040200000001"), repeat_count=5, cadence_ms=100
        )

        assert clock["now"] == pytest.approx(1.30)  # 900ms past the last tick
        summary = controller._coordinator.record_stream_cadence.call_args.kwargs["summary"]
        assert summary["gap_count"] == 4
        assert summary["max_gap_ms"] == pytest.approx(360, abs=1)
        assert summary["median_gap_ms"] == pytest.approx(300, abs=1)
        assert summary["breaches"] == 4

    async def test_a_clean_stream_files_its_cadence_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """A stream that held cadence is still worth a histogram, just not a warning.

        The support bundle is where a cadence question gets answered, and "the
        gaps were fine" only means something if the clean streams are in there
        too.
        """
        controller, client = _paced_link_controller()
        clock, _ = self._install_fake_clock(monkeypatch)

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            clock["now"] += 0.040

        client.write_gatt_char = AsyncMock(side_effect=_write)

        with caplog.at_level(logging.DEBUG, logger="custom_components.adjustable_bed.beds.base"):
            await controller.write_command_paced(
                bytes.fromhex("040200000001"), repeat_count=4, cadence_ms=100
            )

        assert "keep-alive window" not in caplog.text
        call = controller._coordinator.record_stream_cadence.call_args.kwargs
        assert call["target_cadence_ms"] == 100
        assert call["characteristic_uuid"] == controller.control_characteristic_uuid
        summary = call["summary"]
        assert summary["breaches"] == 0
        assert summary["breach_threshold_ms"] == 235
        assert summary["max_gap_ms"] == pytest.approx(100, abs=1)
        assert sum(summary["histogram_ms"].values()) == summary["gap_count"] == 3

    async def test_a_stream_cut_short_still_files_its_cadence(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A stream that ended early is exactly the one whose gaps are worth reading.

        Preemption, the safety cap and a write failure all leave the loop
        without reaching its frame budget, and the cadence up to that point is
        still the record of what the link did.
        """
        controller, client = _paced_link_controller()
        clock, _ = self._install_fake_clock(monkeypatch)
        cancel = controller._coordinator.cancel_command
        writes = {"count": 0}

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            clock["now"] += 0.040
            writes["count"] += 1
            if writes["count"] == 3:
                cancel.set()

        client.write_gatt_char = AsyncMock(side_effect=_write)

        await controller.write_command_paced(
            bytes.fromhex("040200000001"), repeat_count=50, cadence_ms=100
        )

        summary = controller._coordinator.record_stream_cadence.call_args.kwargs["summary"]
        assert summary["gap_count"] == 2

    async def test_unpaced_writes_file_no_cadence(self, monkeypatch: pytest.MonkeyPatch):
        """Every other bed shares this loop and none of them paces, so none reports.

        Their delay is a post-response sleep rather than a target, so a gap
        histogram would describe a cadence they never asked for.
        """
        controller, client = _paced_link_controller()
        clock, _ = self._install_fake_clock(monkeypatch)

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            clock["now"] += 0.040

        client.write_gatt_char = AsyncMock(side_effect=_write)

        await controller.write_command(
            bytes.fromhex("040200000001"), repeat_count=3, repeat_delay_ms=100
        )

        controller._coordinator.record_stream_cadence.assert_not_called()

    async def test_unpaced_writes_keep_the_fixed_sleep(self, monkeypatch: pytest.MonkeyPatch):
        """write_command's loop is untouched: the full delay follows every response.

        Every other bed type shares this loop, so its timing must not change
        however far the clock has advanced during the write.
        """
        controller, client = _paced_link_controller()
        clock, sleeps = self._install_fake_clock(monkeypatch)

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            clock["now"] += 0.040

        client.write_gatt_char = AsyncMock(side_effect=_write)

        await controller.write_command(
            bytes.fromhex("040200000001"), repeat_count=3, repeat_delay_ms=100
        )

        assert sleeps == pytest.approx([0.1, 0.1])

    async def test_paced_stream_stops_on_the_cancel_event_between_frames(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Preemption semantics survive pacing: the loop exits between frames.

        A stop request has to be able to cut a movement short, so the paced
        loop must keep checking the coordinator's cancel event before every
        frame rather than running its whole frame budget.
        """
        controller, client = _paced_link_controller()
        self._install_fake_clock(monkeypatch)
        cancel = controller._coordinator.cancel_command
        writes = {"count": 0}

        async def _write(char_uuid: str, command: bytes, response: bool = True) -> None:
            writes["count"] += 1
            if writes["count"] == 2:
                cancel.set()

        client.write_gatt_char = AsyncMock(side_effect=_write)

        await controller.write_command_paced(
            bytes.fromhex("040200000001"), repeat_count=10, cadence_ms=100
        )

        assert writes["count"] == 2


# Written out rather than taken from the source constant, so a change to the
# parser's idea of the light bit fails here instead of agreeing with itself.
_LIGHT_BIT = 0x20000


def _okin_state_frame(mask: int) -> bytes:
    """Build a state frame in the 20-byte layout captured from a CU170 box."""
    body = mask.to_bytes(4, "big")
    return bytes([0x09, 0x0B]) + body + body + b"\xff" + bytes(9)


def _okin_connected_coordinator(read_value: bytes | None = None) -> MagicMock:
    """Return a mock coordinator whose client is connected."""
    coordinator = MagicMock()
    client = MagicMock()
    client.is_connected = True
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=bytearray(read_value or b""))
    coordinator.client = client
    return coordinator


class TestLeggettOkinStateFeedback:
    """Test Leggett & Platt Okin under-bed light state reading."""

    def test_state_frame_carries_the_light_bit(self):
        """Bytes 2-5 big-endian are the state mask; 0x20000 is the light, per capture."""
        on = LeggettOkinStatus.from_frame(_okin_state_frame(_LIGHT_BIT))
        off = LeggettOkinStatus.from_frame(_okin_state_frame(0))

        assert on is not None
        assert on.mask == 0x20000
        assert on.light is True
        assert off is not None
        assert off.light is False

    def test_bits_this_code_does_not_identify_are_kept_and_ignored(self):
        """Unidentified bits must survive into the mask for diagnostics.

        0x8000 is the app's sleep-timer indicator, and as a keycode it is the
        memory-4 recall - which is why mask bits are not read as keycodes.
        """
        status = LeggettOkinStatus.from_frame(_okin_state_frame(0x8000))

        assert status is not None
        assert status.mask == 0x8000
        assert status.light is False
        assert "0x00008000" in repr(status)

    @pytest.mark.parametrize("frame", [b"", b"\x09\x0b\x00\x02\x00"])
    def test_frames_too_short_to_carry_a_mask_are_rejected(self, frame: bytes):
        """A frame shorter than six bytes has no complete mask, so nothing is parsed."""
        assert LeggettOkinStatus.from_frame(frame) is None

    async def test_notification_publishes_the_light_state(self):
        """Every state change is pushed by the box, so notifications drive the state."""
        coordinator = MagicMock()
        controller = LeggettOkinController(coordinator)

        controller._handle_status_notification(
            None, bytearray(_okin_state_frame(_LIGHT_BIT))
        )

        coordinator.handle_controller_state_updates.assert_called_once_with(
            {"under_bed_lights_on": True}
        )
        assert controller.get_light_state() == {"is_on": True}

        # An unchanged light bit must not republish; a change must.
        coordinator.handle_controller_state_updates.reset_mock()
        controller._handle_status_notification(
            None, bytearray(_okin_state_frame(_LIGHT_BIT | 0x8000))
        )
        coordinator.handle_controller_state_updates.assert_not_called()

        controller._handle_status_notification(None, bytearray(_okin_state_frame(0)))
        coordinator.handle_controller_state_updates.assert_called_once_with(
            {"under_bed_lights_on": False}
        )

    async def test_a_short_frame_leaves_the_last_known_state_alone(self):
        """Unusable frames are logged and dropped, not treated as an all-clear."""
        coordinator = MagicMock()
        controller = LeggettOkinController(coordinator)
        controller._handle_status_notification(
            None, bytearray(_okin_state_frame(_LIGHT_BIT))
        )
        coordinator.handle_controller_state_updates.reset_mock()

        controller._handle_status_notification(None, bytearray(b"\x09\x0b\x00"))

        coordinator.handle_controller_state_updates.assert_not_called()
        assert controller.get_light_state() == {"is_on": True}

    async def test_state_is_unknown_before_the_first_frame(self):
        """No frame yet means no claim about the light."""
        controller = LeggettOkinController(MagicMock())

        assert controller.get_light_state() == {}
        assert controller.last_feedback_frame is None
        assert controller.feedback_state_mask is None

    async def test_start_notify_subscribes_to_the_state_characteristic(self):
        """The state characteristic is both the notify source and the read source."""
        coordinator = _okin_connected_coordinator()
        controller = LeggettOkinController(coordinator)

        await controller.start_notify(None)

        assert coordinator.client.start_notify.await_args.args[0] == LEGGETT_OKIN_NOTIFY_CHAR_UUID
        assert controller._notify_started is True

        # A second call must not resubscribe.
        coordinator.client.start_notify.reset_mock()
        await controller.start_notify(None)
        coordinator.client.start_notify.assert_not_called()

    async def test_a_box_that_refuses_the_subscription_still_controls(self):
        """State is a convenience: a box without the characteristic must still work."""
        coordinator = _okin_connected_coordinator()
        coordinator.client.start_notify = AsyncMock(side_effect=BleakError("no such char"))
        controller = LeggettOkinController(coordinator)

        await controller.start_notify(None)

        assert controller._notify_started is False
        assert controller.get_light_state() == {}

    async def test_read_light_state_hydrates_from_the_state_characteristic(self):
        """The initial read resolves the light state before any notification arrives."""
        frame = _okin_state_frame(_LIGHT_BIT)
        coordinator = _okin_connected_coordinator(frame)
        controller = LeggettOkinController(coordinator)

        state = await controller.read_light_state()

        coordinator.client.read_gatt_char.assert_awaited_once_with(LEGGETT_OKIN_NOTIFY_CHAR_UUID)
        assert state == {"is_on": True}
        assert controller.last_feedback_frame == frame.hex()
        assert controller.feedback_state_mask == 0x20000

    async def test_read_light_state_tolerates_an_empty_read(self):
        """An empty or truncated read must leave the state unknown, not crash."""
        controller = LeggettOkinController(_okin_connected_coordinator(b""))

        assert await controller.read_light_state() == {}

    async def test_lights_on_and_off_are_state_aware(self):
        """The toggle keycode is only sent when the reported state differs."""
        controller = _okin_controller()
        controller._handle_status_notification(
            None, bytearray(_okin_state_frame(_LIGHT_BIT))
        )

        await controller.lights_on()
        controller.write_command.assert_not_called()

        await controller.lights_off()
        # The tap is one frame; only its release burst is paced.
        assert controller.write_command.await_args.args == (bytes.fromhex("040200020000"),)
        assert controller.write_command_paced.await_args.args == (
            bytes.fromhex("040200000000"),
        )

        # With the light reported off, the roles swap.
        controller.write_command.reset_mock()
        controller._handle_status_notification(None, bytearray(_okin_state_frame(0)))
        await controller.lights_off()
        controller.write_command.assert_not_called()
        await controller.lights_on()
        assert controller.write_command.await_args_list[0].args == (
            bytes.fromhex("040200020000"),
        )

    async def test_an_empty_cache_is_resolved_by_a_read_before_deciding(self):
        """With no frame yet, lights_on reads the state instead of blind-toggling."""
        coordinator = _okin_connected_coordinator(_okin_state_frame(_LIGHT_BIT))
        controller = LeggettOkinController(coordinator)
        controller.write_command = AsyncMock()
        controller.write_command_paced = AsyncMock()

        await controller.lights_on()

        coordinator.client.read_gatt_char.assert_awaited_once_with(LEGGETT_OKIN_NOTIFY_CHAR_UUID)
        controller.write_command.assert_not_called()

        # The read hydrated the cache, so the next decision does not read again.
        coordinator.client.read_gatt_char.reset_mock()
        await controller.lights_off()
        coordinator.client.read_gatt_char.assert_not_called()
        assert controller.write_command.await_args.args == (bytes.fromhex("040200020000"),)
        assert controller.write_command_paced.await_args.args == (
            bytes.fromhex("040200000000"),
        )

    async def test_a_failed_read_falls_back_to_a_blind_toggle(self):
        """A box whose state characteristic refuses the read still gets the toggle."""
        coordinator = _okin_connected_coordinator()
        coordinator.client.read_gatt_char = AsyncMock(side_effect=BleakError("no such char"))
        controller = LeggettOkinController(coordinator)
        controller.write_command = AsyncMock()
        controller.write_command_paced = AsyncMock()

        await controller.lights_on()

        sent = [call.args[0] for call in controller.write_command.await_args_list]
        assert sent.count(bytes.fromhex("040200020000")) == 1

    async def test_an_unusable_read_falls_back_to_a_blind_toggle(self):
        """An empty read leaves the state unknown, so the toggle is still sent."""
        controller = LeggettOkinController(_okin_connected_coordinator())
        controller.write_command = AsyncMock()
        controller.write_command_paced = AsyncMock()

        await controller.lights_off()

        sent = [call.args[0] for call in controller.write_command.await_args_list]
        assert sent.count(bytes.fromhex("040200020000")) == 1


class TestLeggettOkinDiscreteLightControl:
    """Test the light capability that reported state makes honest."""

    def test_light_control_is_declared_discrete(self):
        """State-aware on/off is discrete control, whatever the wire primitive is."""
        controller = LeggettOkinController(MagicMock())

        assert controller.supports_discrete_light_control is True
        assert controller.supports_under_bed_lights is True

    def test_light_state_is_declared_feedback_driven(self):
        """State feedback is what makes the switch pessimistic instead of optimistic."""
        controller = LeggettOkinController(MagicMock())

        assert controller.supports_light_state_feedback is True

    async def test_light_state_joins_the_coordinator_hydration_keys(
        self,
        hass: HomeAssistant,
        mock_leggett_okin_config_entry,
        mock_coordinator_connected,
    ):
        """Discrete control is what puts under_bed_lights_on in the refresh gate.

        Without it the key set is empty, and an initial read that fails is never
        retried because the gate has nothing left to satisfy.
        """
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_okin_config_entry)
        await coordinator.async_connect()

        assert coordinator._readable_light_state_required_keys() == {"under_bed_lights_on"}

    async def test_the_switch_state_comes_only_from_the_bed(
        self,
        hass: HomeAssistant,
        mock_leggett_okin_config_entry,
        mock_coordinator_connected,
        enable_custom_integrations,
    ):
        """Command success never flips the switch; only reported state drives it.

        The box ACKs writes it silently ignores on an unbonded link, so an
        optimistic flip could display a state the device never entered.
        """
        await hass.config_entries.async_setup(mock_leggett_okin_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][mock_leggett_okin_config_entry.entry_id]
        entity_id = er.async_get(hass).async_get_entity_id(
            "switch", DOMAIN, f"{OKIN_TEST_ADDRESS}_under_bed_lights"
        )
        assert entity_id is not None
        # The connect-time read returned nothing, so no state has been claimed yet.
        assert hass.states.get(entity_id).state == STATE_UNKNOWN

        await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_UNKNOWN

        # Only the bed's own report drives the state - in either direction.
        coordinator.controller._handle_status_notification(
            None, bytearray(_okin_state_frame(_LIGHT_BIT))
        )
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_ON

        coordinator.controller._handle_status_notification(None, bytearray(_okin_state_frame(0)))
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_OFF

    async def test_setup_hydrates_the_switch_from_the_connect_time_read(
        self,
        hass: HomeAssistant,
        mock_leggett_okin_config_entry,
        mock_coordinator_connected,
        mock_bleak_client,
        enable_custom_integrations,
    ):
        """The forced read at connect resolves the state before any user action."""
        frame = _okin_state_frame(_LIGHT_BIT)

        async def _read_gatt_char(target) -> bytes:
            if str(target) == LEGGETT_OKIN_NOTIFY_CHAR_UUID:
                return frame
            return b""

        mock_bleak_client.read_gatt_char = AsyncMock(side_effect=_read_gatt_char)

        await hass.config_entries.async_setup(mock_leggett_okin_config_entry.entry_id)
        await hass.async_block_till_done()

        entity_id = er.async_get(hass).async_get_entity_id(
            "switch", DOMAIN, f"{OKIN_TEST_ADDRESS}_under_bed_lights"
        )
        assert entity_id is not None
        assert hass.states.get(entity_id).state == STATE_ON

    async def test_reconnect_rereads_the_light_state(
        self,
        hass: HomeAssistant,
        mock_leggett_okin_config_entry,
        mock_coordinator_connected,
        mock_bleak_client,
    ):
        """Every (re)connect repeats the forced read, correcting state missed offline."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_okin_config_entry)
        assert await coordinator.async_connect()
        assert "under_bed_lights_on" not in coordinator.controller_state

        await coordinator.async_disconnect()

        frame = _okin_state_frame(_LIGHT_BIT)

        async def _read_gatt_char(target) -> bytes:
            if str(target) == LEGGETT_OKIN_NOTIFY_CHAR_UUID:
                return frame
            return b""

        mock_bleak_client.read_gatt_char = AsyncMock(side_effect=_read_gatt_char)

        assert await coordinator.async_connect()
        assert coordinator.controller_state["under_bed_lights_on"] is True

    async def test_explicit_disconnect_then_connect_corrects_state_changed_offline(
        self,
        hass: HomeAssistant,
        mock_leggett_okin_config_entry,
        mock_coordinator_connected,
        mock_bleak_client,
    ):
        """The Connect button must hydrate even when the connect-time read fails.

        Hardware sequence: Disconnect button, physical remote toggles the
        light, Connect button. The connect-time read can fail while the link
        is not yet re-encrypted (unbonded reads get ATT error 5); the retry
        must not be suppressed by the stale value the previous connection left
        in controller_state, or the switch freezes on the stale state.
        """
        state = {"frame": _okin_state_frame(_LIGHT_BIT), "failures": 0}

        async def _read_gatt_char(target) -> bytes:
            if str(target) != LEGGETT_OKIN_NOTIFY_CHAR_UUID:
                return b""
            if state["failures"] > 0:
                state["failures"] -= 1
                raise BleakError("ATT error: 0x05 (Insufficient Authentication)")
            return state["frame"]

        mock_bleak_client.read_gatt_char = AsyncMock(side_effect=_read_gatt_char)

        coordinator = AdjustableBedCoordinator(hass, mock_leggett_okin_config_entry)
        # The switch keeps its controller-state subscription across reconnects;
        # the refresh-retry gate requires a subscriber.
        coordinator.register_controller_state_callback(lambda _state: None)
        assert await coordinator.async_connect()
        assert coordinator.controller_state["under_bed_lights_on"] is True

        # Disconnect button, then the remote turns the light off while offline.
        await coordinator.async_disconnect(serialize_with_commands=True)
        state["frame"] = _okin_state_frame(0)
        state["failures"] = 1

        with patch(
            "custom_components.adjustable_bed.coordinator._READABLE_LIGHT_STATE_RETRY_DELAY",
            0,
        ):
            # The Connect button calls async_ensure_connected.
            assert await coordinator.async_ensure_connected()
            await hass.async_block_till_done()
            await hass.async_block_till_done()

        assert coordinator.controller_state["under_bed_lights_on"] is False


class TestLeggettGen2CommandFormat:
    """Verify Gen2 motor command bytes match the LP Control app (issue #385)."""

    def test_motor_command_bytes(self):
        """Format is ``M {down}:{up}:{stop}`` with the code in exactly one field."""
        c = LeggettGen2Commands
        assert c.MOTOR_HEAD_UP == b"M :0:"
        assert c.MOTOR_HEAD_DOWN == b"M 0::"
        assert c.MOTOR_HEAD_STOP == b"M ::0"
        assert c.MOTOR_FEET_UP == b"M :1:"
        assert c.MOTOR_FEET_DOWN == b"M 1::"
        assert c.MOTOR_FEET_STOP == b"M ::1"
        assert c.MOTOR_PILLOW_UP == b"M :2:"
        assert c.MOTOR_LUMBAR_UP == b"M :3:"
        assert c.MOTOR_STOP_ALL == b"STOP"

    def test_motor_move_builder(self):
        """The builder lays fields out as down:up:stop."""
        assert LeggettGen2Commands.motor_move(up="0") == b"M :0:"
        assert LeggettGen2Commands.motor_move(down="1") == b"M 1::"
        assert LeggettGen2Commands.motor_move(stop="2") == b"M ::2"

    def test_massage_command_bytes(self):
        """Massage uses VII (relative) / MVI (absolute) / MMODE / WVE TOGGLE."""
        c = LeggettGen2Commands
        assert c.MASSAGE_HEAD_UP == b"VII :0"
        assert c.MASSAGE_HEAD_DOWN == b"VII 0::"
        assert c.MASSAGE_FOOT_UP == b"VII :1"
        assert c.MASSAGE_FOOT_DOWN == b"VII 1::"
        assert c.MASSAGE_ALL_UP == b"VII :0123"
        assert c.MASSAGE_ALL_DOWN == b"VII 0123::"
        assert c.WAVE_TOGGLE == b"WVE TOGGLE"
        assert c.massage_set(0, 0) == b"MVI 0:0"  # off, head channel
        assert c.massage_set(1, 3) == b"MVI 1:3"  # foot channel, high
        # Mode codes verified from Gen2CommandFormatter: wave=0, pulse=1, always-on=2.
        assert c.massage_mode(c.MODE_WAVE) == b"MMODE 0:0"
        assert c.massage_mode(c.MODE_PULSE) == b"MMODE 0:1"
        assert c.massage_mode(c.MODE_ALWAYS_ON) == b"MMODE 0:2"


class TestLeggettGen2Connection:
    """Gen2 / LP Comfort Connect connection behaviour."""

    @staticmethod
    def _coordinator(hass: HomeAssistant, bed_type: str, variant: str):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
                CONF_NAME: "LP Bed",
                CONF_BED_TYPE: bed_type,
                CONF_PROTOCOL_VARIANT: variant,
            },
            unique_id="AA:BB:CC:DD:EE:FF",
        )
        entry.add_to_hass(hass)
        return AdjustableBedCoordinator(hass, entry)

    @pytest.mark.parametrize(
        ("bed_type", "variant", "expected"),
        [
            (BED_TYPE_LEGGETT_GEN2, VARIANT_AUTO, True),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_GEN2, True),
            # Before a controller is resolved, only auto/gen2 leggett_platt is
            # assumed persistent; okin and mlrm reconnect normally.
            (BED_TYPE_LEGGETT_PLATT, VARIANT_AUTO, True),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_MLRM, False),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_OKIN, False),
        ],
    )
    async def test_persistent_connection_fallback(
        self, hass: HomeAssistant, bed_type: str, variant: str, expected: bool
    ):
        """Pre-connect (no controller, no cache): fall back to the bed-type heuristic."""
        coordinator = self._coordinator(hass, bed_type, variant)
        assert coordinator._controller is None
        assert coordinator._persistent_connection_resolved is None
        assert coordinator._uses_persistent_connection() is expected

    async def test_resolved_controller_is_authoritative(self, hass: HomeAssistant):
        """Once resolved, the controller decides — a leggett_platt+auto that
        resolved to WiLinke/MlRM is NOT persistent; one that resolved to Gen2 is
        (issue #385 review, both directions)."""
        from custom_components.adjustable_bed.beds.leggett_gen2 import LeggettGen2Controller
        from custom_components.adjustable_bed.beds.leggett_wilinke import (
            LeggettWilinkeController,
        )

        coordinator = self._coordinator(hass, BED_TYPE_LEGGETT_PLATT, VARIANT_AUTO)

        coordinator._controller = LeggettWilinkeController(coordinator)
        assert coordinator._uses_persistent_connection() is False

        coordinator._controller = LeggettGen2Controller(coordinator)
        assert coordinator._uses_persistent_connection() is True

    async def test_persistence_cached_across_disconnect(self, hass: HomeAssistant):
        """_on_disconnect clears the controller before the reconnect decision, so
        the cached resolved value must drive it — an MlRM bed resolved via auto
        must stay non-persistent (and thus reconnect) after the drop (issue #385
        review)."""
        coordinator = self._coordinator(hass, BED_TYPE_LEGGETT_PLATT, VARIANT_AUTO)

        # Resolved to a non-persistent controller, then cleared on disconnect.
        coordinator._persistent_connection_resolved = False
        coordinator._controller = None
        assert coordinator._uses_persistent_connection() is False

        # Resolved to a persistent (Gen2) controller, then cleared.
        coordinator._persistent_connection_resolved = True
        assert coordinator._uses_persistent_connection() is True

    @pytest.mark.parametrize(
        ("bed_type", "variant", "expected"),
        [
            (BED_TYPE_LEGGETT_GEN2, None, True),
            (BED_TYPE_LEGGETT_GEN2, VARIANT_AUTO, True),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_GEN2, True),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_OKIN, True),
            (BED_TYPE_LEGGETT_PLATT, VARIANT_AUTO, False),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_MLRM, False),
        ],
    )
    async def test_gen2_requires_pairing(
        self, bed_type: str, variant: str | None, expected: bool
    ):
        """LP Control calls createBond() for Gen2 after service discovery."""
        assert requires_pairing(bed_type, variant) is expected

    @pytest.mark.parametrize(
        ("bed_type", "variant", "expected"),
        [
            (BED_TYPE_LEGGETT_GEN2, None, True),
            (BED_TYPE_LEGGETT_GEN2, VARIANT_AUTO, True),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_GEN2, True),
            # Okin-style pairing beds accept the connection and only gate GATT
            # access, so a connect timeout there is not a pairing symptom.
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_OKIN, False),
            (BED_TYPE_LEGGETT_PLATT, VARIANT_AUTO, False),
            (BED_TYPE_OKIMAT, None, False),
        ],
    )
    async def test_connection_gated_by_bond(
        self, bed_type: str, variant: str | None, expected: bool
    ):
        """Only Gen2 showed the bond-gated timeout signature in issue #385."""
        assert connection_gated_by_bond(bed_type, variant) is expected

    @pytest.mark.parametrize(
        ("bed_type", "variant", "expected"),
        [
            (BED_TYPE_LEGGETT_GEN2, None, True),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_GEN2, True),
            (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_OKIN, False),
            (BED_TYPE_LEGGETT_PLATT, VARIANT_AUTO, False),
            (BED_TYPE_OKIMAT, None, False),
        ],
    )
    async def test_pairing_order(
        self, bed_type: str, variant: str | None, expected: bool
    ) -> None:
        """Only LP Gen2 connects and discovers GATT before requesting a bond."""
        assert requires_pairing_after_service_discovery(bed_type, variant) is expected

    async def test_gen2_unexpected_disconnect_schedules_auto_reconnect(
        self,
        hass: HomeAssistant,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """A persistent Gen2 bed should promptly repair an unexpected drop."""
        del mock_coordinator_connected
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="LP Comfort Connect Bed",
            data={
                CONF_ADDRESS: "AA:BB:CC:DD:EE:85",
                CONF_NAME: "Smart Bed 22D8",
                CONF_BED_TYPE: BED_TYPE_LEGGETT_GEN2,
                CONF_MOTOR_COUNT: 2,
                CONF_HAS_MASSAGE: False,
                CONF_DISABLE_ANGLE_SENSING: True,
                CONF_PREFERRED_ADAPTER: "auto",
            },
            unique_id="AA:BB:CC:DD:EE:85",
            entry_id="leggett_gen2_auto_reconnect_test",
        )
        entry.add_to_hass(hass)

        coordinator = AdjustableBedCoordinator(hass, entry)
        assert coordinator._auto_reconnect_enabled() is True
        await coordinator.async_connect()
        mock_bleak_client.is_connected = False

        coordinator._on_disconnect(mock_bleak_client)

        assert coordinator._reconnect_timer is not None
        coordinator._reconnect_timer.cancel()
        coordinator._reconnect_timer = None


class TestLeggettGen2Capabilities:
    """Gen2 capabilities are gated per model by the bundled product profile."""

    @staticmethod
    def _controller(hass, entry, product_id: int) -> LeggettGen2Controller:
        # Payload encodes the productId: "XP" + little-endian bytes -> reversed
        # first 4 hex -> base16. For ids < 256 that is just byte[2].
        payload = bytes([0x58, 0x50]) + product_id.to_bytes(4, "little")
        coordinator = AdjustableBedCoordinator(hass, entry)
        return LeggettGen2Controller(coordinator, manufacturer_data={0x092D: payload})

    async def test_rgb_bed_with_pillow_lumbar(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        c = self._controller(hass, mock_leggett_gen2_config_entry, 5)
        assert c.supports_lights and c.supports_light_color_control
        assert c.has_pillow_support and c.has_lumbar_support
        assert c.memory_slot_count == 3  # not the old hardcoded 4

    async def test_toggle_light_bed_without_pillow_lumbar(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        c = self._controller(hass, mock_leggett_gen2_config_entry, 8)
        assert c.supports_lights and not c.supports_light_color_control
        assert not c.has_pillow_support and not c.has_lumbar_support

    async def test_bed_without_light(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        c = self._controller(hass, mock_leggett_gen2_config_entry, 10011)
        assert not c.supports_lights and not c.supports_light_color_control
        assert c.default_light_rgb_color is None

    async def test_unknown_product_falls_back_to_full_featured(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        c = self._controller(hass, mock_leggett_gen2_config_entry, 999999)
        assert c.supports_light_color_control and c.has_pillow_support

    async def test_no_light_no_massage_profile_suppresses_helpers(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        # Product 10011: head-only, no foot, no light, no massage.
        c = self._controller(hass, mock_leggett_gen2_config_entry, 10011)
        # Light + massage capability helpers are gated off (no phantom entities).
        assert not c.supports_light_toggle_control
        assert not c.supports_under_bed_lights
        assert not c.supports_massage_off_control
        assert not c.supports_massage_toggle_control
        # Motor specs expose only present primary actuators (head, not foot).
        keys = {spec.key for spec in c.motor_control_specs}
        assert "back" in keys  # head present
        assert "legs" not in keys  # no foot

    async def test_full_profile_keeps_helpers(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        c = self._controller(hass, mock_leggett_gen2_config_entry, 5)
        assert c.supports_light_toggle_control and c.supports_massage_off_control
        keys = {spec.key for spec in c.motor_control_specs}
        assert {"back", "legs"} <= keys
        # Gen2 never exposes the base's duplicate head/feet specs.
        assert not ({"head", "feet"} & keys)

    async def test_no_massage_profile_hides_zone_buttons(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        # Product 10011: no massage -> all massage helpers (incl. zone) report False.
        c = self._controller(hass, mock_leggett_gen2_config_entry, 10011)
        assert not c.supports_head_massage_intensity_step_control
        assert not c.supports_foot_massage_intensity_step_control
        assert not c.supports_massage_off_control

    async def test_manual_disconnect_strands_connection(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        # Keep this hidden until bonded reconnects are confirmed on hardware.
        assert self._controller(
            hass, mock_leggett_gen2_config_entry, 5
        ).manual_disconnect_strands_connection is True

    async def test_stale_motor_keys_for_absent_actuators(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        # Product 10011: head only -> legs/pillow/lumbar removable, plus the
        # duplicate head/feet keys Gen2 never exposes.
        c = self._controller(hass, mock_leggett_gen2_config_entry, 10011)
        assert c.stale_motor_entity_keys == frozenset(
            {"legs", "pillow", "lumbar", "head", "feet"}
        )
        # Full profile (5): only the duplicate head/feet keys are stale.
        assert self._controller(
            hass, mock_leggett_gen2_config_entry, 5
        ).stale_motor_entity_keys == frozenset({"head", "feet"})
        # No-actuator profile (10014): everything is stale.
        assert self._controller(
            hass, mock_leggett_gen2_config_entry, 10014
        ).stale_motor_entity_keys == frozenset(
            {"back", "legs", "pillow", "lumbar", "head", "feet"}
        )

    async def test_set_light_color_updates_cache(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        controller = coordinator.controller

        # RGBSET is the RGB turn-on path -> cache must flip on, so a later off works.
        await controller.set_light_color((255, 0, 0))
        assert controller._light_on is True
        await controller.set_light_color((0, 0, 0))
        assert controller._light_on is False

    async def test_massage_toggle_gated_on_wave(
        self, hass: HomeAssistant, mock_leggett_gen2_config_entry
    ):
        # Product 10012: massage but NO wave -> no massage toggle (WVE TOGGLE),
        # but intensity step + mode step are still available.
        c = self._controller(hass, mock_leggett_gen2_config_entry, 10012)
        assert c.supports_massage_off_control
        assert c.supports_massage_intensity_step_control
        assert c.supports_massage_mode_step_control
        assert not c.supports_massage_toggle_control
        # Product 5 has wave -> toggle available.
        assert self._controller(
            hass, mock_leggett_gen2_config_entry, 5
        ).supports_massage_toggle_control

    async def test_massage_intensity_all_and_mode_step(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        controller = coordinator.controller

        await controller.massage_intensity_up()
        assert mock_bleak_client.write_gatt_char.call_args[0][1] == b"VII :0123"
        await controller.massage_intensity_down()
        assert mock_bleak_client.write_gatt_char.call_args[0][1] == b"VII 0123::"

        # Mode step cycles wave -> pulse -> always-on -> wave (fallback profile has wave).
        sent = []
        for _ in range(4):
            await controller.massage_mode_step()
            sent.append(mock_bleak_client.write_gatt_char.call_args[0][1])
        assert sent == [b"MMODE 0:1", b"MMODE 0:2", b"MMODE 0:0", b"MMODE 0:1"]

    async def test_lights_on_off_idempotent_and_optimistic(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        controller = coordinator.controller

        # Unknown state -> on toggles and caches True; repeat is a no-op.
        await controller.lights_on()
        assert controller._light_on is True
        mock_bleak_client.write_gatt_char.reset_mock()
        await controller.lights_on()
        mock_bleak_client.write_gatt_char.assert_not_called()

        # Off toggles once and caches False; repeat is a no-op (no inversion).
        await controller.lights_off()
        assert controller._light_on is False
        mock_bleak_client.write_gatt_char.reset_mock()
        await controller.lights_off()
        mock_bleak_client.write_gatt_char.assert_not_called()


class TestLeggettMovement:
    """Test Leggett & Platt movement commands."""

    async def test_move_head_up_gen2_sends_command(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test move head up on Gen2 sends motor command."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        with patch("custom_components.adjustable_bed.beds.leggett_gen2.asyncio.sleep"):
            await coordinator.controller.move_head_up()

        # _move_with_stop sends the head-up command then the per-actuator stop
        assert mock_bleak_client.write_gatt_char.called
        last_call = mock_bleak_client.write_gatt_char.call_args
        assert last_call[0][1] == LeggettGen2Commands.MOTOR_HEAD_STOP
        # The move command itself (head up = "M :0:") was sent before the stop
        sent = [c[0][1] for c in mock_bleak_client.write_gatt_char.call_args_list]
        assert LeggettGen2Commands.MOTOR_HEAD_UP in sent

    async def test_move_head_stop_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test move head stop sends the per-actuator head stop command."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.move_head_stop()

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, LeggettGen2Commands.MOTOR_HEAD_STOP, response=True
        )

    async def test_stop_all_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test stop all sends MOTOR_STOP_ALL command."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.stop_all()

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, LeggettGen2Commands.MOTOR_STOP_ALL, response=True
        )


class TestLeggettPresets:
    """Test Leggett & Platt preset commands."""

    async def test_preset_flat_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test preset flat command on Gen2."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        mock_bleak_client.write_gatt_char.reset_mock()  # ignore the GET STATE on connect

        with patch("custom_components.adjustable_bed.beds.leggett_gen2.asyncio.sleep"):
            await coordinator.controller.preset_flat()

        first_call = mock_bleak_client.write_gatt_char.call_args_list[0]
        assert first_call[0][1] == LeggettGen2Commands.PRESET_FLAT

    async def test_preset_anti_snore_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test preset anti-snore command on Gen2."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        mock_bleak_client.write_gatt_char.reset_mock()  # ignore the GET STATE on connect

        with patch("custom_components.adjustable_bed.beds.leggett_gen2.asyncio.sleep"):
            await coordinator.controller.preset_anti_snore()

        first_call = mock_bleak_client.write_gatt_char.call_args_list[0]
        assert first_call[0][1] == LeggettGen2Commands.PRESET_ANTI_SNORE

    @pytest.mark.parametrize(
        "memory_num,expected_command",
        [
            (1, LeggettGen2Commands.PRESET_UNWIND),
            (2, LeggettGen2Commands.PRESET_SLEEP),
            (3, LeggettGen2Commands.PRESET_WAKE_UP),
            (4, LeggettGen2Commands.PRESET_RELAX),
        ],
    )
    async def test_preset_memory_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
        memory_num: int,
        expected_command: bytes,
    ):
        """Test preset memory commands on Gen2."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        mock_bleak_client.write_gatt_char.reset_mock()  # ignore the GET STATE on connect

        with patch("custom_components.adjustable_bed.beds.leggett_gen2.asyncio.sleep"):
            await coordinator.controller.preset_memory(memory_num)

        first_call = mock_bleak_client.write_gatt_char.call_args_list[0]
        assert first_call[0][1] == expected_command

    @pytest.mark.parametrize(
        "memory_num,expected_command",
        [
            (1, LeggettGen2Commands.PROGRAM_UNWIND),
            (2, LeggettGen2Commands.PROGRAM_SLEEP),
            (3, LeggettGen2Commands.PROGRAM_WAKE_UP),
            (4, LeggettGen2Commands.PROGRAM_RELAX),
        ],
    )
    async def test_program_memory_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
        memory_num: int,
        expected_command: bytes,
    ):
        """Test program memory commands on Gen2."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.program_memory(memory_num)

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, expected_command, response=True
        )


class TestLeggettLights:
    """Test Leggett & Platt light commands."""

    async def test_lights_toggle_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test lights toggle on Gen2 sends the UBL TOGGLE command."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.lights_toggle()

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, LeggettGen2Commands.LIGHT_TOGGLE, response=True
        )
        assert LeggettGen2Commands.LIGHT_TOGGLE == b"UBL TOGGLE"

    async def test_lights_on_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Lights on (state unknown) sends the toggle; idempotent once state known."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        controller = coordinator.controller

        # State unknown -> toggle to turn on.
        await controller.lights_on()
        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, LeggettGen2Commands.LIGHT_TOGGLE, response=True
        )

        # Once the bed reports the light is on, lights_on is a no-op (no inversion).
        controller._light_on = True
        mock_bleak_client.write_gatt_char.reset_mock()
        await controller.lights_on()
        mock_bleak_client.write_gatt_char.assert_not_called()

    async def test_lights_off_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test lights off Gen2 uses the confirmed toggle (UBL TOGGLE)."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.lights_off()

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, LeggettGen2Commands.LIGHT_TOGGLE, response=True
        )


class TestLeggettGen2RgbLights:
    """Test Leggett & Platt Gen2 RGB light controls."""

    async def test_supports_light_color_control(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
    ):
        """Test Gen2 controller reports RGB light color support."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        assert coordinator.controller.supports_light_color_control is True

    async def test_supports_explicit_light_on_control(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
    ):
        """Gen2 has no separate explicit on command (only UBL TOGGLE); setting a
        colour turns RGB lights on, so explicit-on is reported False."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        assert coordinator.controller.supports_explicit_light_on_control is False

    async def test_default_light_rgb_color(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
    ):
        """Test Gen2 controller returns white as default RGB color."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        assert coordinator.controller.default_light_rgb_color == (255, 255, 255)

    async def test_set_light_color(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test set_light_color sends correct RGBSET ASCII command."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.set_light_color((255, 0, 128))

        expected = LeggettGen2Commands.rgb_set(255, 0, 128, 255)
        assert expected == b"RGBSET 0:FF0080FF"
        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, expected, response=True
        )

    async def test_set_light_color_red(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test set_light_color with pure red produces correct hex."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.set_light_color((255, 0, 0))

        expected = b"RGBSET 0:FF0000FF"
        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, expected, response=True
        )

    async def test_set_light_color_green(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test set_light_color with pure green produces correct hex."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.set_light_color((0, 255, 0))

        expected = b"RGBSET 0:00FF00FF"
        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, expected, response=True
        )


class TestLeggettMassage:
    """Test Leggett & Platt massage commands."""

    async def test_massage_off_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Test massage off on Gen2."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.massage_off()

        calls = mock_bleak_client.write_gatt_char.call_args_list
        # Should send head(0) and foot(0)
        assert len(calls) >= 2

    async def test_massage_head_up_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Head massage up on Gen2 sends the relative increase command (VII :0)."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.massage_head_up()

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, LeggettGen2Commands.MASSAGE_HEAD_UP, response=True
        )
        assert LeggettGen2Commands.MASSAGE_HEAD_UP == b"VII :0"

    async def test_massage_toggle_gen2(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Massage toggle on Gen2 sends the wave toggle (WVE TOGGLE)."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.massage_toggle()

        mock_bleak_client.write_gatt_char.assert_called_with(
            LEGGETT_GEN2_WRITE_CHAR_UUID, LeggettGen2Commands.WAVE_TOGGLE, response=True
        )
        assert LeggettGen2Commands.WAVE_TOGGLE == b"WVE TOGGLE"


class TestLeggettPositionNotifications:
    """Test Leggett & Platt position notification handling."""

    async def test_start_notify_subscribes_to_state_char(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
        mock_bleak_client: MagicMock,
    ):
        """Gen2 subscribes to the 45e25103 STATE characteristic for light state."""
        from custom_components.adjustable_bed.const import LEGGETT_GEN2_READ_CHAR_UUID

        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()

        await coordinator.controller.start_notify(None)

        chars = [c.args[0] for c in mock_bleak_client.start_notify.call_args_list]
        assert LEGGETT_GEN2_READ_CHAR_UUID in chars
        assert coordinator.controller._notify_started is True

    async def test_light_state_notification_parses_on_off(
        self,
        hass: HomeAssistant,
        mock_leggett_gen2_config_entry,
        mock_coordinator_connected,
    ):
        """STATE byte 6 = OperatingMode (0x01 on / 0x04 off); 7-9 = RGB."""
        coordinator = AdjustableBedCoordinator(hass, mock_leggett_gen2_config_entry)
        await coordinator.async_connect()
        controller = coordinator.controller

        # Constant-colour (on), red: bytes [..6 header.., 0x01, FF, 00, 00, FF, ...]
        on_frame = bytearray([0x6E, 0x20, 0x00, 0x04, 0x01, 0x03, 0x01, 0xFF, 0x00, 0x00, 0xFF])
        controller._handle_state_notification(None, on_frame)
        assert controller._light_on is True
        assert controller._light_rgb == (0xFF, 0x00, 0x00)
        assert coordinator.controller_state.get("under_bed_lights_on") is True
        # RGB must be published to controller state, not just cached privately.
        assert coordinator.controller_state.get("under_bed_lights_rgb") == (0xFF, 0x00, 0x00)

        # Off frame (byte 6 = 0x04).
        off_frame = bytearray([0x6E, 0x20, 0x00, 0x04, 0x01, 0x03, 0x04, 0x00, 0x00, 0x00, 0x00])
        controller._handle_state_notification(None, off_frame)
        assert controller._light_on is False
        assert coordinator.controller_state.get("under_bed_lights_on") is False

        # A non-light frame (byte 6 outside {0x01,0x04}) is ignored.
        controller._handle_state_notification(
            None, bytearray([0] * 6 + [0x09] + [0] * 5)
        )
        assert controller._light_on is False  # unchanged
