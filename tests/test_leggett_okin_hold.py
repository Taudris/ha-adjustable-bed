"""Tests for the CU170's hold surface.

Its encoder and link-bound writer, and the two counters that read the box's
own notifications: the receipt credit gate and the light pulse counter behind a
staged gesture's cue.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.adjustable_bed.beds.leggett_okin_evidence import (
    _WARNED_ADDRESSES,
    CREDIT_EXTRA_EVERY,
    CREDIT_WINDOW,
    LIGHT_STATE_BIT,
    LightPulseCounter,
    OkinStreamFeedback,
    ReceiptCreditGate,
)
from custom_components.adjustable_bed.beds.leggett_okin_hold import (
    CU170_STREAM_PROFILE,
    FACTORY_RESET,
    FRAME_INTERVAL_MS,
    HOLD_TTL_MAX_MS,
    LATCH_MODE_ENABLE,
    LIGHT_TOGGLE,
    MODE_STAGE_CEILING_MS,
    STORE_STAGE_CEILING_MS,
    LeggettOkinCommands,
    OkinFrameEncoder,
    OkinFrameWriter,
    build_frame,
    okin_dummy_program,
    okin_mode_program,
    okin_store_program,
)
from custom_components.adjustable_bed.const import LEGGETT_OKIN_CHAR_UUID
from custom_components.adjustable_bed.hold_operation import CuedStage, CueRequest, PressStage
from custom_components.adjustable_bed.hold_roster import (
    Control,
    ControlDeclaration,
    ControlRoster,
    HoldSupport,
    PressFloor,
)
from custom_components.adjustable_bed.hold_streamer import HoldStreamer, SendVerdict
from custom_components.adjustable_bed.services import MAX_TIMED_MOVE_DURATION_MS

HEAD_UP = Control("motor-head-up")
FEET_UP = Control("motor-feet-up")
LIGHT = Control("light-toggle")
PING = Control("ping")
RELEASE = bytes.fromhex("040200000000")

SEND_UNCONFIRMED = SendVerdict.WRITE_COMMAND
SEND_BARRIER = SendVerdict.WRITE_REQUEST_BARRIER


@pytest.fixture(autouse=True)
def forget_stall_warnings():
    """Forget which addresses have warned, so each test starts unwarned."""
    _WARNED_ADDRESSES.clear()
    yield
    _WARNED_ADDRESSES.clear()


def _writer(client: MagicMock) -> OkinFrameWriter:
    """Return a writer over client."""
    return OkinFrameWriter(
        client=client,
        ble_lock=asyncio.Lock(),
        characteristic_uuid=LEGGETT_OKIN_CHAR_UUID,
    )


def _client() -> MagicMock:
    """Return a connected BLE client double."""
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    return client


class TestCu170Constants:
    """The two figures this module states that another module also states."""

    def test_the_emission_floor_is_the_vendors_cadence_not_the_entrys_pulse(self):
        """emission-cadence: F is the box-proven cadence, not a configurable.

        The entry's default pulse delay is the same number today, and a change
        to that default must not move the wire cadence with it.
        """
        assert CU170_STREAM_PROFILE.frame_interval_ms == FRAME_INTERVAL_MS
        assert FRAME_INTERVAL_MS == 100

    def test_the_hold_cap_equals_the_services_own_maximum(self):
        """motor-ttl-max: no clamp binds a valid timed_move.

        Two modules state this figure - the bed's roster and the service's
        schema - and importing one from the other would point a bed module at
        the service layer, so the equality is pinned here instead.
        """
        assert HOLD_TTL_MAX_MS == MAX_TIMED_MOVE_DURATION_MS


class TestReceiptEvidenceConstants:
    """The window the receipt evidence runs under."""

    def test_the_credit_window_is_eight(self):
        """receipt-paced-write-commands: moving the window is a decision, so it is pinned."""
        assert CREDIT_WINDOW == 8


class TestOkinFrameEncoder:
    """frame-is-the-or: one frame is the OR of every key held down."""

    def test_a_frame_is_the_or_of_the_controls_it_carries(self):
        """frame-is-the-or: the encoder composes the whole expressed set."""
        encoder = OkinFrameEncoder(1)

        assert encoder.encode(frozenset({HEAD_UP})) == bytes.fromhex("040200000001")
        assert encoder.encode(frozenset({HEAD_UP, FEET_UP})) == bytes.fromhex("040200000005")
        assert encoder.encode(frozenset({LIGHT})) == bytes.fromhex("040200020000")

    def test_the_empty_set_encodes_as_the_release_frame(self):
        """single-release-per-lifecycle: a release is not a different kind of frame."""
        assert OkinFrameEncoder(1).encode(frozenset()) == RELEASE

    def test_ping_carries_no_bit_of_its_own(self):
        """ping: the benchmark's frames are bit-empty, and end no lifecycle."""
        assert OkinFrameEncoder(1).encode(frozenset({PING})) == RELEASE

    @pytest.mark.parametrize(
        ("slot", "keycode"),
        [(1, "00001000"), (2, "00002000"), (3, "00004000"), (4, "00008000")],
    )
    def test_the_memory_slot_ladder_is_exact(self, slot: int, keycode: str):
        """Every slot recalls its own bit.

        The ladder previously started at 0x2000 and ran to 0x10000, so every
        slot recalled its neighbour and "memory 4" actually sent the store-arm
        keycode (issue #368).
        """
        encoder = OkinFrameEncoder(1)

        assert encoder.encode(frozenset({Control(f"preset-{slot}")})) == bytes.fromhex(
            f"0402{keycode}"
        )

    def test_the_store_arm_bit_is_never_a_slot_bit(self):
        """The arm keycode overwrites the next recalled slot, so it recalls nothing."""
        arm = OkinFrameEncoder(1).encode(frozenset({Control("store-preset-1")}))

        assert arm == bytes.fromhex("040200010000")
        assert LeggettOkinCommands.MEMORY_STORE not in {
            LeggettOkinCommands.PRESET_MEMORY_1,
            LeggettOkinCommands.PRESET_MEMORY_2,
            LeggettOkinCommands.PRESET_MEMORY_3,
            LeggettOkinCommands.PRESET_MEMORY_4,
        }

    def test_revision_zero_takes_the_checksummed_framing(self):
        """The two revisions differ in framing, not in the keycode they carry."""
        assert OkinFrameEncoder(0).encode(frozenset({HEAD_UP})) == build_frame(
            LeggettOkinCommands.MOTOR_HEAD_UP, 0
        )
        assert OkinFrameEncoder(1).encode(frozenset({HEAD_UP})) == build_frame(
            LeggettOkinCommands.MOTOR_HEAD_UP, 1
        )

    def test_an_unresolved_revision_is_refused_rather_than_guessed(self):
        """The stream and the command path answer one unresolved state alike."""
        with pytest.raises(ConnectionError, match="not resolved"):
            OkinFrameEncoder(None).encode(frozenset({HEAD_UP}))
        with pytest.raises(ConnectionError, match="not resolved"):
            OkinFrameEncoder(None).encode(frozenset())

    def test_a_control_this_bed_has_no_keycode_for_is_a_defect(self):
        """The encoder covers every control the roster declares, and nothing else."""
        with pytest.raises(KeyError):
            OkinFrameEncoder(1).encode(frozenset({Control("motor-nose-up")}))


class TestOkinFrameWriter:
    """ha-api-only: what a submission returns, and what it files."""

    async def test_an_unconfirmed_frame_returns_nothing_to_wait_on(self):
        """ha-api-only: a Write Command's fate is never learned."""
        client = _client()
        writer = _writer(client)

        assert writer.submit(bytes.fromhex("040200000001")) is None
        await asyncio.sleep(0)

        client.write_gatt_char.assert_awaited_once_with(
            LEGGETT_OKIN_CHAR_UUID, bytes.fromhex("040200000001"), response=False
        )

    async def test_a_confirmed_frame_returns_its_completion(self):
        """release-is-a-write-request: the release's round trip is readable."""
        client = _client()
        writer = _writer(client)

        await writer.submit_confirmed(RELEASE)

        client.write_gatt_char.assert_awaited_once_with(
            LEGGETT_OKIN_CHAR_UUID, RELEASE, response=True
        )

    async def test_a_failed_unconfirmed_frame_raises_at_no_caller(self):
        """receipts-positive-only: nothing waits on a Write Command, error included."""
        client = _client()
        client.is_connected = False
        writer = _writer(client)

        assert writer.submit(RELEASE) is None
        await asyncio.sleep(0)

    async def test_a_failed_confirmed_frame_carries_its_error(self):
        """release-is-a-write-request: the completion is where a failure shows."""
        client = _client()
        client.is_connected = False
        writer = _writer(client)

        with pytest.raises(ConnectionError):
            await writer.submit_confirmed(RELEASE)

    async def test_a_stop_drops_a_barrier_still_queued_on_the_lock(self):
        """ha-api-only: a stop withdraws the barrier the pump awaits, and the release follows.

        The pump awaits the write's own task, so cancelling the pump cancels a
        barrier that has not reached the link yet. That barrier carries the same
        keys as the frame before it, and the release queues directly behind it.
        """
        client = _client()
        lock = asyncio.Lock()
        streamer = _light_toggle_streamer(
            OkinFrameWriter(
                client=client, ble_lock=lock, characteristic_uuid=LEGGETT_OKIN_CHAR_UUID
            )
        )
        await lock.acquire()
        streamer.hold({LIGHT_TOGGLE: 10.0})
        for _ in range(3):
            await asyncio.sleep(0)

        streamer.release_wire()
        lock.release()
        for _ in range(3):
            await asyncio.sleep(0)

        client.write_gatt_char.assert_awaited_once_with(
            LEGGETT_OKIN_CHAR_UUID, RELEASE, response=True
        )


def _light_toggle_streamer(
    writer: OkinFrameWriter, on_lifecycle_open: Callable[[bytes], None] = lambda frame: None
) -> HoldStreamer:
    """Return a revision-1 streamer over writer whose roster declares the light toggle.

    It carries no feedback, so every frame it streams is a Write Request.
    """
    return HoldStreamer(
        name="AA:BB",
        press_floor=ControlRoster(
            (
                ControlDeclaration(
                    control=LIGHT_TOGGLE,
                    press_floor=PressFloor(frames=1, ms=0),
                    hold=HoldSupport(ttl_max_ms=30000),
                ),
            )
        ).press_floor,
        encoder=OkinFrameEncoder(1),
        writer=writer,
        profile=CU170_STREAM_PROFILE,
        clock=lambda: 0.0,
        on_lifecycle_open=on_lifecycle_open,
    )


class TestStreamTraceEdge:
    """One command-trace entry per wire lifecycle, filed at the edge the streamer owns."""

    async def test_the_trace_records_the_frame_that_opened_the_lifecycle(self):
        """A per-frame trace would empty the coordinator's trace deque in ten seconds."""
        opened: list[bytes] = []
        streamer = _light_toggle_streamer(_writer(_client()), on_lifecycle_open=opened.append)

        streamer.hold({LIGHT_TOGGLE: 10.0})
        await asyncio.sleep(0)
        streamer.release_wire()
        streamer.hold({LIGHT_TOGGLE: 10.0})
        await asyncio.sleep(0)
        streamer.release_wire()

        assert opened == [bytes.fromhex("040200020000")] * 2


class TestReceiptCreditGate:
    """receipt-paced-write-commands: the depth bound the box's receipts give."""

    def test_a_send_spends_one_credit_and_a_receipt_returns_it(self):
        """receipt-paced-write-commands: the ladder, one frame at a time."""
        gate = ReceiptCreditGate()

        for _ in range(CREDIT_WINDOW):
            assert gate.before_send(0.0) == SEND_UNCONFIRMED
            gate.after_send(0.0, confirmed=False)

        assert gate.diagnostics["credit"] == 0

        gate.note_receipt()

        assert gate.diagnostics["credit"] == 1

    def test_credit_never_rises_above_the_window(self):
        """receipt-paced-write-commands: a receipt arriving at W returns nothing."""
        gate = ReceiptCreditGate()

        for _ in range(10):
            gate.note_receipt()

        assert gate.diagnostics["credit"] == CREDIT_WINDOW

    def test_every_ninth_receipt_returns_an_extra_credit(self):
        """receipt-paced-write-commands: what keeps pace under the measured loss."""
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)

        for _ in range(CREDIT_EXTRA_EVERY - 1):
            gate.note_receipt()
            gate.after_send(0.0, confirmed=False)
        assert gate.diagnostics["credit"] == 0

        gate.note_receipt()

        assert gate.diagnostics["credit"] == 2

    def test_exhausted_credit_calls_for_the_barrier(self):
        """receipt-paced-write-commands: at credit 0 the next frame is a Write Request."""
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)

        assert gate.before_send(1.0) == SEND_BARRIER
        assert gate.diagnostics["stalls"] == 1

    def test_nothing_leaves_under_the_barrier_until_it_completes(self):
        """receipt-paced-write-commands: the barrier withholds every wake behind it."""
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)
        assert gate.before_send(0.0) == SEND_BARRIER
        gate.after_send(0.0, confirmed=True)

        assert gate.before_send(0.1) is SendVerdict.WITHHOLD
        assert gate.diagnostics["barrier_outstanding"] is True

        gate.after_confirmation(0.2, sent_since=0)

        assert gate.diagnostics["barrier_outstanding"] is False
        assert gate.before_send(0.3) == SEND_UNCONFIRMED

    def test_a_completion_from_before_the_barrier_leaves_it_standing(self):
        """release-is-a-write-request: a completion proves its own write and what went before.

        A stop wrote a release after seven frames, and the next hold sent one
        more before it stalled. The release's completion proves the seven, so
        credit rises to the window less the two frames sent after it, but it
        proves nothing about the barrier that is one of those two.
        """
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)
        gate.before_send(1.0)
        gate.after_send(1.0, confirmed=True)

        gate.after_confirmation(1.1, sent_since=2)

        assert gate.diagnostics["credit"] == CREDIT_WINDOW - 2
        assert gate.diagnostics["barrier_outstanding"] is True
        assert gate.diagnostics["clears"] == 0
        assert gate.before_send(1.2) is SendVerdict.WITHHOLD

    def test_an_abandoned_barrier_moves_no_count(self):
        """receipt-paced-write-commands: a completion nobody awaits proves nothing.

        The next wake asks afresh, and credit still at zero calls for a new
        barrier.
        """
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)
        gate.before_send(1.0)
        gate.after_send(1.0, confirmed=True)

        gate.abandon_barrier()

        assert gate.diagnostics["barrier_outstanding"] is False
        assert gate.diagnostics["clears"] == 0
        assert gate.before_send(1.1) == SEND_BARRIER
        assert gate.diagnostics["stalls"] == 2

    def test_the_barriers_completion_resets_credit_and_times_the_clear(self):
        """receipt-paced-write-commands: the clear and its duration reach diagnostics."""
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)
        gate.before_send(1.0)
        gate.after_send(1.0, confirmed=True)

        gate.after_confirmation(1.25, sent_since=0)

        assert gate.diagnostics["credit"] == CREDIT_WINDOW
        assert gate.diagnostics["clears"] == 1
        assert gate.diagnostics["last_clear_ms"] == 250.0
        assert gate.before_send(1.3) == SEND_UNCONFIRMED

    def test_a_completion_raises_credit_by_what_it_proved_and_never_lowers_it(self):
        """release-is-a-write-request: credit rises to W less what went since."""
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)

        gate.after_confirmation(1.0, sent_since=3)

        assert gate.diagnostics["credit"] == CREDIT_WINDOW - 3

        gate.note_receipt()
        gate.note_receipt()
        gate.after_confirmation(2.0, sent_since=CREDIT_WINDOW)

        assert gate.diagnostics["credit"] == CREDIT_WINDOW - 1

    def test_elapsed_time_returns_no_credit(self):
        """receipt-paced-write-commands: no time-based forgiveness exists."""
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)

        assert gate.before_send(600.0) == SEND_BARRIER

    def test_the_gate_reports_its_first_stall_once(self):
        """receipt-paced-write-commands: the gate counts; its owner does the talking."""
        gate = ReceiptCreditGate()
        for _ in range(CREDIT_WINDOW):
            gate.after_send(0.0, confirmed=False)
        gate.before_send(1.0)

        assert gate.take_first_stall() is True
        assert gate.take_first_stall() is False

        gate.after_send(1.0, confirmed=True)
        gate.after_confirmation(1.1, sent_since=0)
        for _ in range(CREDIT_WINDOW):
            gate.after_send(1.2, confirmed=False)
        gate.before_send(1.3)

        assert gate.take_first_stall() is False


class TestLightPulseCounter:
    """The feedback vocabulary a staged gesture is acknowledged in."""

    def test_a_pulse_is_two_transitions_of_the_light_bit(self):
        """cue-or-ceiling: the cue is met at two transitions per pulse."""
        counter = LightPulseCounter()
        counter.note_led_mask(0)
        counter.begin_cue(CueRequest(1))

        assert counter.met() is False

        counter.note_led_mask(LIGHT_STATE_BIT)
        assert counter.met() is False

        counter.note_led_mask(0)
        assert counter.met() is True

    def test_a_stage_starts_from_no_transitions(self):
        """cue-or-ceiling: what the previous stage saw cannot satisfy the next."""
        counter = LightPulseCounter()
        counter.note_led_mask(0)
        counter.begin_cue(CueRequest(1))
        counter.note_led_mask(LIGHT_STATE_BIT)
        counter.note_led_mask(0)
        assert counter.met() is True

        counter.begin_cue(CueRequest(3))

        assert counter.met() is False

    def test_a_notification_that_moves_no_bit_counts_nothing(self):
        """The box publishes every state change, so a repeat is not a transition."""
        counter = LightPulseCounter()
        counter.note_led_mask(LIGHT_STATE_BIT)
        counter.begin_cue(CueRequest(1))

        for _ in range(6):
            counter.note_led_mask(LIGHT_STATE_BIT)

        assert counter.met() is False

    def test_no_cue_is_met_before_one_begins(self):
        """cue-or-ceiling: a stage with no cue waiting is not a satisfied one."""
        counter = LightPulseCounter()
        counter.note_led_mask(0)
        counter.note_led_mask(LIGHT_STATE_BIT)

        assert counter.met() is False


class TestOkinStreamFeedback:
    """One notification feeds both counters, and the owner decides what to say."""

    def test_the_first_stall_per_bed_device_warns(self, caplog):
        """receipt-paced-write-commands: one warning per bed device per HA start."""
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                feedback = OkinStreamFeedback("AA:BB")
                for _ in range(CREDIT_WINDOW):
                    feedback.after_send(0.0, confirmed=False)
                feedback.before_send(1.0)

        warnings = [record for record in caplog.records if "credit" in record.message]
        assert len(warnings) == 1

    def test_a_notification_is_a_receipt_and_a_cue_transition(self):
        """receipts-positive-only: presence is what the box's answer proves."""
        feedback = OkinStreamFeedback("AA:BB")
        feedback.after_send(0.0, confirmed=False)
        feedback.begin_cue(CueRequest(1))

        feedback.note_notification(0)
        feedback.note_notification(LIGHT_STATE_BIT)
        feedback.note_notification(0)

        assert feedback.cue_met() is True
        assert feedback.diagnostics["credit"] == CREDIT_WINDOW
        assert feedback.diagnostics["receipts"] == 3


class TestOkinStageLists:
    """The stages the controller declares, and the cues each waits on."""

    def test_a_store_arms_alone_then_presses_its_slot_key(self):
        """store-cue-backstop: SET to the arm cue, then the slot key to the saved cue."""
        arm, slot = okin_store_program(2, presses_the_disarm_key=True).stages

        assert arm.controls == frozenset({Control("store-preset-2")})
        assert arm.cue == CueRequest(1)
        assert arm.ceiling_ms == STORE_STAGE_CEILING_MS
        assert slot.controls == frozenset({Control("preset-2")})
        assert slot.cue == CueRequest(3)
        assert slot.ceiling_ms == STORE_STAGE_CEILING_MS

    def test_a_store_that_ends_without_its_cue_presses_dummy(self):
        """dummy-disarms-a-pending-store: the recovery track is the disarming press."""
        (recovery,) = okin_store_program(1, presses_the_disarm_key=True).recovery

        assert isinstance(recovery, PressStage)
        assert recovery.controls == frozenset({Control("preset-dummy")})

    def test_a_store_owes_no_press_of_a_key_its_profile_lacks(self):
        """dummy-disarms-a-pending-store: the key is Prodigy CE's, and so is the recovery."""
        assert okin_store_program(1, presses_the_disarm_key=False).recovery == ()

    def test_each_stage_carries_its_bits_alone(self):
        """operations-express-alone: one stage, one control, no chord."""
        program = okin_store_program(4, presses_the_disarm_key=True)
        stages = program.stages + program.recovery + okin_mode_program(FACTORY_RESET).stages
        for stage in stages:
            assert len(stage.controls) == 1

    @pytest.mark.parametrize(
        ("control", "pulses"),
        [(FACTORY_RESET, 2), (LATCH_MODE_ENABLE, 4)],
    )
    def test_a_mode_chord_runs_one_stage_to_its_own_pulse_count(
        self, control: Control, pulses: int
    ):
        """cue-or-ceiling: the box answers each gesture with its own pulse count."""
        program = okin_mode_program(control)
        (stage,) = program.stages

        assert isinstance(stage, CuedStage)
        assert stage.controls == frozenset({control})
        assert stage.cue == CueRequest(pulses)
        assert stage.ceiling_ms == MODE_STAGE_CEILING_MS
        assert program.recovery == ()

    def test_the_disarming_press_waits_on_no_cue(self):
        """cue-or-ceiling: a press stage ends at its press floor, so it prices none."""
        program = okin_dummy_program()
        (stage,) = program.stages

        assert isinstance(stage, PressStage)
        assert stage.controls == frozenset({Control("preset-dummy")})
        assert program.recovery == ()

    def test_the_store_stages_encode_the_arm_bit_and_then_the_slot_bit(self):
        """release-edges-end-lifecycles: two stages, two frames, one release between."""
        encoder = OkinFrameEncoder(1)
        arm, slot = okin_store_program(1, presses_the_disarm_key=True).stages

        assert encoder.encode(arm.controls) == bytes.fromhex("040200010000")
        assert encoder.encode(slot.controls) == bytes.fromhex("040200001000")

    def test_the_mode_chords_encode_the_two_captured_values(self):
        """The reset chord and the latch chord are the values the bed answered."""
        encoder = OkinFrameEncoder(1)

        assert encoder.encode(
            okin_mode_program(FACTORY_RESET).stages[0].controls
        ) == bytes.fromhex("040208010000")
        assert encoder.encode(
            okin_mode_program(LATCH_MODE_ENABLE).stages[0].controls
        ) == bytes.fromhex("040201800000")
