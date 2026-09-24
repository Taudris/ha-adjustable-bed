"""Leggett & Platt Okin variant bed controller implementation.

Reverse engineering by MarcusW and Richard Hopton (smartbed-mqtt).

This controller handles Leggett & Platt beds using the Okin binary protocol.

Protocol details:
    Service UUID: 62741523-52f9-8864-b1ab-3b3a8d65950b (shared with Okimat/Nectar)
    Write characteristic: 62741525-52f9-8864-b1ab-3b3a8d65950b
    Command format: runtime-selected 6-byte R1 or checksummed 8-byte R0 frame
    Motor timing: one frame every 100ms while a key is held, ended by one zero frame
    Position feedback: Not supported
    Pairing: Required before first use; handled by coordinator

The bed is hold-capable: every motion and tap here is a hold intent submitted to
the coordinator's reconstructor, and the streamer this controller builds per link
is the only writer to the command characteristic. What a frame carries lives in
leggett_okin_hold, and what the box's notifications say about the stream lives in
leggett_okin_evidence.

Note: This shares the same BLE service UUID with Okimat and Nectar beds.
Detection uses device name patterns ("leggett", "l&p", "lp bed") to distinguish
between these bed types. See okin_protocol.py for the shared binary protocol
specification.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    DEVICE_INFO_READ_TIMEOUT,
    LEGGETT_OKIN_CHAR_UUID,
    LEGGETT_OKIN_NOTIFY_CHAR_UUID,
    LEGGETT_OKIN_PULSE_DEFAULTS,
    LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID,
    LEGGETT_OKIN_SERVICE_UUID,
    OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID,
    OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID,
)
from ..hold_capability import HoldCapable
from ..hold_intent import Activate, Deadline, Hold, IntentAction
from ..hold_operation import OperationOutcome
from ..hold_roster import (
    ActionKind,
    Control,
    ControlDeclaration,
    ControlDeclarationInputs,
    MotorControls,
    PressFloor,
    preset_control_name,
)
from ..hold_streamer import HoldStreamer
from ..leggett_app_protocol import (
    LEGGETT_APP_PROFILES,
    SLEEP_CANCEL_KEY,
    USERIES_SLEEP_MINUTES,
    AppProfile,
    build_alarm,
    build_sleep_timer,
)
from .base import (
    BedController,
    ControllerStateBinarySensorSpec,
    ControllerStateSensorSpec,
    MotorControlSpec,
)
from .leggett_okin_evidence import DEFAULT_DEFICIT_TRIP, OkinStreamFeedback
from .leggett_okin_hold import (
    CU170_STREAM_PROFILE,
    FACTORY_RESET,
    LATCH_MODE_ENABLE,
    LIGHT_TOGGLE,
    MASSAGE_FOOT_DOWN,
    MASSAGE_FOOT_UP,
    MASSAGE_HEAD_DOWN,
    MASSAGE_HEAD_UP,
    MASSAGE_TOGGLE,
    MASSAGE_WAVE_STEP,
    PRESET_ANTI_SNORE,
    PRESET_DUMMY,
    PRESET_FLAT,
    PROGRAMMABLE_MEMORY_SLOTS,
    LeggettOkinCommands,
    OkinFrameEncoder,
    OkinFrameWriter,
    OkinProfile,
    build_frame,
    control_declarations,
    okin_dummy_program,
    okin_mode_program,
    okin_store_program,
)

if TYPE_CHECKING:
    from ..coordinator import AdjustableBedCoordinator

_LOGGER = logging.getLogger(__name__)

# The keycode table lives in leggett_okin_hold with the frame composition that
# reads it; it is re-exported here because the app-protocol paths below still
# name keycodes directly.
__all__ = ["LeggettOkinCommands", "LeggettOkinController", "control_declarations"]

# The app streams a held keycode until release, then emits exactly four
# keycode-0 frames (OutputThread.runNormal, MaxZeroCount = 3). There is no
# distinct stop opcode: the release frame is an ordinary frame carrying 0. The
# streamed set releases with one confirmed frame instead; these two constants
# serve the bounded app-button paths that still stream through write_command.
# There Prodigy CE / CU170 also sends that frame once as a Write Request, so
# these counts are what the other profiles release with.
RELEASE_FRAME_COUNT = 4
# Same 100ms as the recall cadence today, but deliberately a separate constant:
# these are independent findings about different command families, and retuning
# one must not silently retune the other.
RELEASE_FRAME_DELAY_MS = 100
# Prodigy CE / CU170 only: a liveness bound on that confirmed release, so a
# Write Request the box accepts but never answers cannot hold the command lock
# for the rest of the connection. The owner's proxy measured a confirmed
# write's completion at p50 128-174 ms and p99 259-412 ms (2026-08), so five
# seconds sits an order of magnitude clear of the slowest round trip observed.
# It matches DEVICE_INFO_READ_TIMEOUT, the bound this integration already puts
# on a GATT operation this hardware can accept and silently drop, and stays a
# separate constant because the two answer different questions.
RELEASE_WRITE_TIMEOUT_S = 5.0

# The connect-time status query. Keycode 0 toggles nothing, so the frame only
# asks the box for its live status, and one frame draws one receipt. The notify
# channel loses 2.5-4.5% of receipts (2026-08) and this query's gate fires once
# per connection, so the other three frames are its retry. Same 100ms as the
# release cadence, and again deliberately separate constants.
STATUS_QUERY_ZERO_FRAME_COUNT = 4
STATUS_QUERY_ZERO_FRAME_DELAY_MS = 100

# A memory recall is a fixed 10-frame burst with no terminator at all. The
# control box drives the move to completion by itself, so appending a release
# frame here could cancel the motion the recall just started.
RECALL_FRAME_COUNT = 10
RECALL_FRAME_DELAY_MS = 100

# CU170 hardware observations in issue #368, distinct from the app's UI masks.
CU170_LIGHT_MASK = 0x00020000
CU170_ALARM_MASK = 0x00400000
CU170_SLEEP_MASK = 0x00800000
# Integration response timeout, not a firmware timing requirement.
LIGHT_STATE_TIMEOUT_S = 3.0

# Prodigy CE / CU170 only: how long a tap holds the key once the box has the
# press. The owner's 2026-09-06 sweep put the under-bed light's
# press-to-release floor at 150 ms, with 101 of 101 taps registering at 150 ms
# or more and the last failure at 114 ms, timing the gap at the writer. This
# hold runs from the box's receipt of the press instead, so the gap the box
# sees is this hold plus the receipt's return trip plus the release's delivery,
# above the measured floor by construction.
CU170_PRESS_HOLD_S = 0.15
# How long a tap holds the key before releasing without having seen a receipt.
# The notify channel drops 2.5-4.5% of receipts under a stream (2026-08), and by
# this point the box has made its own release edge from the silence.
CU170_PRESS_RECEIPT_BACKSTOP_S = 0.5


def _cu170_status_mask_of(payload: bytes) -> int | None:
    """Return the CU170 status mask a notification carries, or None for any other.

    The shape is fixed: twenty bytes opening 09 0b, the mask written twice and
    an FF between the copies. A frame that fails it carries no status, which is
    a narrower fact than carrying nothing - the channel it arrived on still
    answered a frame.
    """
    if (
        payload[:2] != b"\x09\x0b"
        or len(payload) != 20
        or payload[10] != 0xFF
        or payload[2:6] != payload[6:10]
    ):
        return None
    return int.from_bytes(payload[2:6], "big")


def _build_revision_0_command(command_value: int) -> bytes:
    """Return the app's revision-0 E5 FE 16 command frame for one keycode.

    The framing itself lives with the keycode table in leggett_okin_hold, which
    composes a frame for whichever revision a link resolved; this names the
    revision-0 form on its own, for the app-protocol vectors that pin it.
    """
    return build_frame(command_value, 0)


def parse_leggett_okin_feedback(data: bytes) -> tuple[int, int] | None:
    """Reconstruct the Prodigy CE LED/status pair from a notification."""
    if len(data) < 4:
        return None

    size = data[0] & 0x0F
    if len(data) < size + 3:
        return None

    led_mask = 0
    for index in range(min(max(size - 2, 0), 4)):
        led_mask = (led_mask << 8) | data[index + 2]

    status = data[6] if size > 5 else -1
    if status >= 0x80:
        status -= 0x100

    def read_unsigned(offset: int, count: int) -> int:
        value = 0
        for index in range(count):
            value = (value << 8) | data[offset + index]
        return value

    opcode = data[2]
    if opcode == 6:
        led_mask |= read_unsigned(3, size)
    elif opcode in (7, 9):
        led_mask &= ~read_unsigned(3, size)
    elif opcode == 8 and size != 6:
        led_mask ^= read_unsigned(3, size)
    elif opcode == 11:
        half = size >> 1
        offset = 3 + (size & 1)
        led_mask = (read_unsigned(offset + half, half) & read_unsigned(offset, half) & ~0x200) | (
            led_mask & 0x200
        )

    return led_mask & 0xFFFFFFFF, status


class LeggettOkinController(BedController, HoldCapable):
    """Controller for Leggett & Platt beds using Okin protocol.

    These beds use the binary Okin protocol and require BLE pairing.
    They support motor control, presets, massage, and under-bed lighting.

    Hold-capable: every motion and tap is a hold intent, and the streamer built
    here expresses the whole held set as one frame per wake.
    """

    # CU170 hardware accepts unconfirmed writes. Waiting for a response and then
    # sleeping the configured interval pushes the stream beyond its 217-218 ms
    # motion watchdog on ESPHome proxies.
    _write_with_response = False

    def __init__(
        self, coordinator: AdjustableBedCoordinator, app_profile: str = "prodigy4"
    ) -> None:
        """Initialize the Leggett & Platt Okin controller."""
        super().__init__(coordinator)
        if app_profile not in LEGGETT_APP_PROFILES:
            raise ValueError(f"Unknown Leggett app profile: {app_profile}")
        self._app_profile = cast(AppProfile, app_profile)
        self._profile = LEGGETT_APP_PROFILES[self._app_profile]
        self._device_information: dict[str, str] = {}
        self._device_information_read: set[str] = set()
        self._protocol_revision = self._detect_protocol_revision()
        self._notification_led_mask: int | None = None
        self._notification_status: int | None = None
        self._cu170_status_mask: int | None = None
        self._light_is_on: bool | None = None
        self._light_state_changed = asyncio.Event()
        # Every CU170 status frame, which the box sends for every frame it
        # receives. A tap clears this before its press write and waits on it for
        # the box's receipt of that press; the light logic waits on
        # _light_state_changed for a later, different frame on the same channel.
        self._status_frame_received = asyncio.Event()
        self._notify_started: set[str] = set()
        self._notifications_stopped = False
        self._settings_initialized = False
        client = self.client
        if client is None:
            raise ConnectionError("A hold-capable controller is built on a connected link")
        self._feedback = OkinStreamFeedback(coordinator.address, self._read_deficit_trip)
        self._streamer = HoldStreamer(
            name=coordinator.address,
            press_floor=self._press_floor,
            encoder=OkinFrameEncoder(self._protocol_revision),
            writer=OkinFrameWriter(
                client=client,
                ble_lock=self._ble_lock,
                characteristic_uuid=self.control_characteristic_uuid,
            ),
            profile=CU170_STREAM_PROFILE,
            clock=coordinator.hass.loop.time,
            feedback=self._feedback,
            on_sick=self._on_stream_sick,
            on_lifecycle_open=self._record_stream_trace,
        )
        _LOGGER.debug(
            "LeggettOkinController initialized (protocol revision: %s)",
            self._protocol_revision if self._protocol_revision is not None else "unknown",
        )

    def control_declarations(
        self, inputs: ControlDeclarationInputs
    ) -> tuple[ControlDeclaration, ...]:
        """Return the controls this bed's app profile carries."""
        chords = {
            control
            for control, supported in (
                (FACTORY_RESET, self.supports_control_mode_press_and_hold),
                (LATCH_MODE_ENABLE, self.supports_control_mode_configuration),
            )
            if supported
        }
        return control_declarations(
            inputs,
            OkinProfile(
                motors=frozenset(spec.key for spec in self.motor_control_specs),
                memory_slots=self.memory_slot_count,
                programs_memory=self.supports_memory_programming,
                mode_chords=frozenset(chords),
                presses_the_disarm_key=self.presses_the_disarm_key,
            ),
        )

    @property
    def presses_the_disarm_key(self) -> bool:
        """Return whether this profile presses the key with no function of its own.

        Prodigy CE alone, because that is the profile the key is
        hardware-confirmed on. Every other profile leaves it undeclared and
        stops at the release, rather than pressing a key no hardware answered
        for.
        """
        return self._app_profile == "prodigy4"

    def link_up(self) -> None:
        """Take up nothing: this bed owes its box no connect-time gesture.

        A press at link-up would have to assume the box is idle, and nothing
        the integration can read says whether it is. A latch-mode box takes any
        press during an autonomous travel as that travel's stop, and a reconnect
        is not user-initiated on every path, so a press here can cut short a
        travel the physical remote started. The disarming press the box does
        need keeps its own occasions: after a SET stage that ended without the
        saved cue, and at the bed-wide stop.
        """

    def hold(self, held: Mapping[Control, Deadline]) -> None:
        """Replace the expressed set with held, each control mapped to its deadline."""
        self._streamer.hold(held)

    def stop(self, controls: frozenset[Control]) -> None:
        """Drop these controls' presses now, their press floors met or not."""
        self._streamer.stop(controls)

    def release_wire(self) -> None:
        """Drop every expressed bit and submit the zero frame that ends the lifecycle."""
        self._streamer.release_wire()

    def link_lost(self) -> None:
        """End the stream and any staged operation, writing nothing."""
        self._streamer.link_lost()

    def _read_deficit_trip(self) -> int:
        """Return the deficit trip one wire lifecycle runs under.

        The entry exposes no such option today, so this is the guard's own
        default; the read still happens per lifecycle, which is where a future
        option has to land.
        """
        return DEFAULT_DEFICIT_TRIP

    def _record_stream_trace(self, frame: bytes) -> None:
        """File one command-trace entry for a wire lifecycle's first frame.

        One entry per lifecycle rather than per frame: at ten frames a second
        the per-frame form empties the coordinator's hundred-entry trace deque
        in ten seconds.
        """
        self._coordinator.record_command_trace(
            payload=self._format_command_trace_payload(frame) or {},
            characteristic_uuid=self.control_characteristic_uuid,
            characteristic_handle=None,
            response=False,
            repeat_count=1,
            repeat_delay_ms=CU170_STREAM_PROFILE.frame_interval_ms,
            command_origin="hold_stream",
            controller_class=type(self).__name__,
        )

    def _on_stream_sick(self) -> None:
        """Order the disconnect a failed barrier write calls for.

        The streamer has already released the wire; only this class owns the
        link, so ending it is here.
        """
        _LOGGER.warning(
            "Leggett Okin at %s failed a confirmed stream write; disconnecting",
            self._coordinator.address,
        )
        self._coordinator.hass.async_create_task(self._disconnect_sick_link())

    async def _disconnect_sick_link(self) -> None:
        """Disconnect, and warn once when the link outlives the attempt.

        Nothing retries: the streamer stays fenced for the rest of the link, so
        a link the disconnect did not end sends no stream frame until it ends
        some other way.
        """
        try:
            ended = await self._coordinator.async_disconnect()
        except Exception as err:  # noqa: BLE001 - a raise leaves the link as a False does
            _LOGGER.debug("Disconnecting %s raised: %s", self._coordinator.address, err)
            ended = False
        if not ended:
            _LOGGER.warning(
                "Leggett Okin at %s did not disconnect after a failed confirmed stream "
                "write; its hold stream stays stopped until the link ends",
                self._coordinator.address,
            )

    def _submit(self, control: Control) -> None:
        """Submit one press of the control as an intent, and return.

        One expression door: a tap never waits for the bed and never cancels an
        operation the streamer is staging. A control that declares an Activate
        is pressed for the duration the declaration prices; a preset, which
        this bed only holds, is pressed for the roster's press minimum.
        """
        roster = self._coordinator.control_roster
        action: IntentAction = (
            Activate()
            if roster.supports(control, ActionKind.ACTIVATE)
            else Hold(roster.minimum_press_ms(control))
        )
        self._coordinator.hold_reconstructor.submit(control, action)

    def _press_floor(self, control: Control) -> PressFloor:
        """Return one control's press floor, as the entry's roster declares it.

        Read per lookup rather than captured, because a connect-time protocol
        correction can replace the roster under a live controller.
        """
        return self._coordinator.control_roster.press_floor(control)

    def _motor(self, motor: str) -> MotorControls:
        """Return one of this bed's motors, which its own module declares.

        The app surface answers first, because the roster declares all four
        actuators for every Okin bed while a profile can carry fewer.

        Raises:
            NotImplementedError: Thrown when the selected app profile has no
                such actuator.
            KeyError: Thrown when the roster declares no such motor, which
                means the declarations disagree with this class.
        """
        if motor == "pillow" and not self.has_pillow_support:
            raise NotImplementedError("This Leggett app profile has no pillow control")
        if motor == "lumbar" and not self.has_lumbar_support:
            raise NotImplementedError("This Leggett app profile has no lumbar control")
        controls = self._coordinator.control_roster.motor(motor)
        if controls is None:
            raise KeyError(f"This bed declares no motor '{motor}'")
        return controls

    def _stop_motor(self, motor: str) -> None:
        """Fence both directions of one motor at the reconstructor."""
        self._coordinator.hold_reconstructor.stop(self._motor(motor).both)

    @property
    def control_characteristic_uuid(self) -> str:
        """Return the UUID of the control characteristic."""
        return LEGGETT_OKIN_CHAR_UUID

    # Capability properties
    @property
    def supports_preset_anti_snore(self) -> bool:
        """Expose one-shot Snore only for profiles with an autonomous recall."""
        return self._app_profile != "useries"

    @property
    def supports_lights(self) -> bool:
        """Return True - Okin beds support under-bed lighting."""
        return True

    @property
    def supports_light_state_feedback(self) -> bool:
        """Only the Prodigy CE hardware has a verified light-state mapping."""
        return self._app_profile == "prodigy4"

    @property
    def supports_discrete_light_control(self) -> bool:
        """Return False - Okin only supports toggle, not discrete on/off."""
        return False

    @property
    def supports_memory_presets(self) -> bool:
        """U-Series memories require an explicit held-control duration."""
        return self._app_profile != "useries"

    @property
    def memory_slot_count(self) -> int:
        """Return only the memories directly reachable in the selected app."""
        return self._profile.memory_slots

    @property
    def supports_memory_programming(self) -> bool:
        """Only the Prodigy apps prove the automated two-stage store sequence."""
        return self._app_profile != "useries"

    @property
    def memory_slot_names(self) -> tuple[str | None, ...]:
        """Return the three editable favorites and fixed Snore entry."""
        if self._app_profile == "useries":
            return ("Memory 1", "Memory 2")
        return ("Favorite 1", "Favorite 2", "Snore", "Favorite 3")

    def is_memory_slot_programmable(self, memory_num: int) -> bool:
        """Return False for the APK's fixed Snore entry in slot 3."""
        return self.supports_memory_programming and memory_num in PROGRAMMABLE_MEMORY_SLOTS

    @property
    def app_profile(self) -> str:
        """Return the explicitly selected app generation."""
        return self._app_profile

    @property
    def supports_massage(self) -> bool:
        return True

    @property
    def supports_control_mode_configuration(self) -> bool:
        return self._profile.settings

    @property
    def supports_control_mode_press_and_hold(self) -> bool:
        """CU170 hardware treats the app's SET+FLAT chord as a factory reset."""
        return self._profile.settings and self._app_profile != "prodigy4"

    @property
    def supports_sleep_timer(self) -> bool:
        return True

    @property
    def supports_alarm_timer(self) -> bool:
        return True

    @property
    def sleep_timer_memory_options(self) -> tuple[int, ...]:
        # U-Series has a timer-only third memory and a separate Flat action.
        return (0, 1, 2, 3) if self._app_profile == "useries" else (1, 2, 3, 4)

    @property
    def sleep_timer_duration_options(self) -> tuple[int, ...]:
        return USERIES_SLEEP_MINUTES if self._app_profile == "useries" else ()

    @property
    def supports_held_control(self) -> bool:
        return True

    _HELD_CONTROLS = {
        "flat": LeggettOkinCommands.PRESET_FLAT,
        "snore": LeggettOkinCommands.PRESET_ANTI_SNORE,
        "lights_toggle": LeggettOkinCommands.TOGGLE_LIGHTS,
        "massage_toggle": LeggettOkinCommands.MASSAGE_STEP,
        "massage_wave": LeggettOkinCommands.MASSAGE_WAVE_STEP,
        "massage_head_up": LeggettOkinCommands.MASSAGE_HEAD_UP,
        "massage_head_down": LeggettOkinCommands.MASSAGE_HEAD_DOWN,
        "massage_foot_up": LeggettOkinCommands.MASSAGE_FOOT_UP,
        "massage_foot_down": LeggettOkinCommands.MASSAGE_FOOT_DOWN,
    }
    _USERIES_HELD_CONTROLS = {
        "memory_1": LeggettOkinCommands.PRESET_MEMORY_1,
        "memory_2": LeggettOkinCommands.PRESET_MEMORY_2,
        "store": LeggettOkinCommands.MEMORY_STORE,
    }

    @property
    def held_control_options(self) -> tuple[str, ...]:
        options = tuple(self._HELD_CONTROLS)
        if self._app_profile == "useries":
            options += tuple(self._USERIES_HELD_CONTROLS)
        return options

    async def hold_control(self, control: str, duration_ms: int) -> None:
        """Reproduce a bounded app button hold with its ordinary zero release."""
        if control not in self.held_control_options:
            raise ValueError(f"Unsupported held control for {self._app_profile}: {control}")
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not 100 <= duration_ms <= 60000
        ):
            raise ValueError("Held duration must be an integer from 100 to 60000 ms")
        command = self._HELD_CONTROLS.get(control)
        if command is None:
            command = self._USERIES_HELD_CONTROLS[control]
        completed = False
        deadline = asyncio.get_running_loop().time() + duration_ms / 1000
        try:
            await self.write_command(
                self._build_command(command),
                repeat_count=(duration_ms + 99) // 100,
                repeat_delay_ms=100,
                deadline=deadline,
            )
            await self._wait_hold_deadline(deadline)
            completed = True
        finally:
            await self._send_release_frames(control, raise_on_error=completed)

    async def set_sleep_timer(self, minutes: int, memory_num: int = 1) -> None:
        """Schedule the profile's proven sleep action; successful timers have no zero tail."""
        command = build_sleep_timer(self._app_profile, minutes, memory_num)
        await self._send_special(
            self._build_command(command) if isinstance(command, int) else command
        )

    async def cancel_sleep_timer(self) -> None:
        await self._recall(SLEEP_CANCEL_KEY)

    async def set_alarm_timer(self, minutes: int) -> None:
        await self._recall(build_alarm(self._app_profile, minutes))

    async def cancel_alarm_timer(self) -> None:
        await self._recall(build_alarm(self._app_profile, None))

    @property
    def controller_state_sensor_specs(self) -> tuple[ControllerStateSensorSpec, ...]:
        """Publish opaque feedback without assigning unsupported hardware semantics."""
        return (
            ControllerStateSensorSpec(
                key="leggett_led_mask",
                translation_key="leggett_led_mask",
                state_key="leggett_led_mask",
                icon="mdi:led-outline",
            ),
            ControllerStateSensorSpec(
                key="leggett_status",
                translation_key="leggett_status",
                state_key="leggett_status",
                icon="mdi:information-outline",
            ),
        )

    @property
    def controller_state_binary_sensor_specs(self) -> tuple[ControllerStateBinarySensorSpec, ...]:
        """Expose the app's alarm/timer indicators, not reconstructed timer settings."""
        return (
            ControllerStateBinarySensorSpec(
                key="leggett_alarm_indicator",
                translation_key="leggett_alarm_indicator",
                state_key="leggett_alarm_indicator",
                icon="mdi:alarm",
            ),
            ControllerStateBinarySensorSpec(
                key="leggett_sleep_timer_indicator",
                translation_key="leggett_sleep_timer_indicator",
                state_key="leggett_sleep_timer_indicator",
                icon="mdi:timer-outline",
            ),
        )

    @property
    def _display_led_mask(self) -> int | None:
        mask = self._notification_led_mask
        # U-Series deliberately hides every indicator when any low byte bit is set.
        if self._app_profile == "useries" and mask is not None and mask & 0xFF:
            return 0
        return mask

    @property
    def requires_notification_channel(self) -> bool:
        """Subscribe to the app's status channels even without position sensing."""
        return True

    @property
    def protocol_diagnostics(self) -> dict[str, Any]:
        """Report resolved framing and the APK-parsed opaque status mask."""
        led_mask = self._notification_led_mask
        display_mask = self._display_led_mask
        return {
            "app_profile": self._app_profile,
            "device_information": dict(self._device_information),
            "protocol_revision": self._protocol_revision,
            "revision_selector_present": (
                self._protocol_revision == 1 if self._protocol_revision is not None else None
            ),
            "notification_led_mask": f"0x{led_mask:08x}" if led_mask is not None else None,
            "notification_status": self._notification_status,
            "alarm_armed": (
                bool(self._cu170_status_mask & CU170_ALARM_MASK)
                if self._cu170_status_mask is not None
                else bool(display_mask & 0x4000) if display_mask is not None else None
            ),
            "sleep_timer_armed": (
                bool(self._cu170_status_mask & CU170_SLEEP_MASK)
                if self._cu170_status_mask is not None
                else bool(display_mask & 0x8000) if display_mask is not None else None
            ),
            "under_bed_lights_on": self._light_is_on,
            "notification_characteristics": sorted(self._notify_started),
            "settings_initialized": self._settings_initialized,
            "hold_stream": {**self._streamer.diagnostics, **self._feedback.diagnostics},
        }

    @property
    def has_pillow_support(self) -> bool:
        """Return True - the third actuator moves the pillow platform."""
        return self._profile.pillow

    @property
    def has_lumbar_support(self) -> bool:
        """Return True - Okin beds have lumbar motor control."""
        return self._profile.lumbar

    @property
    def motor_control_specs(self) -> tuple[MotorControlSpec, ...]:
        """Expose only the actuators present in the selected app surface."""
        specs = (
            MotorControlSpec(
                key="head",
                translation_key="head",
                open_fn=lambda ctrl: ctrl.move_head_up(),
                close_fn=lambda ctrl: ctrl.move_head_down(),
                stop_fn=lambda ctrl: ctrl.move_head_stop(),
            ),
            MotorControlSpec(
                key="lumbar",
                translation_key="lumbar",
                open_fn=lambda ctrl: ctrl.move_lumbar_up(),
                close_fn=lambda ctrl: ctrl.move_lumbar_down(),
                stop_fn=lambda ctrl: ctrl.move_lumbar_stop(),
            ),
            MotorControlSpec(
                key="pillow",
                translation_key="pillow",
                open_fn=lambda ctrl: ctrl.move_pillow_up(),
                close_fn=lambda ctrl: ctrl.move_pillow_down(),
                stop_fn=lambda ctrl: ctrl.move_pillow_stop(),
            ),
            MotorControlSpec(
                key="feet",
                translation_key="feet",
                open_fn=lambda ctrl: ctrl.move_feet_up(),
                close_fn=lambda ctrl: ctrl.move_feet_down(),
                stop_fn=lambda ctrl: ctrl.move_feet_stop(),
            ),
        )
        return tuple(
            spec
            for spec in specs
            if (spec.key != "pillow" or self.has_pillow_support)
            and (spec.key != "lumbar" or self.has_lumbar_support)
        )

    @property
    def stale_motor_entity_keys(self) -> frozenset[str]:
        """Remove duplicate aliases and the former tilt label."""
        return frozenset({"back", "legs", "tilt", "pillow", "lumbar"})

    def _available_characteristic_uuids(self) -> frozenset[str] | None:
        """Return discovered characteristic UUIDs, or None before discovery."""
        client = self.client
        services = getattr(client, "services", None) if client is not None else None
        if services is None:
            return None

        try:
            return frozenset(
                str(characteristic.uuid).lower()
                for service in services
                for characteristic in getattr(service, "characteristics", ())
            )
        except TypeError:
            return None

    def _detect_protocol_revision(self) -> int | None:
        """Select R1 only when the APK's selector characteristic is present."""
        client = self.client
        services = getattr(client, "services", None) if client is not None else None
        if services is None:
            return None

        try:
            service = next(
                (
                    service
                    for service in services
                    if str(getattr(service, "uuid", "")).lower()
                    == LEGGETT_OKIN_SERVICE_UUID.lower()
                ),
                None,
            )
        except TypeError:
            return None
        if service is None:
            return None

        characteristic_uuids = {
            str(characteristic.uuid).lower()
            for characteristic in getattr(service, "characteristics", ())
        }
        if LEGGETT_OKIN_CHAR_UUID.lower() not in characteristic_uuids:
            return None
        return int(LEGGETT_OKIN_REVISION_SELECTOR_CHAR_UUID.lower() in characteristic_uuids)

    def _build_command(self, command_value: int) -> bytes:
        """Build the runtime-selected revision-0 or revision-1 command."""
        if self._protocol_revision is None:
            self._protocol_revision = self._detect_protocol_revision()
        if self._protocol_revision is None:
            raise ConnectionError("Leggett key characteristic is not resolved")
        return build_frame(command_value, self._protocol_revision)

    async def write_command(
        self,
        command: bytes,
        repeat_count: int = 1,
        repeat_delay_ms: int = 100,
        cancel_event: asyncio.Event | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        """Write an unconfirmed stream; duration-bound holds stop starting late writes."""
        if deadline is not None:
            loop = asyncio.get_running_loop()
            cancel = cancel_event or self._coordinator.cancel_command
            for _ in range(repeat_count):
                started = loop.time()
                if started >= deadline or cancel.is_set():
                    break
                await self._write_gatt_with_retry(
                    self.control_characteristic_uuid,
                    command,
                    cancel_event=cancel,
                    response=False,
                )
                await self._wait_hold_deadline(min(started + repeat_delay_ms / 1000, deadline))
            return
        await self._write_gatt_with_retry(
            self.control_characteristic_uuid,
            command,
            repeat_count=repeat_count,
            repeat_delay_ms=repeat_delay_ms,
            cancel_event=cancel_event,
            response=False,
            wall_clock_pacing=True,
        )

    async def start_notify(self, callback: Callable[[str, float], None] | None = None) -> None:
        """Subscribe to the status channels recovered from Prodigy CE."""
        self._notify_callback = callback
        client = self.client
        if client is None or not client.is_connected:
            _LOGGER.warning("Cannot start Leggett Okin notifications: not connected")
            return

        self._protocol_revision = self._detect_protocol_revision()
        self._notifications_stopped = False
        characteristic_uuids = self._available_characteristic_uuids() or frozenset()
        candidates = (LEGGETT_OKIN_NOTIFY_CHAR_UUID,)
        if self._profile.settings:
            candidates += (OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID,)
        settings_started_now = False
        for characteristic_uuid in candidates:
            normalized_uuid = characteristic_uuid.lower()
            if (
                normalized_uuid not in characteristic_uuids
                or normalized_uuid in self._notify_started
            ):
                continue
            try:
                async with self._ble_lock:
                    await client.start_notify(characteristic_uuid, self._handle_notification)
                self._notify_started.add(normalized_uuid)
                settings_started_now |= (
                    normalized_uuid == OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID.lower()
                )
            except BleakError as err:
                _LOGGER.debug(
                    "Could not start Leggett Okin notifications on %s: %s",
                    characteristic_uuid,
                    err,
                )

        if (
            (
                settings_started_now
                or OKIN_SMART_REMOTE_CSS_NOTIFY_CHAR_UUID.lower() in self._notify_started
            )
            and not self._settings_initialized
            and not self._coordinator.cancel_command.is_set()
            and OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID.lower() in characteristic_uuids
        ):
            try:
                await self._write_gatt_with_retry(
                    OKIN_SMART_REMOTE_CSS_WRITE_CHAR_UUID,
                    b"\x01\x02",
                    response=True,
                    log_errors=False,
                )
                self._settings_initialized = not self._coordinator.cancel_command.is_set()
            except (BleakError, ConnectionError) as err:
                _LOGGER.debug("Could not initialize Leggett Okin settings: %s", err)

        if (
            self.supports_light_state_feedback
            and self._light_is_on is None
            and LEGGETT_OKIN_NOTIFY_CHAR_UUID.lower() in self._notify_started
            and not self._coordinator.cancel_command.is_set()
        ):
            # The app starts with idle frames. CU170 replies with live status,
            # so subscribe first and use zero, which does not toggle the light.
            await self.write_command(
                self._build_command(0),
                repeat_count=STATUS_QUERY_ZERO_FRAME_COUNT,
                repeat_delay_ms=STATUS_QUERY_ZERO_FRAME_DELAY_MS,
            )

        await self._read_device_information(characteristic_uuids)

    async def _read_device_information(self, available: frozenset[str]) -> None:
        """Read the six standard fields serially; values never select capabilities."""
        fields = {
            "2a29": "manufacturer",
            "2a24": "model",
            "2a25": "serial",
            "2a27": "hardware",
            "2a26": "firmware",
            "2a28": "software",
        }
        client = self.client
        if client is None:
            return
        for short_uuid, name in fields.items():
            if self._coordinator.cancel_command.is_set():
                break
            uuid = f"0000{short_uuid}-0000-1000-8000-00805f9b34fb"
            if uuid not in available or uuid in self._device_information_read:
                continue
            try:
                async with self._ble_lock:
                    value = await asyncio.wait_for(
                        client.read_gatt_char(uuid), DEVICE_INFO_READ_TIMEOUT
                    )
                self._device_information[name] = (
                    bytes(value).decode("utf-8", errors="replace").rstrip("\0")
                )
                self._device_information_read.add(uuid)
            except (BleakError, ConnectionError, TimeoutError) as err:
                _LOGGER.debug("Could not read Leggett %s information: %s", name, err)

    def _handle_notification(self, sender: object, data: bytearray) -> None:
        """Forward raw data, retain the LED/status pair, and feed the stream's evidence.

        Only the main status characteristic answers frames, so every
        notification it carries is a receipt and the settings channel is none.
        Status is the narrower reading: a profile that publishes CU170 status
        reads the light state off the frames that carry it and counts every
        other main-channel frame as the receipt it still is, with the light bit
        left where the last status put it.
        """
        characteristic_uuid = str(getattr(sender, "uuid", sender)).lower()
        payload = bytes(data)
        self.forward_raw_notification(characteristic_uuid, payload)
        if self._notifications_stopped:
            return
        on_main_channel = characteristic_uuid == LEGGETT_OKIN_NOTIFY_CHAR_UUID.lower()
        status_mask = (
            _cu170_status_mask_of(payload)
            if self.supports_light_state_feedback and on_main_channel
            else None
        )
        if status_mask is not None:
            # Receipts contain the pre-command state; subsequent spontaneous
            # notifications contain the new state. Process both, without
            # consuming one notification as an acknowledgement of one write.
            self._cu170_status_mask = status_mask
            self._light_is_on = bool(status_mask & CU170_LIGHT_MASK)
            self._notification_led_mask = status_mask
            self._notification_status = None
            self._light_state_changed.set()
            self._status_frame_received.set()
            self.forward_controller_state_updates(
                {
                    "leggett_led_mask": status_mask,
                    "leggett_status": None,
                    "leggett_alarm_indicator": bool(status_mask & CU170_ALARM_MASK),
                    "leggett_sleep_timer_indicator": bool(status_mask & CU170_SLEEP_MASK),
                    "under_bed_lights_on": bool(status_mask & CU170_LIGHT_MASK),
                }
            )
            self._feedback.note_notification(
                status_mask, self._coordinator.hass.loop.time()
            )
            return
        parsed = parse_leggett_okin_feedback(payload)
        if parsed is None:
            return
        # The optional settings channel must not replace live CU170 status.
        if self._cu170_status_mask is None:
            self._notification_led_mask, self._notification_status = parsed
            display_mask = self._display_led_mask or 0
            self.forward_controller_state_updates(
                {
                    "leggett_led_mask": parsed[0],
                    "leggett_status": parsed[1],
                    "leggett_alarm_indicator": bool(display_mask & 0x4000),
                    "leggett_sleep_timer_indicator": bool(display_mask & 0x8000),
                }
            )
        if on_main_channel:
            self._feedback.note_notification(
                self._notification_led_mask or 0, self._coordinator.hass.loop.time()
            )

    async def stop_notify(self) -> None:
        """Stop subscriptions and discard feedback, including on failed shutdown."""
        self._notify_callback = None
        self._notifications_stopped = True
        self._device_information_read.clear()
        self._protocol_revision = None
        client = self.client
        try:
            if client is not None and client.is_connected:
                for characteristic_uuid in tuple(self._notify_started):
                    try:
                        async with self._ble_lock:
                            await client.stop_notify(characteristic_uuid)
                    except BleakError as err:
                        _LOGGER.debug(
                            "Could not stop Leggett Okin notifications on %s: %s",
                            characteristic_uuid,
                            err,
                        )
        finally:
            self._notify_started.clear()
            self._settings_initialized = False
            self._cu170_status_mask = None
            self._light_is_on = None
            self._notification_led_mask = None
            self._notification_status = None
            if self.supports_light_state_feedback:
                self.forward_controller_state_update("under_bed_lights_on", None)

    async def _send_release_frames(
        self,
        context: str,
        *,
        raise_on_error: bool = False,
        repeat_count: int = RELEASE_FRAME_COUNT,
    ) -> None:
        """Send the release that ends a held keycode.

        There is no distinct stop opcode: the release is an ordinary frame
        carrying keycode 0. Prodigy CE / CU170 always sends exactly one, as a
        Write Request whose completion reports non-delivery, which stop_all's
        contract promises and an unconfirmed write cannot keep; ``repeat_count``
        does not reach that path. The other profiles keep the app's four
        keycode-0 frames, and there ``repeat_count`` selects another proven
        count for a caller with a protocol-specific lifecycle. The release gets
        a fresh cancel event so a stop request cannot suppress it.

        ``raise_on_error`` is for callers where the release *is* the operation,
        so a failure must reach the user. Cleanup callers leave it False: they
        are already unwinding and have their own error to report.
        """

        async def send_release() -> None:
            if self._app_profile != "prodigy4":
                await self.write_command(
                    self._build_command(0),
                    repeat_count=repeat_count,
                    repeat_delay_ms=RELEASE_FRAME_DELAY_MS,
                    cancel_event=asyncio.Event(),
                )
                return
            async with asyncio.timeout(RELEASE_WRITE_TIMEOUT_S):
                await self._write_gatt_with_retry(
                    self.control_characteristic_uuid,
                    self._build_command(0),
                    cancel_event=asyncio.Event(),
                    response=True,
                )

        release = asyncio.ensure_future(send_release())
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            # Returning here would hand the command lock back with the release
            # still in flight: the coordinator would start the replacement
            # command while the shielded task was still writing, and that frame
            # would stop the movement it had just started. Both shapes are
            # bounded, the burst by its own length and the request by
            # RELEASE_WRITE_TIMEOUT_S, so wait it out before propagating.
            while not release.done():
                with contextlib.suppress(
                    asyncio.CancelledError, BleakError, ConnectionError, TimeoutError
                ):
                    await asyncio.shield(release)
            raise
        except BleakError, ConnectionError, TimeoutError:
            # Losing release can leave a held command active, and a request the
            # box never answers is lost in exactly that sense.
            _LOGGER.warning(
                "Failed to send the release after %s; the bed may still be moving",
                context,
                exc_info=True,
            )
            if raise_on_error:
                raise

    # Motor control methods
    async def move_head_up(self) -> None:
        """Move head up."""
        self._submit(self._motor("head").up)

    async def move_head_down(self) -> None:
        """Move head down."""
        self._submit(self._motor("head").down)

    async def move_head_stop(self) -> None:
        """Stop head motor."""
        self._stop_motor("head")

    async def move_back_up(self) -> None:
        """Move back up (same as head)."""
        await self.move_head_up()

    async def move_back_down(self) -> None:
        """Move back down (same as head)."""
        await self.move_head_down()

    async def move_back_stop(self) -> None:
        """Stop back motor."""
        await self.move_head_stop()

    async def move_legs_up(self) -> None:
        """Move legs up."""
        self._submit(self._motor("feet").up)

    async def move_legs_down(self) -> None:
        """Move legs down."""
        self._submit(self._motor("feet").down)

    async def move_legs_stop(self) -> None:
        """Stop legs motor."""
        self._stop_motor("feet")

    async def move_feet_up(self) -> None:
        """Move feet up."""
        await self.move_legs_up()

    async def move_feet_down(self) -> None:
        """Move feet down."""
        await self.move_legs_down()

    async def move_feet_stop(self) -> None:
        """Stop feet motor."""
        await self.move_legs_stop()

    async def stop_all(self) -> None:
        """Stop every motor: drop the bits, release the wire, then press DUMMY.

        No floor delays the release and nothing waits on it. The press behind it
        is what ends a travel the box latched, which dropping bits does not, and
        it clears a store the stop cancelled; in hold mode it is a key with no
        function. Prodigy CE only, because that is the profile the press is
        hardware-confirmed on; every other profile stops at the release.
        """
        self.release_wire()
        if self.presses_the_disarm_key:
            await self._streamer.run_operation(okin_dummy_program())

    # Preset methods
    async def _recall(self, command: int) -> None:
        """Send a one-shot recall burst.

        Recall is 10 frames at 100ms and then silence: the control box drives
        the move to completion on its own. This is the one command family the
        app deliberately leaves unterminated, so no release follows it - that
        could cancel the motion the recall just started.
        """
        await self._send_special(self._build_command(command))

    async def _send_special(self, packet: bytes) -> None:
        """Leave successful special bursts unterminated; cancel partial bursts safely."""
        completed = False
        try:
            await self.write_command(
                packet,
                repeat_count=RECALL_FRAME_COUNT,
                repeat_delay_ms=RECALL_FRAME_DELAY_MS,
            )
            completed = not self._coordinator.cancel_command.is_set()
        finally:
            if not completed:
                await self._send_release_frames("interrupted special command")

    async def preset_flat(self) -> None:
        """Go to flat position.

        FLAT is a held button rather than an autonomous recall, so this is a
        press like any other: a hold-mode box travels while the key is down and
        a latch-mode box latches the whole travel at the press.
        """
        self._submit(PRESET_FLAT)

    async def preset_memory(self, memory_num: int) -> None:
        """Go to memory preset."""
        if not 1 <= memory_num <= self.memory_slot_count:
            _LOGGER.warning("Invalid memory slot for recall: %d", memory_num)
            return
        if self._app_profile == "useries":
            raise NotImplementedError("Use leggett_hold_control with an explicit memory duration")
        self._submit(Control(preset_control_name(memory_num)))

    async def program_memory(self, memory_num: int) -> None:
        """Store the current position into a memory slot.

        There is no program opcode. The box arms while SET is held alone and
        answers with one light pulse; the slot key inside that window saves, and
        the box answers with three. Each stage runs to its cue or fails at its
        ceiling, and a stage that ends without the saved cue is followed by the
        disarming press on the one profile that declares that key.
        """
        if not self.is_memory_slot_programmable(memory_num):
            _LOGGER.warning("Memory slot %d is fixed and cannot be programmed", memory_num)
            return

        _LOGGER.debug("Arming memory store for slot %d", memory_num)
        outcome = await self._streamer.run_operation(
            okin_store_program(
                memory_num, presses_the_disarm_key=self.presses_the_disarm_key
            )
        )
        if outcome is not OperationOutcome.COMPLETED:
            _LOGGER.debug("Memory store for slot %d ended %s", memory_num, outcome.value)

    async def _wait_hold_deadline(self, deadline: float) -> None:
        """Release at the requested time, including the interval after the last frame."""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(remaining):
                await self._coordinator.cancel_command.wait()

    async def preset_anti_snore(self) -> None:
        """Go to anti-snore position (memory slot 3 on this protocol)."""
        if self._app_profile == "useries":
            raise NotImplementedError("Use leggett_hold_control with an explicit snore duration")
        self._submit(PRESET_ANTI_SNORE)

    async def preset_dummy(self) -> None:
        """Press the key that does nothing but count as a press.

        Pressed like any other preset: this key is only useful as a press, and
        the box counts it as one.
        """
        self._submit(PRESET_DUMMY)

    async def _set_control_mode(self, control: Control, context: str) -> None:
        """Hold one of the box's mode chords to the pulses that acknowledge it."""
        if not self.supports_control_mode_configuration:
            raise NotImplementedError("Control-mode settings are absent from this app profile")
        if control == FACTORY_RESET and not self.supports_control_mode_press_and_hold:
            raise HomeAssistantError(
                "Press-and-hold mode selection is disabled for Prodigy CE: "
                "the SET+FLAT command can factory-reset the bed and erase its presets"
            )
        outcome = await self._streamer.run_operation(okin_mode_program(control))
        if outcome is not OperationOutcome.COMPLETED:
            _LOGGER.debug("The %s gesture ended %s", context, outcome.value)

    async def set_control_mode_press_and_hold(self) -> None:
        """Require a control to remain held while its action runs.

        The chord is a factory reset: restoring the box's defaults is what
        restores hold mode, and it wipes the stored presets with them.
        """
        await self._set_control_mode(FACTORY_RESET, "press-and-hold control mode")

    async def set_control_mode_press_and_release(self) -> None:
        """Allow an action to continue after its control is released.

        One-way: the only route back to hold mode is the factory reset above.
        """
        await self._set_control_mode(LATCH_MODE_ENABLE, "press-and-release control mode")

    async def _tap_keycode(self, command: int, context: str) -> None:
        """Send a keycode as a short press, then release it.

        Lights and massage are ordinary held keycodes in the app, not one-shot
        recalls. Sending the frame alone can leave the key asserted, so the next
        press of the same control may not register.

        The only entity door that still reaches it is the blind light press that
        runs while no light state is known; every other tap is a hold intent,
        and the streamer is the only writer to the command characteristic. It
        stays because the tap shape is a bed-independent contract two other
        branches are changing.
        """
        completed = False
        try:
            # Cleared before the write, so a receipt that arrives while the
            # frame is still on the wire still counts as this press's.
            self._status_frame_received.clear()
            await self.write_command(self._build_command(command))
            await self._hold_press(context)
            completed = True
        finally:
            await self._send_release_frames(context, raise_on_error=completed)

    async def _hold_press(self, context: str) -> None:
        """Hold a tapped key down long enough for the control box to register it."""
        if self._app_profile == "prodigy4":
            await self._hold_cu170_press_from_receipt(context)
            return
        # The app's own one-interval wait, on the profiles whose press floor no
        # hardware run has measured.
        await self._wait_hold_deadline(
            asyncio.get_running_loop().time() + LEGGETT_OKIN_PULSE_DEFAULTS[1] / 1000
        )

    async def _hold_cu170_press_from_receipt(self, context: str) -> None:
        """Wait for the box's receipt of the press, then hold the key past its floor.

        The control box makes its press and release edges from the arrival of
        frames rather than from their contents, so a release timed from the
        write lands inside the press floor whenever the transport is quick and
        the tap does not register. Every frame the box receives draws a status
        notification, so that receipt dates the press on the box's own clock.
        """
        if await self._wait_for_press_receipt(context):
            await self._wait_hold_deadline(asyncio.get_running_loop().time() + CU170_PRESS_HOLD_S)

    async def _wait_for_press_receipt(self, context: str) -> bool:
        """Report whether the box acknowledged the press within the backstop.

        False means release the key now: either a stop was requested, which
        releases at once rather than behind the backstop, or the receipt was
        lost and the box has already made its release edge from the silence.
        """
        receipt = asyncio.create_task(self._status_frame_received.wait())
        cancelled = asyncio.create_task(self._coordinator.cancel_command.wait())
        try:
            done, _ = await asyncio.wait(
                {receipt, cancelled},
                timeout=CU170_PRESS_RECEIPT_BACKSTOP_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (receipt, cancelled):
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if not done:
            _LOGGER.debug(
                "No status frame acknowledged the %s press within %.1fs; releasing it",
                context,
                CU170_PRESS_RECEIPT_BACKSTOP_S,
            )
            return False
        return receipt in done

    # Light methods
    def get_light_state(self) -> dict[str, Any]:
        """Return only feedback from this connection, never a persisted guess."""
        return {"is_on": self._light_is_on}

    async def lights_toggle(self) -> None:
        """Toggle once, then wait for the bed to report the state it produced."""
        if self._light_is_on is None:
            await self._tap_light_and_confirm()
        else:
            await self._set_light_state(not self._light_is_on)

    async def lights_on(self) -> None:
        """Turn on the light, or press once when its state is unknown."""
        await self._set_light_state(True)

    async def lights_off(self) -> None:
        """Turn off the light, or press once when its state is unknown."""
        await self._set_light_state(False)

    async def _tap_light(self) -> None:
        """Press the light key once, whatever the caller knows about the state."""
        await self._tap_keycode(LeggettOkinCommands.TOGGLE_LIGHTS, "lights_toggle")

    def _forget_light_state(self) -> None:
        """Drop a state the bed has not confirmed, so nothing reports a guess."""
        self._light_is_on = None
        self.forward_controller_state_update("under_bed_lights_on", None)

    async def _wait_for_light_report(self) -> None:
        """Wait for the frame that reports the state a blind press produced."""
        try:
            async with asyncio.timeout(LIGHT_STATE_TIMEOUT_S):
                while self._light_is_on is None:
                    self._light_state_changed.clear()
                    await self._light_state_changed.wait()
        except TimeoutError as err:
            self._forget_light_state()
            raise HomeAssistantError("The bed did not confirm the requested light state") from err

    async def _tap_light_and_confirm(self) -> None:
        """Press once with no state known, then take the state the bed reports.

        Without feedback the profile never learns a state and always presses
        once; with feedback and no state yet, one press is the command and the
        bed reports the result. Every frame the box receives draws a receipt
        carrying the current state, so the release frame's receipt, or the
        receipts of the release frames where the profile sends more than one,
        reports the state the press produced. The pre-press receipt is left as
        the last word only when the state-change frame and every release receipt
        are lost, and that residual grows the fewer release frames there are.
        """
        try:
            await self._tap_light()
            if self.supports_light_state_feedback:
                await self._wait_for_light_report()
        except (asyncio.CancelledError, BleakError, ConnectionError):
            # A press that failed or was cancelled produced nothing to report,
            # and the receipt it drew carries the state from before it.
            self._forget_light_state()
            raise

    async def _set_light_state(self, is_on: bool) -> None:
        if self._light_is_on is None:
            await self._tap_light_and_confirm()
            return
        if self._light_is_on == is_on:
            return
        try:
            self._submit(LIGHT_TOGGLE)
            async with asyncio.timeout(LIGHT_STATE_TIMEOUT_S):
                while self._light_is_on != is_on:
                    self._light_state_changed.clear()
                    await self._light_state_changed.wait()
        except TimeoutError as err:
            self._forget_light_state()
            raise HomeAssistantError("The bed did not confirm the requested light state") from err
        except (asyncio.CancelledError, BleakError, ConnectionError):
            self._forget_light_state()
            raise

    # Massage methods
    #
    # There is deliberately no ``massage_off`` override: massage power is a
    # single toggle keycode with no discrete off. ``supports_massage_off_control``
    # detects the capability by checking whether the subclass overrides
    # ``massage_off``, so overriding it just to raise NotImplementedError would
    # advertise a massage-off button that can only ever fail (issue #368).
    async def massage_head_up(self) -> None:
        """Increase head massage intensity."""
        self._submit(MASSAGE_HEAD_UP)

    async def massage_head_down(self) -> None:
        """Decrease head massage intensity."""
        self._submit(MASSAGE_HEAD_DOWN)

    async def massage_foot_up(self) -> None:
        """Increase foot massage intensity."""
        self._submit(MASSAGE_FOOT_UP)

    async def massage_foot_down(self) -> None:
        """Decrease foot massage intensity."""
        self._submit(MASSAGE_FOOT_DOWN)

    async def massage_toggle(self) -> None:
        """Toggle massage / step through modes."""
        self._submit(MASSAGE_TOGGLE)

    async def massage_mode_step(self) -> None:
        """Step through massage wave patterns."""
        self._submit(MASSAGE_WAVE_STEP)

    # Pillow motor control. Keep the tilt methods as compatibility aliases for
    # service calls or stale entities created by older releases.
    async def move_pillow_up(self) -> None:
        """Move the pillow motor up."""
        self._submit(self._motor("pillow").up)

    async def move_pillow_down(self) -> None:
        """Move the pillow motor down."""
        self._submit(self._motor("pillow").down)

    async def move_pillow_stop(self) -> None:
        """Stop the pillow motor."""
        self._stop_motor("pillow")

    async def move_tilt_up(self) -> None:
        await self.move_pillow_up()

    async def move_tilt_down(self) -> None:
        await self.move_pillow_down()

    async def move_tilt_stop(self) -> None:
        await self.move_pillow_stop()

    # Lumbar motor control
    async def move_lumbar_up(self) -> None:
        """Move lumbar motor up."""
        self._submit(self._motor("lumbar").up)

    async def move_lumbar_down(self) -> None:
        """Move lumbar motor down."""
        self._submit(self._motor("lumbar").down)

    async def move_lumbar_stop(self) -> None:
        """Stop lumbar motor."""
        self._stop_motor("lumbar")
