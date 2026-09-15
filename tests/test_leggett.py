"""Tests for Leggett & Platt bed controller."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adjustable_bed.beds.leggett_gen2 import (
    LeggettGen2Commands,
    LeggettGen2Controller,
)
from custom_components.adjustable_bed.beds.leggett_okin import (
    LeggettOkinController,
    okin_dummy_program,
    okin_mode_program,
    okin_store_program,
    parse_leggett_okin_feedback,
)
from custom_components.adjustable_bed.beds.leggett_okin_hold import LeggettOkinCommands
from custom_components.adjustable_bed.button import BUTTON_DESCRIPTIONS, _should_add_button
from custom_components.adjustable_bed.const import (
    BED_TYPE_LEGGETT_GEN2,
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
    LEGGETT_OKIN_CHAR_UUID,
    LEGGETT_OKIN_NOTIFY_CHAR_UUID,
    LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID,
    LEGGETT_OKIN_SERVICE_UUID,
    LEGGETT_VARIANT_GEN2,
    LEGGETT_VARIANT_MLRM,
    LEGGETT_VARIANT_OKIN,
    OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID,
    OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID,
    VARIANT_AUTO,
    connection_gated_by_bond,
    requires_pairing,
    requires_pairing_after_service_discovery,
)
from custom_components.adjustable_bed.coordinator import AdjustableBedCoordinator
from custom_components.adjustable_bed.hold_capability import HoldCapable
from custom_components.adjustable_bed.hold_intent import Activate, Hold, IntentAction
from custom_components.adjustable_bed.hold_operation import OperationOutcome, OperationProgram
from custom_components.adjustable_bed.hold_roster import (
    Control,
    ControlDeclarationInputs,
    ControlRoster,
)


class _FakeStreamer:
    """Records what the controller asks the streamer to do, in order."""

    def __init__(self, *, outcome: OperationOutcome = OperationOutcome.COMPLETED) -> None:
        self.calls: list[str] = []
        self.operations: list[OperationProgram] = []
        self.pushes: list[dict] = []
        self.stops: list[frozenset] = []
        self._outcome = outcome

    def hold(self, held) -> None:
        self.calls.append("hold")
        self.pushes.append(dict(held))

    def stop(self, controls) -> None:
        self.calls.append("stop")
        self.stops.append(frozenset(controls))

    def release_wire(self) -> None:
        self.calls.append("release_wire")

    def link_lost(self) -> None:
        self.calls.append("link_lost")

    def stage(self, program) -> asyncio.Future:
        self.calls.append("stage")
        self.operations.append(program)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        future.set_result(self._outcome)
        return future

    async def run_operation(self, program) -> OperationOutcome:
        self.calls.append("run_operation")
        self.operations.append(program)
        return self._outcome


def _hold_coordinator(
    *, has_massage: bool = True, app_profile: str = "prodigy4"
) -> MagicMock:
    """Return a coordinator double carrying the roster this profile declares.

    The roster is read off a controller on the same profile, the way production
    reads it: the coordinator adopts what the controller it built says its bed
    has, so a test never spells the profile's control set a second time.

    The reconstructor is a mock, so an expression-door test reads the intent the
    controller submitted rather than the frames a streamer would compose.
    """
    coordinator = MagicMock()
    coordinator.motor_pulse_count = 10
    coordinator.motor_pulse_delay_ms = 100
    declaring = LeggettOkinController(MagicMock(), app_profile=app_profile)
    coordinator.control_roster = ControlRoster(
        declaring.control_declarations(
            ControlDeclarationInputs(
                motor_pulse_count=10,
                motor_pulse_delay_ms=100,
                has_massage=has_massage,
            )
        )
    )
    coordinator.hold_reconstructor = MagicMock()
    return coordinator


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
        controller = self._controller_with_characteristics()

        # Okin format: [0x04, 0x02, ...int_bytes]
        command = controller._build_command(LeggettOkinCommands.MOTOR_HEAD_UP)

        assert len(command) == 6
        assert command[:2] == bytes([0x04, 0x02])
        # Command 0x1 in big-endian
        assert command[2:] == bytes([0x00, 0x00, 0x00, 0x01])

    @staticmethod
    def _controller_with_characteristics(*uuids: str) -> LeggettOkinController:
        coordinator = MagicMock()
        if not uuids:
            uuids = (LEGGETT_OKIN_CHAR_UUID, LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID)
        coordinator.client = SimpleNamespace(
            is_connected=True,
            services=[
                SimpleNamespace(
                    uuid=LEGGETT_OKIN_SERVICE_UUID,
                    characteristics=[SimpleNamespace(uuid=uuid) for uuid in uuids],
                )
            ],
        )
        coordinator.cancel_command = asyncio.Event()
        coordinator.address = "AA:BB:CC:DD:EE:FF"
        controller = LeggettOkinController(coordinator)
        controller._wait_hold_deadline = AsyncMock()
        return controller

    def test_revision_zero_framing_selected_when_selector_is_absent(self):
        """The write characteristic without 1721 selects the checksummed R0 frame."""
        controller = self._controller_with_characteristics(LEGGETT_OKIN_CHAR_UUID)

        assert controller._build_command(LeggettOkinCommands.MOTOR_HEAD_UP) == bytes.fromhex(
            "e5fe160000000105"
        )
        assert controller._build_command(0) == bytes.fromhex("e5fe160000000006")
        assert controller._build_command(
            LeggettOkinCommands.CONTROL_MODE_PRESS_AND_HOLD
        ) == bytes.fromhex("e5fe1608010000fd")
        assert controller.protocol_diagnostics["protocol_revision"] == 0

    def test_revision_one_framing_selected_when_selector_is_present(self):
        """Characteristic 1721 selects the established six-byte R1 frame."""
        controller = self._controller_with_characteristics(
            LEGGETT_OKIN_CHAR_UUID,
            LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID,
        )

        assert controller._build_command(LeggettOkinCommands.MOTOR_HEAD_UP) == bytes.fromhex(
            "040200000001"
        )
        assert controller.protocol_diagnostics["protocol_revision"] == 1

    async def test_massage_off_is_not_advertised(self):
        """Massage power is a toggle, so no massage-off button should be offered.

        The capability is detected by checking whether the subclass overrides
        massage_off, so overriding it just to raise NotImplementedError created a
        button that could only ever fail (issue #368).
        """
        controller = self._controller_with_characteristics()

        assert controller.supports_massage_off_control is False

    async def test_favorites_match_the_prodigy_ce_model(self):
        """Only Favorite 1/2/3 are editable; the third wire slot is fixed Snore."""
        controller = LeggettOkinController(_hold_coordinator())

        assert controller.supports_preset_zero_g is False
        assert controller.memory_slot_names == (
            "Favorite 1",
            "Favorite 2",
            "Snore",
            "Favorite 3",
        )
        assert [controller.is_memory_slot_programmable(slot) for slot in range(1, 5)] == [
            True,
            True,
            False,
            True,
        ]

        descriptions = {description.key: description for description in BUTTON_DESCRIPTIONS}
        assert not _should_add_button(descriptions["preset_zero_g"], controller, True)
        assert not _should_add_button(descriptions["program_memory_3"], controller, True)
        assert not _should_add_button(descriptions["control_mode_press_and_hold"], controller, True)
        assert _should_add_button(descriptions["control_mode_press_and_release"], controller, True)

    async def test_massage_wave_mode_is_advertised(self):
        """Expose the proven wave keycode through the shared mode-step entity."""
        controller = LeggettOkinController(_hold_coordinator())

        assert controller.supports_massage_mode_step_control is True

    async def test_write_stream_is_unconfirmed_and_wall_clock_paced(self):
        """write_command stays BedController's contract for the service door.

        The streamer writes through its own link-bound writer, but
        async_write_command still reaches this, and a confirmed write here would
        add a round trip to every frame it carries.
        """
        controller = LeggettOkinController(MagicMock())
        controller._write_gatt_with_retry = AsyncMock()
        cancel_event = MagicMock()

        await controller.write_command(
            b"command",
            repeat_count=10,
            repeat_delay_ms=100,
            cancel_event=cancel_event,
        )

        controller._write_gatt_with_retry.assert_awaited_once_with(
            controller.control_characteristic_uuid,
            b"command",
            repeat_count=10,
            repeat_delay_ms=100,
            cancel_event=cancel_event,
            response=False,
            wall_clock_pacing=True,
        )

    async def test_motor_surface_matches_cu170_actuators(self):
        """Expose head, lumbar, pillow and feet once each, without aliases."""
        controller = self._controller_with_characteristics()

        assert [spec.key for spec in controller.motor_control_specs] == [
            "head",
            "lumbar",
            "pillow",
            "feet",
        ]
        assert controller.stale_motor_entity_keys == frozenset(
            {"back", "legs", "tilt", "pillow", "lumbar"}
        )

    async def test_the_controller_is_hold_capable(self):
        """one-expression-door: the class declares the capability by inheriting it."""
        controller = LeggettOkinController(_hold_coordinator())

        assert isinstance(controller, HoldCapable)

    @pytest.mark.parametrize(
        ("method", "control", "action"),
        [
            ("move_head_up", "motor-head-up", Activate()),
            ("move_head_down", "motor-head-down", Activate()),
            ("move_legs_up", "motor-feet-up", Activate()),
            ("move_lumbar_down", "motor-lumbar-down", Activate()),
            ("move_pillow_up", "motor-pillow-up", Activate()),
            ("preset_flat", "preset-flat", Hold(223)),
            ("preset_anti_snore", "preset-3", Hold(223)),
            ("preset_dummy", "preset-dummy", Hold(223)),
            ("lights_toggle", "light-toggle", Activate()),
            ("massage_toggle", "massage-toggle", Activate()),
            ("massage_head_down", "massage-head-down", Activate()),
            ("massage_mode_step", "massage-wave-step", Activate()),
        ],
    )
    async def test_a_one_shot_submits_its_controls_fixed_duration_intent(
        self, method: str, control: str, action: IntentAction
    ):
        """one-expression-door: every motion and tap is a hold intent, unawaited.

        A control declaring an Activate is submitted as one, so the roster
        prices the press; a preset, which this bed only holds, is submitted as
        a Hold of the roster's press minimum.
        """
        coordinator = _hold_coordinator()
        controller = LeggettOkinController(coordinator)

        await getattr(controller, method)()

        coordinator.hold_reconstructor.submit.assert_called_once_with(
            Control(control), action
        )

    @pytest.mark.parametrize("slot", [1, 2, 3, 4])
    async def test_each_preset_recalls_only_its_own_slot(self, slot: int):
        """Memory 1 owns 0x1000; no other exposed preset may reach for it.

        0x1000 is the slot the vendor app also labels Zero-G, so a second button
        carrying it would be a duplicate of Memory 1 under another name.
        """
        coordinator = _hold_coordinator()
        controller = LeggettOkinController(coordinator)

        await controller.preset_memory(slot)

        coordinator.hold_reconstructor.submit.assert_called_once_with(
            Control(f"preset-{slot}"), Hold(223)
        )

    @pytest.mark.parametrize(
        ("method", "motor"),
        [
            ("move_head_stop", "head"),
            ("move_legs_stop", "feet"),
            ("move_pillow_stop", "pillow"),
            ("move_lumbar_stop", "lumbar"),
        ],
    )
    async def test_a_per_motor_stop_fences_both_of_its_directions(
        self, method: str, motor: str
    ):
        """end-is-not-stop: a per-motor stop passes the motor's two controls."""
        coordinator = _hold_coordinator()
        controller = LeggettOkinController(coordinator)

        await getattr(controller, method)()

        coordinator.hold_reconstructor.stop.assert_called_once_with(
            frozenset({Control(f"motor-{motor}-up"), Control(f"motor-{motor}-down")})
        )

    async def test_the_held_set_and_the_release_reach_the_streamer(self):
        """frame-is-the-or: the controller relays both and decides neither."""
        controller = LeggettOkinController(_hold_coordinator())
        controller._streamer = _FakeStreamer()

        controller.hold({Control("motor-head-up"): 12.0})
        controller.release_wire()

        assert controller._streamer.calls == ["hold", "release_wire"]
        assert controller._streamer.pushes == [{Control("motor-head-up"): 12.0}]

    async def test_link_up_stages_nothing(self):
        """A connect expresses nothing, so it cannot consume a travel in progress.

        A latch-mode box takes any press during an autonomous travel as that
        travel's stop, and a reconnect is not user-initiated on every path.
        """
        controller = LeggettOkinController(_hold_coordinator(app_profile="prodigy4"), app_profile="prodigy4")
        controller._streamer = _FakeStreamer()

        controller.link_up()
        controller.hold({Control("preset-1"): 12.0})

        assert controller._streamer.calls == ["hold"]
        assert controller._streamer.operations == []

    async def test_a_staged_operation_carries_its_own_cues(self):
        """store-cue-backstop: the store and the mode gestures stage, and nothing else.

        On a Prodigy 2 profile, because Prodigy CE refuses the press-and-hold
        chord before it reaches the streamer: that chord is a factory reset,
        and this bed's presets are what it erases.
        """
        controller = LeggettOkinController(_hold_coordinator(app_profile="prodigy2"), app_profile="prodigy2")
        controller._streamer = _FakeStreamer()

        await controller.program_memory(2)
        await controller.set_control_mode_press_and_hold()
        await controller.set_control_mode_press_and_release()

        assert controller._streamer.operations == [
            okin_store_program(2, presses_the_disarm_key=False),
            okin_mode_program(Control("factory-reset")),
            okin_mode_program(Control("latch-mode-enable")),
        ]

    @pytest.mark.parametrize("app_profile", ["prodigy2", "prodigy2l", "prodigy4"])
    async def test_a_store_names_only_controls_its_own_roster_prices(
        self, app_profile: str
    ):
        """dummy-disarms-a-pending-store: the streamer prices every stage it presses.

        The disarming key is declared on Prodigy CE alone, so a store that owed
        it everywhere would meet a roster that cannot price it and end the pump
        part way through the store, with the save button still waiting.
        """
        coordinator = _hold_coordinator(app_profile=app_profile)
        controller = LeggettOkinController(coordinator, app_profile=app_profile)
        controller._streamer = _FakeStreamer()

        await controller.program_memory(1)

        (program,) = controller._streamer.operations
        for stage in program.stages + program.recovery:
            for control in stage.controls:
                assert coordinator.control_roster.press_floor(control) is not None

    async def test_a_fixed_slot_stages_nothing(self):
        """operation-controls-command-path-only: slot 3 is the fixed snore entry."""
        controller = LeggettOkinController(_hold_coordinator())
        controller._streamer = _FakeStreamer()

        await controller.program_memory(3)

        assert controller._streamer.operations == []

    async def test_stop_all_releases_the_wire_and_then_presses_dummy(self):
        """latched-travel-stop, cancellation-leaves-bed-ready: the stop's own cause.

        The bed-wide stop drops every bit, then presses DUMMY - which is what
        leaves the box ready for the next preset key whatever the stop cancelled.
        """
        controller = LeggettOkinController(_hold_coordinator())
        controller._streamer = _FakeStreamer()

        await controller.stop_all()

        assert controller._streamer.calls == ["release_wire", "run_operation"]
        assert controller._streamer.operations == [okin_dummy_program()]

    async def test_a_status_notification_is_a_receipt_and_a_cue_transition(self):
        """receipts-positive-only: only the main status channel answers frames."""
        controller = LeggettOkinController(_hold_coordinator())
        controller._feedback = MagicMock()

        controller._handle_notification(
            SimpleNamespace(uuid=LEGGETT_OKIN_NOTIFY_CHAR_UUID),
            bytearray.fromhex("040055aa000000"),
        )
        controller._handle_notification(
            SimpleNamespace(uuid=OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID),
            bytearray.fromhex("040055aa000000"),
        )

        controller._feedback.note_notification.assert_called_once()
        assert controller._feedback.note_notification.call_args.args[0] == 0x55AA

    async def test_a_main_channel_frame_carrying_no_status_is_still_a_receipt(self):
        """receipts-positive-only: the channel answers frames, and status is narrower.

        On Prodigy CE the first status frame latches the mask. A later frame on
        the same channel that is not that shape is still the box answering one,
        and the light bit stays where the last status put it.
        """
        controller = LeggettOkinController(_hold_coordinator())
        controller._handle_notification(
            SimpleNamespace(uuid=LEGGETT_OKIN_NOTIFY_CHAR_UUID),
            bytearray.fromhex("090b0002000000020000ff000000000000000000"),
        )
        controller._feedback = MagicMock()

        controller._handle_notification(
            SimpleNamespace(uuid=LEGGETT_OKIN_NOTIFY_CHAR_UUID),
            bytearray.fromhex("040055aa000000"),
        )
        controller._handle_notification(
            SimpleNamespace(uuid=OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID),
            bytearray.fromhex("040055aa000000"),
        )

        controller._feedback.note_notification.assert_called_once()
        assert controller._feedback.note_notification.call_args.args[0] == 0x00020000
        assert controller._cu170_status_mask == 0x00020000

    async def test_a_missing_slot_submits_nothing(self):
        """A slot this bed does not have reaches the reconstructor as nothing."""
        coordinator = _hold_coordinator()
        controller = LeggettOkinController(coordinator)

        await controller.preset_memory(7)

        coordinator.hold_reconstructor.submit.assert_not_called()

    async def test_only_memory_1_asserts_the_memory_1_control(self):
        """Memory 1 owns 0x1000; no other exposed preset may reach for it.

        0x1000 is the slot the vendor app also labels Zero-G, so a second
        button carrying it would be a duplicate of Memory 1 under another name.
        """
        coordinator = _hold_coordinator()
        controller = LeggettOkinController(coordinator)

        presets = [
            description
            for description in BUTTON_DESCRIPTIONS
            if description.key.startswith("preset_")
            and _should_add_button(description, controller, True)
        ]
        assert {description.key for description in presets} == {
            "preset_memory_1",
            "preset_memory_2",
            "preset_memory_3",
            "preset_memory_4",
            "preset_flat",
            "preset_anti_snore",
        }

        for description in presets:
            coordinator.hold_reconstructor.submit.reset_mock()
            assert description.press_fn is not None
            await description.press_fn(controller)
            control = coordinator.hold_reconstructor.submit.call_args.args[0]
            assert (control == Control("preset-1")) is (
                description.key == "preset_memory_1"
            ), description.key

    async def test_store_keycode_is_never_a_recall(self):
        """0x10000 arms an overwrite and must not appear in the recall ladder."""
        assert LeggettOkinCommands.MEMORY_STORE == 0x10000
        assert LeggettOkinCommands.MEMORY_STORE not in {
            LeggettOkinCommands.PRESET_MEMORY_1,
            LeggettOkinCommands.PRESET_MEMORY_2,
            LeggettOkinCommands.PRESET_MEMORY_3,
            LeggettOkinCommands.PRESET_MEMORY_4,
            LeggettOkinCommands.PRESET_ANTI_SNORE,
        }

    async def test_the_control_mode_gestures_are_advertised(self):
        """The two mode chords stay reachable from the settings dialog."""
        controller = LeggettOkinController(_hold_coordinator())

        assert controller.supports_control_mode_configuration is True

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("010203", None),
            ("06000600000000", None),
            ("0200061234", (4660, -1)),
            ("04000712340000", (1810, -1)),
            ("04000812340000", (305399826, -1)),
            ("060008123456fe0000", (135410774, -2)),
            ("04000912340000", (2322, -1)),
            ("04000bffff0f0f", (3855, -1)),
            ("040055aa000000", (21930, -1)),
            ("060055aa0000fe0000", (1437204480, -2)),
        ],
    )
    def test_feedback_parser_matches_frozen_apk_vectors(
        self, payload: str, expected: tuple[int, int] | None
    ):
        """Keep all ten accepted clean-room parser vectors exact."""
        assert parse_leggett_okin_feedback(bytes.fromhex(payload)) == expected

    async def test_status_channels_are_subscribed_and_settings_initialized(self):
        """Start both APK channels and perform the optional raw 01 02 setup write."""
        controller = self._controller_with_characteristics(
            LEGGETT_OKIN_CHAR_UUID,
            LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID,
            LEGGETT_OKIN_NOTIFY_CHAR_UUID,
            OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID,
            OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID,
        )
        client = controller.client
        assert client is not None
        client.start_notify = AsyncMock()
        client.stop_notify = AsyncMock()
        client.write_gatt_char = AsyncMock()

        await controller.start_notify()

        assert client.start_notify.await_args_list == [
            call(LEGGETT_OKIN_NOTIFY_CHAR_UUID, controller._handle_notification),
            call(OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID, controller._handle_notification),
        ]
        assert client.write_gatt_char.await_args_list == [
            call(OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID, b"\x01\x02", response=True),
            *[call(LEGGETT_OKIN_CHAR_UUID, bytes.fromhex("040200000000"), response=False)] * 4,
        ]
        assert controller.protocol_diagnostics["settings_initialized"] is True

        controller._handle_notification(
            SimpleNamespace(uuid=LEGGETT_OKIN_NOTIFY_CHAR_UUID),
            bytearray.fromhex("040055aa000000"),
        )
        assert controller.protocol_diagnostics["notification_led_mask"] == "0x000055aa"
        assert controller.protocol_diagnostics["notification_status"] == -1

        await controller.stop_notify()
        assert client.stop_notify.await_count == 2

    async def test_massage_timer_step_is_not_exposed(self):
        """0x200 is a constant the app never builds or writes, so it is not a command."""
        controller = self._controller_with_characteristics()

        assert not hasattr(LeggettOkinCommands, "MASSAGE_TIMER_STEP")
        assert not hasattr(controller, "massage_timer_step")


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
