"""The CU170's keycodes, its frame composition, and what it declares it can hold.

The control box reads one frame as the OR of every key held down, so composing a
frame is composing a bit set: this module owns the control-name-to-keycode table,
the two protocol revisions' framing, the link-bound writer that puts a frame on
the wire, and the roster declarations that name every control the table covers.

Semantics come from the layout bindings in com.leggett.prodigy4 1.2.0 and from
what the bed itself answered, not from the app's own ``FBP_KEYCODE_*``
identifiers: several of those names are demonstrably wrong for this hardware
(0x800000 is declared LIGHT_INTENSITY_DOWN but is bound to the head massage-down
button). See docs/beds/leggett-okin.md.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio import Future, Task
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ..const import LEGGETT_OKIN_PULSE_DEFAULTS
from ..hold_operation import CuedStage, CueRequest, OperationProgram, PressStage
from ..hold_roster import (
    ActivateSupport,
    Control,
    ControlDeclaration,
    ControlDeclarationInputs,
    ControlMark,
    HoldSupport,
    MotorDirection,
    PressFloor,
    StagedActivate,
    motor_control_name,
    preset_control_name,
)
from ..hold_streamer import PING, StreamProfile
from .okin_protocol import build_okin_command

if TYPE_CHECKING:
    from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)


class LeggettOkinCommands:
    """Leggett & Platt Okin keycode constants (32-bit values)."""

    # Presets. FLAT is a held button rather than a one-shot recall; the memory
    # slots and SNORE are recalls the control box completes on its own.
    PRESET_FLAT = 0x8000000
    PRESET_MEMORY_1 = 0x1000
    PRESET_MEMORY_2 = 0x2000
    PRESET_MEMORY_3 = 0x4000  # Ships pre-assigned as the snore position
    PRESET_MEMORY_4 = 0x8000
    PRESET_ANTI_SNORE = 0x4000  # Deliberate alias of memory 3
    # Arms the control box to overwrite the next recalled slot. This is NOT a
    # recall: sending it alone and then a slot code reprograms that slot.
    MEMORY_STORE = 0x10000
    # A key with no function of its own. The control box still counts it as a
    # button press, which is the point of exposing it: pressing it clears a
    # pending memory store without moving the bed or changing anything else.
    DUMMY = 0x40000

    # Motor controls
    MOTOR_HEAD_UP = 0x1
    MOTOR_HEAD_DOWN = 0x2
    MOTOR_FEET_UP = 0x4
    MOTOR_FEET_DOWN = 0x8
    MOTOR_TILT_UP = 0x10
    MOTOR_TILT_DOWN = 0x20
    MOTOR_LUMBAR_UP = 0x40
    MOTOR_LUMBAR_DOWN = 0x80

    # Massage
    MASSAGE_HEAD_UP = 0x800
    MASSAGE_HEAD_DOWN = 0x800000
    MASSAGE_FOOT_UP = 0x400
    MASSAGE_FOOT_DOWN = 0x1000000
    MASSAGE_STEP = 0x100
    MASSAGE_WAVE_STEP = 0x10000000

    # Lights
    TOGGLE_LIGHTS = 0x20000

    # Persistent handset-control mode settings. The press-and-hold chord is a
    # factory reset - restoring the defaults is what restores hold mode - and
    # the press-and-release chord latches the box into toggle mode one way.
    CONTROL_MODE_PRESS_AND_HOLD = 0x08010000
    CONTROL_MODE_PRESS_AND_RELEASE = 0x01800000


# Every press registers past the box's debounce, taken equal to its 217-218 ms
# motion watchdog, plus the margin that covers the client-side pacing jitter.
PRESS_FLOOR_MS: Final = 223
# A motor and a preset hold for at most the timed_move service's own maximum, so
# no clamp binds a valid call.
HOLD_TTL_MAX_MS: Final = 30000
# The link benchmark holds long enough for the hardware checklist's 60 s run.
PING_TTL_MAX_MS: Final = 120000
# Slot 3 is the vendor's fixed snore entry and is programmable on no path.
PROGRAMMABLE_MEMORY_SLOTS: Final = (1, 2, 4)

# The emission floor F: the vendor app's own cadence, which its live sender
# streams a held key at (`emission-cadence`). Deliberately not the entry's
# default pulse delay, which happens to be the same number: that is a
# configurable the roster floors an Activate against, and a change to it must
# not move the wire cadence.
FRAME_INTERVAL_MS: Final = 100
# The box's 217-218 ms motion watchdog read at its midpoint, and the margin a
# sender keeps below that window.
CU170_STREAM_PROFILE: Final = StreamProfile(
    frame_interval_ms=FRAME_INTERVAL_MS,
    sustain_window_ms=217.5,
    send_margin_ms=5,
)

# The four physical actuators, and the older motor names each still answers to.
_MOTORS: Final = (("head", "back"), ("feet", "legs"), ("pillow", "tilt"), ("lumbar", None))

# Every control this bed declares, named once so no layer spells one twice.
# ``PING`` is the streamer's, because the streamer needs its identity to tell a
# bit-empty benchmark frame from a release.
LIGHT_TOGGLE = Control("light-toggle")
PRESET_FLAT = Control("preset-flat")
PRESET_DUMMY = Control("preset-dummy")
PRESET_ANTI_SNORE = Control(preset_control_name(3))
FACTORY_RESET = Control("factory-reset")
LATCH_MODE_ENABLE = Control("latch-mode-enable")
# Declared in this order wherever the profile has them, so a roster's control
# order follows the module rather than a set's iteration.
_MODE_CHORDS: Final = (FACTORY_RESET, LATCH_MODE_ENABLE)
MASSAGE_TOGGLE = Control("massage-toggle")
MASSAGE_HEAD_UP = Control("massage-head-up")
MASSAGE_HEAD_DOWN = Control("massage-head-down")
MASSAGE_FOOT_UP = Control("massage-foot-up")
MASSAGE_FOOT_DOWN = Control("massage-foot-down")
MASSAGE_WAVE_STEP = Control("massage-wave-step")


def _motor(motor: str, direction: MotorDirection) -> Control:
    """Return one actuator's control, driven one way."""
    return Control(motor_control_name(motor, direction))


def _preset(slot: int) -> Control:
    """Return one memory slot's recall control."""
    return Control(preset_control_name(slot))


def _store(slot: int) -> Control:
    """Return the arming control that stores into one memory slot."""
    return Control(f"store-{preset_control_name(slot)}")


_KEYCODES: Final[dict[Control, int]] = {
    _motor("head", "up"): LeggettOkinCommands.MOTOR_HEAD_UP,
    _motor("head", "down"): LeggettOkinCommands.MOTOR_HEAD_DOWN,
    _motor("feet", "up"): LeggettOkinCommands.MOTOR_FEET_UP,
    _motor("feet", "down"): LeggettOkinCommands.MOTOR_FEET_DOWN,
    _motor("pillow", "up"): LeggettOkinCommands.MOTOR_TILT_UP,
    _motor("pillow", "down"): LeggettOkinCommands.MOTOR_TILT_DOWN,
    _motor("lumbar", "up"): LeggettOkinCommands.MOTOR_LUMBAR_UP,
    _motor("lumbar", "down"): LeggettOkinCommands.MOTOR_LUMBAR_DOWN,
    PRESET_FLAT: LeggettOkinCommands.PRESET_FLAT,
    _preset(1): LeggettOkinCommands.PRESET_MEMORY_1,
    _preset(2): LeggettOkinCommands.PRESET_MEMORY_2,
    _preset(3): LeggettOkinCommands.PRESET_MEMORY_3,
    _preset(4): LeggettOkinCommands.PRESET_MEMORY_4,
    PRESET_DUMMY: LeggettOkinCommands.DUMMY,
    LIGHT_TOGGLE: LeggettOkinCommands.TOGGLE_LIGHTS,
    MASSAGE_TOGGLE: LeggettOkinCommands.MASSAGE_STEP,
    MASSAGE_HEAD_UP: LeggettOkinCommands.MASSAGE_HEAD_UP,
    MASSAGE_HEAD_DOWN: LeggettOkinCommands.MASSAGE_HEAD_DOWN,
    MASSAGE_FOOT_UP: LeggettOkinCommands.MASSAGE_FOOT_UP,
    MASSAGE_FOOT_DOWN: LeggettOkinCommands.MASSAGE_FOOT_DOWN,
    MASSAGE_WAVE_STEP: LeggettOkinCommands.MASSAGE_WAVE_STEP,
    _store(1): LeggettOkinCommands.MEMORY_STORE,
    _store(2): LeggettOkinCommands.MEMORY_STORE,
    _store(4): LeggettOkinCommands.MEMORY_STORE,
    FACTORY_RESET: LeggettOkinCommands.CONTROL_MODE_PRESS_AND_HOLD,
    LATCH_MODE_ENABLE: LeggettOkinCommands.CONTROL_MODE_PRESS_AND_RELEASE,
    PING: 0,
}

_MASSAGE_CONTROLS: Final = (
    MASSAGE_TOGGLE,
    MASSAGE_HEAD_UP,
    MASSAGE_HEAD_DOWN,
    MASSAGE_FOOT_UP,
    MASSAGE_FOOT_DOWN,
    MASSAGE_WAVE_STEP,
)

# Each preset control answers to the entity key of the button that renders it,
# so a preset tile resolves its own control and no layer composes a name. Slot 3
# answers to two keys: snore and memory 3 are one command at one value on this
# bed, so the anti-snore tile is the slot's tile under another name.
_PRESET_ALIASES: Final[dict[str, frozenset[str]]] = {
    PRESET_FLAT.name: frozenset({"preset_flat"}),
    PRESET_DUMMY.name: frozenset({"preset_dummy"}),
    **{
        preset_control_name(slot): frozenset({f"preset_memory_{slot}"})
        for slot in (1, 2, 4)
    },
    preset_control_name(3): frozenset({"preset_memory_3", "preset_anti_snore"}),
}

# The box answers a gesture by flashing the under-bed light. A memory store's
# arm cue is one pulse about 1-2 s into the SET hold and its saved cue three
# pulses after the slot key; the vendors' 5-6 s holds are the ceiling, not the
# arming time.
STORE_ARM_CUE: Final = CueRequest(1)
STORE_SAVED_CUE: Final = CueRequest(3)
STORE_STAGE_CEILING_MS: Final = 5500
# The three captured mode gestures answer 5.0-5.2 s into the hold with a burst
# spanning 281-625 ms, so 7000 clears the observed envelope.
MODE_STAGE_CEILING_MS: Final = 7000
_MODE_CUES: Final[dict[str, CueRequest]] = {
    FACTORY_RESET.name: CueRequest(2),
    LATCH_MODE_ENABLE.name: CueRequest(4),
}


def okin_store_program(slot: int, *, presses_the_disarm_key: bool) -> OperationProgram:
    """Return the program that stores the bed's pose into slot.

    SET alone to the arm cue, a release edge, then the slot key to the saved
    cue. A stage that ends without its cue owes the disarming press instead: a
    slot key against an armed box stores where it should recall.

    ``presses_the_disarm_key`` is the profile's own answer, because the key is
    declared on Prodigy CE alone. Elsewhere the recovery track is empty and a
    failed store stops at the release, which is what the profile's roster can
    price: a program naming a control the roster never declared is one the
    streamer cannot run.
    """
    return OperationProgram(
        stages=(
            CuedStage(
                controls=frozenset({_store(slot)}),
                cue=STORE_ARM_CUE,
                ceiling_ms=STORE_STAGE_CEILING_MS,
            ),
            CuedStage(
                controls=frozenset({_preset(slot)}),
                cue=STORE_SAVED_CUE,
                ceiling_ms=STORE_STAGE_CEILING_MS,
            ),
        ),
        recovery=(_dummy_stage(),) if presses_the_disarm_key else (),
    )


def okin_mode_program(control: Control) -> OperationProgram:
    """Return the one stage a mode chord runs, held to its own pulse count.

    No recovery: neither chord arms anything a failure would owe a press for.
    """
    return OperationProgram(
        stages=(
            CuedStage(
                controls=frozenset({control}),
                cue=_MODE_CUES[control.name],
                ceiling_ms=MODE_STAGE_CEILING_MS,
            ),
        )
    )


def okin_dummy_program() -> OperationProgram:
    """Return the one press that clears a possibly-armed box.

    The key has no function of its own, so the press is invisible; what it does
    is count as a button, which is what the box's store arming watches for.
    """
    return OperationProgram(stages=(_dummy_stage(),))


def _dummy_stage() -> PressStage:
    """Return one press of the key that does nothing."""
    return PressStage(controls=frozenset({PRESET_DUMMY}))


def build_frame(command_value: int, revision: int | None) -> bytes:
    """Return the frame carrying one composed keycode, in the resolved revision.

    Revision 0 is the app's checksummed E5 FE 16 frame; every other resolution,
    an unresolved one included, takes the plain revision-1 frame.
    """
    if revision == 0:
        keycode = build_okin_command(command_value)[2:]
        frame = b"\xe5\xfe\x16" + keycode
        return frame + bytes(((~sum(frame)) & 0xFF,))
    return build_okin_command(command_value)


class OkinFrameEncoder:
    """Composes the CU170's frame from the controls a wake expresses."""

    def __init__(self, revision: int | None) -> None:
        """Initialize the encoder for one link's resolved protocol revision."""
        self._revision = revision

    def encode(self, controls: frozenset[Control]) -> bytes:
        """Return the frame asserting exactly these controls.

        The empty set encodes as the zero frame, which is this protocol's
        release: nothing distinguishes it from any other frame but its bits.

        Raises:
            ConnectionError: Thrown when the link's protocol revision is
                unresolved, which is what ``_build_command`` answers for the
                same state: one policy, both paths. Guessing revision 1 puts
                frames an R0 box ignores on the wire while the timer and alarm
                paths refuse to write at all, which reads as a dead bed with no
                error. No frame leaves, so the lifecycle never opens and the
                box's watchdog covers anything already in flight; the refusal
                ends the streamer's pump, which logs it and hands it to whoever
                is awaiting a staged operation.
            KeyError: Thrown when a control has no keycode on this bed.
        """
        if self._revision is None:
            raise ConnectionError("Leggett key characteristic is not resolved")
        value = 0
        for control in controls:
            value |= _KEYCODES[control]
        return build_frame(value, self._revision)


class OkinFrameWriter:
    """Puts one frame on one link's control characteristic.

    Submissions do not block the caller: each frame goes out as its own task
    under the controller's BLE lock, so a frame never races a position read and
    the lock's queue keeps the frames in submission order.

    One link, one client: the writer is built with the connected client the
    controller was built on, and a link that has since dropped raises from
    ``_write`` rather than being a state this type models.
    """

    def __init__(
        self,
        *,
        client: BleakClient,
        ble_lock: asyncio.Lock,
        characteristic_uuid: str,
    ) -> None:
        """Initialize the writer for one link."""
        self._client = client
        self._ble_lock = ble_lock
        self._characteristic_uuid = characteristic_uuid
        self._pending: set[Task[None]] = set()

    def submit(self, frame: bytes) -> None:
        """Send frame as a Write Command, whose failure only the log learns."""
        task = self._start(frame, confirmed=False)
        task.add_done_callback(_log_unconfirmed_failure)

    def submit_confirmed(self, frame: bytes) -> Future[None]:
        """Send frame as a Write Request, returning its completion.

        The completion is the write's own task, so cancelling it drops a frame
        still queued on the BLE lock and interrupts one being written.
        """
        return self._start(frame, confirmed=True)

    def _start(self, frame: bytes, *, confirmed: bool) -> Task[None]:
        """Run one write as its own task, kept alive until it completes."""
        task = asyncio.get_running_loop().create_task(self._write(frame, confirmed=confirmed))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def _write(self, frame: bytes, *, confirmed: bool) -> None:
        """Write one frame, raising what the link raises.

        Raises:
            ConnectionError: Thrown when the link is gone.
        """
        if not self._client.is_connected:
            raise ConnectionError("Not connected to bed")
        async with self._ble_lock:
            await self._client.write_gatt_char(
                self._characteristic_uuid, frame, response=confirmed
            )


def _log_unconfirmed_failure(task: Task[None]) -> None:
    """Read the error of a write whose fate nothing else learns."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _LOGGER.debug("Leggett Okin stream frame failed: %s", error)


@dataclass(frozen=True, slots=True)
class OkinProfile:
    """Which of the CU170's controls one app profile actually carries.

    Composed from the controller's own capability answers rather than read off
    the profile table a second time, so a control's declaration and the refusal
    guarding the same control cannot disagree about whether the bed has it.
    """

    motors: frozenset[str]
    memory_slots: int
    programs_memory: bool
    mode_chords: frozenset[Control]
    presses_the_disarm_key: bool


def control_declarations(
    inputs: ControlDeclarationInputs, profile: OkinProfile
) -> tuple[ControlDeclaration, ...]:
    """Return every control this bed carries, for one app profile and one entry.

    A profile can have fewer actuators, fewer memory slots, and on U-Series no
    settings chords at all, so the roster declares only what the box answers
    for: a control the profile lacks is one no sample, no service call and no
    card tile can reach, rather than a keycode with nothing behind it. Massage
    controls appear only where the entry says the bed has massage, matching the
    button platform's own gate.
    """
    declarations = [
        *_motor_declarations(inputs, profile),
        *_preset_declarations(profile),
        _tap(LIGHT_TOGGLE),
        *_store_declarations(profile),
        *_mode_declarations(profile),
        ControlDeclaration(
            control=PING,
            # A control with no bits is neither momentary nor latching, so the
            # uniform press floor does not apply to it.
            press_floor=PressFloor(frames=1, ms=0),
            hold=HoldSupport(ttl_max_ms=PING_TTL_MAX_MS),
        ),
    ]
    if inputs.has_massage:
        declarations.extend(_tap(control) for control in _MASSAGE_CONTROLS)
    return tuple(declarations)


def _motor_declarations(
    inputs: ControlDeclarationInputs, profile: OkinProfile
) -> list[ControlDeclaration]:
    """Return both directions of each actuator this profile drives."""
    duration_ms = _activate_duration_ms(inputs)
    return [
        ControlDeclaration(
            control=_motor(motor, direction),
            press_floor=PressFloor(frames=1, ms=PRESS_FLOOR_MS),
            hold=HoldSupport(ttl_max_ms=HOLD_TTL_MAX_MS),
            activate=ActivateSupport(duration_ms=duration_ms),
            aliases=frozenset()
            if alias is None
            else frozenset({motor_control_name(alias, direction)}),
        )
        for motor, alias in _MOTORS
        if motor in profile.motors
        for direction in ("up", "down")
    ]


def _activate_duration_ms(inputs: ControlDeclarationInputs) -> int:
    """Return how long one cover press drives a motor.

    The entry's configured pulse as wall-clock time. The delay is floored at the
    protocol's proven cadence, because the setup flows accept any integer and a
    zero or small value would otherwise price a press at nothing; the product is
    floored at the press minimum, so a press always registers.
    """
    delay_ms = max(inputs.motor_pulse_delay_ms, LEGGETT_OKIN_PULSE_DEFAULTS[1])
    return max(inputs.motor_pulse_count * delay_ms, PRESS_FLOOR_MS)


def _preset_declarations(profile: OkinProfile) -> list[ControlDeclaration]:
    """Return every preset this profile recalls, all Hold-only.

    A preset holds and never activates. The slots are the ones the profile
    reaches, so a two-slot surface declares no slot 3, which is also this
    protocol's fixed anti-snore entry; the no-op disarming key is declared only
    where its press is hardware-confirmed. Each carries the entity key of the
    button that renders it as an alias, so the roster is the sole author of the
    control's name and the button asks for it by the one name it already has.
    """
    controls = [PRESET_FLAT]
    if profile.presses_the_disarm_key:
        controls.append(PRESET_DUMMY)
    controls.extend(_preset(slot) for slot in range(1, profile.memory_slots + 1))
    return [
        ControlDeclaration(
            control=control,
            press_floor=PressFloor(frames=1, ms=PRESS_FLOOR_MS),
            hold=HoldSupport(ttl_max_ms=HOLD_TTL_MAX_MS),
            aliases=_PRESET_ALIASES.get(control.name, frozenset()),
        )
        for control in controls
    ]


def _store_declarations(profile: OkinProfile) -> list[ControlDeclaration]:
    """Return one store control per slot this profile can program."""
    if not profile.programs_memory:
        return []
    return [
        _operation(_store(slot))
        for slot in PROGRAMMABLE_MEMORY_SLOTS
        if slot <= profile.memory_slots
    ]


def _mode_declarations(profile: OkinProfile) -> list[ControlDeclaration]:
    """Return the settings chords this profile stages, both deliberate-only."""
    return [
        _operation(control, deliberate_only=True)
        for control in _MODE_CHORDS
        if control in profile.mode_chords
    ]


def _operation(control: Control, *, deliberate_only: bool = False) -> ControlDeclaration:
    """Return a control whose Activate stages bed-side."""
    return ControlDeclaration(
        control=control,
        press_floor=PressFloor(frames=1, ms=PRESS_FLOOR_MS),
        activate=StagedActivate(),
        marks=frozenset({ControlMark.DELIBERATE_ONLY} if deliberate_only else ()),
    )


def _tap(control: Control) -> ControlDeclaration:
    """Return an Activate-only control: one press, the press minimum long."""
    return ControlDeclaration(
        control=control,
        press_floor=PressFloor(frames=1, ms=PRESS_FLOOR_MS),
        activate=ActivateSupport(duration_ms=PRESS_FLOOR_MS),
    )
