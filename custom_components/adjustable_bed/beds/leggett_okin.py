"""Leggett & Platt Okin variant bed controller implementation.

Reverse engineering by MarcusW and Richard Hopton (smartbed-mqtt).

This controller handles Leggett & Platt beds using the Okin binary protocol.

Protocol details:
    Service UUID: 62741523-52f9-8864-b1ab-3b3a8d65950b (shared with Okimat/Nectar)
    Write characteristic: 62741525-52f9-8864-b1ab-3b3a8d65950b
    State characteristic: 62741625-52f9-8864-b1ab-3b3a8d65950b (read + notify)
    Command format: 6-byte binary [0x04, 0x02, <4-byte-command-big-endian>]
    Motor timing: held keycodes stream every 100ms, released with four zero frames
    Position feedback: Not supported; the state characteristic carries a bitmask
        of latched functions (under-bed light today)
    Pairing: Required before first use; handled by coordinator

Note: This shares the same BLE service UUID with Okimat and Nectar beds.
Detection uses device name patterns ("leggett", "l&p", "lp bed") to distinguish
between these bed types. See okin_protocol.py for the shared binary protocol
specification.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError

from ..const import LEGGETT_OKIN_CHAR_UUID, LEGGETT_OKIN_NOTIFY_CHAR_UUID
from .base import BedController, MotorControlSpec
from .okin_protocol import build_okin_command

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..coordinator import AdjustableBedCoordinator

_LOGGER = logging.getLogger(__name__)


class LeggettOkinCommands:
    """Leggett & Platt Okin keycode constants (32-bit values).

    Semantics come from the layout bindings in com.leggett.prodigy4 1.2.0, not
    from the app's own ``FBP_KEYCODE_*`` identifiers: several of those names are
    demonstrably wrong for this hardware (0x800000 is declared
    LIGHT_INTENSITY_DOWN but is bound to the head massage-down button). See
    docs/beds/leggett-okin.md.
    """

    # Presets. FLAT, the memory slots and SNORE are all latched recalls: the
    # control box completes the move on its own once the keycode arrives.
    PRESET_FLAT = 0x8000000
    PRESET_ZERO_G = 0x1000  # Deliberate alias of memory 1
    PRESET_MEMORY_1 = 0x1000
    PRESET_MEMORY_2 = 0x2000
    PRESET_MEMORY_3 = 0x4000  # Ships pre-assigned as the snore position
    PRESET_MEMORY_4 = 0x8000
    PRESET_ANTI_SNORE = 0x4000  # Deliberate alias of memory 3
    # Arms the control box to overwrite the next recalled slot. This is NOT a
    # recall: sending it alone and then a slot code reprograms that slot.
    MEMORY_STORE = 0x10000

    # Motor controls. The frame has exactly these four motors, and the physical
    # remote labels its sections HEAD, PILLOW, LUMBAR, FOOT - the app's slider
    # tags carry the same keycodes.
    MOTOR_HEAD_UP = 0x1
    MOTOR_HEAD_DOWN = 0x2
    MOTOR_FEET_UP = 0x4
    MOTOR_FEET_DOWN = 0x8
    MOTOR_PILLOW_UP = 0x10
    MOTOR_PILLOW_DOWN = 0x20
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


# Every repeated-frame family below is a *cadence*, not a post-response sleep:
# the app's output thread ticks one unconditional 100ms scheduler for every key
# including releases and recalls, and the control box's keep-alive watchdog
# bounds the gap between frames rather than the gap after a response. Adding a
# BLE round trip on top of these numbers is what produced gaps past that
# watchdog, so they are all sent through write_command_paced.
#
# Pacing makes the inter-frame interval max(cadence, round trip) instead of
# cadence + round trip. A link that keeps up with the cadence therefore runs
# these families at exactly the nominal numbers; a slower proxied link is round
# trip bound and runs longer, but never longer than the unpaced path did.

# The app streams a held keycode until release, then emits exactly four
# keycode-0 frames (OutputThread.runNormal, MaxZeroCount = 3). There is no
# distinct stop opcode: the release frame is an ordinary frame carrying 0.
RELEASE_FRAME_COUNT = 4
# Same 100ms as the held-key cadence, but deliberately a separate constant:
# these are independent findings about different command families, and retuning
# one must not silently retune the other. The release burst is also not
# configurable - it is the protocol's only stop, so it is not a knob to sweep.
RELEASE_FRAME_CADENCE_MS = 100

# A recall is one frame with no terminator at all. The control box latches the
# keycode and drives the move to completion by itself, so appending a release
# frame could cancel the motion the recall just started - and so could a second
# copy of the keycode: once the box's ~235ms hold watchdog has expired, another
# frame is a second press, and a second press during preset motion stops the
# bed. Delivery is therefore confirmed rather than repeated; see _recall.
#
# How long to wait for that confirmation. A write round trip through an ESPHome
# proxy to this box measured ~180ms with a tail past 250ms, and the receipt is
# pushed back over the same link, so this is ~2x the measured tail: long enough
# that a slow-but-fine frame is never retried, short enough that a command which
# genuinely never landed is retried while the user is still watching.
RECALL_RECEIPT_TIMEOUT_S = 0.5

# Programming a slot is a two-stage hold, not an opcode: MEMORY_STORE for ~5s,
# then the slot keycode for ~2s. The app switches between the stages directly -
# it swaps its key buffer within one 100ms tick, so no release burst separates
# them - and releases only once, after the slot hold. Decompile-derived
# (MainActivityBase.onPositionSetMessage); not yet verified on hardware.
MEMORY_STORE_HOLD_S = 5.0
MEMORY_SLOT_HOLD_S = 2.0
MEMORY_PROGRAM_FRAME_CADENCE_MS = 100

# A movement hold has no protocol-defined end: the box moves while the keycode
# keeps arriving. The stream therefore ends only when the next serialized
# command preempts it (a stop, a new direction) or when this cap expires. The
# cap exists so a lost stop can never run a motor forever; a full recline takes
# about 30s, so this covers full travel on any actuator with margin. A hold
# outlasting the cap restarts cleanly: the capped call returns and the card
# re-issues the movement while the button stays pressed. No existing option
# models a hold duration (the pulse options set a frame cadence and, for a
# latched command, an attempt budget), so this is a constant rather than
# configuration.
MOVEMENT_HOLD_CAP_S = 60.0


# Feedback frame layout, from a capture of a CU170 box (frames were 20 bytes):
#   byte[0] & 0x0F  payload size (9 observed)
#   byte[1]         wire opcode (0x0b observed)
#   bytes[2..5]     state bitmask, big-endian
#   bytes[6..9]     a second copy of bytes[2..5]
#   byte[10]        0xFF observed
# Bytes past 10 are unknown, and the second copy is not cross-checked: on this
# hardware every frame is a full-state replace, so the first copy is the state.
STATUS_FRAME_MIN_LENGTH = 6
_STATUS_MASK_BYTES = slice(2, 6)

# Under-bed light, confirmed on hardware. It happens to equal TOGGLE_LIGHTS, but
# it is written out here because mask bits do NOT generally mirror keycodes: the
# app renders mask bits 0x4000 and 0x8000 as the alarm-armed and sleep-timer-armed
# indicators (activity_main.xml ledMask tags), while those same values as keycodes
# recall memory slots 3 and 4. Each state bit needs its own evidence.
STATUS_LIGHT = 0x20000


@dataclass(frozen=True, slots=True)
class LeggettOkinStatus:
    """State bitmask reported by the control box.

    Only the under-bed light bit is identified here. The raw mask is kept intact
    so unidentified bits reach diagnostics, and further bits get named as
    evidence for each one arrives.
    """

    mask: int

    @classmethod
    def from_frame(cls, data: bytes) -> LeggettOkinStatus | None:
        """Parse a feedback frame, or return None if it is too short to carry a mask."""
        if len(data) < STATUS_FRAME_MIN_LENGTH:
            return None
        return cls(int.from_bytes(data[_STATUS_MASK_BYTES], "big"))

    @property
    def light(self) -> bool:
        """Return True when the under-bed light is on."""
        return bool(self.mask & STATUS_LIGHT)

    def __repr__(self) -> str:
        """Return the raw mask in hex, so unnamed bits survive into logs."""
        return f"LeggettOkinStatus(0x{self.mask:08x})"


class MotorDirection(Enum):
    """Direction for motor movement."""

    UP = "up"
    DOWN = "down"
    STOP = "stop"


class LeggettOkinController(BedController):
    """Controller for Leggett & Platt beds using Okin protocol.

    These beds use the binary Okin protocol and require BLE pairing.
    They support motor control, presets, massage, and under-bed lighting.
    """

    def __init__(self, coordinator: AdjustableBedCoordinator) -> None:
        """Initialize the Leggett & Platt Okin controller."""
        super().__init__(coordinator)
        self._motor_state: dict[str, MotorDirection] = {}
        # Set by every status frame the box pushes. _recall arms it before a
        # write and waits on it afterwards, so it means "the box answered
        # something we sent since we armed it".
        self._status_receipt = asyncio.Event()
        # Whether a status frame has ever reached us on this connection. Boxes
        # without the feedback characteristic never answer at all, and on those
        # an unanswered frame is no evidence that anything was lost.
        self._status_receipt_channel_proven = False
        # Live state from the feedback characteristic; None until a frame arrives.
        self._status: LeggettOkinStatus | None = None
        self._last_status_frame: bytes | None = None
        self._notify_started = False
        _LOGGER.debug("LeggettOkinController initialized")

    def forward_raw_notification(self, characteristic_uuid: str, data: bytes) -> None:
        """Note the box's receipt for a sent frame, then forward as usual.

        The box answers every command frame it receives with a state frame, so
        this is where a confirmed send learns that its frame landed. Every path
        that observes a state frame already funnels through this call, so
        hooking it here needs no second subscription and no second parser.
        """
        if characteristic_uuid == LEGGETT_OKIN_NOTIFY_CHAR_UUID:
            self._status_receipt_channel_proven = True
            self._status_receipt.set()
        super().forward_raw_notification(characteristic_uuid, data)

    @property
    def control_characteristic_uuid(self) -> str:
        """Return the UUID of the control characteristic."""
        return LEGGETT_OKIN_CHAR_UUID

    @property
    def requires_notification_channel(self) -> bool:
        """Keep the feedback subscription up even with angle sensing disabled.

        It carries no angles to disable - it is the only source of light state.
        """
        return True

    # Capability properties
    @property
    def supports_preset_zero_g(self) -> bool:
        return True

    @property
    def supports_preset_anti_snore(self) -> bool:
        """Return True - 0x4000 is bound to the app's dedicated snore button."""
        return True

    @property
    def supports_lights(self) -> bool:
        """Return True - Okin beds support under-bed lighting."""
        return True

    @property
    def supports_discrete_light_control(self) -> bool:
        """Return True - the wire primitive is a toggle, but on/off are discrete.

        lights_on and lights_off decide against the state the bed reports, so
        they are idempotent in the way a discrete control has to be. This is
        what puts the under-bed light on a switch instead of a toggle button.
        """
        return True

    @property
    def supports_under_bed_lights(self) -> bool:
        """Return True - the light has its own keycode and its own state bit."""
        return True

    @property
    def supports_light_state_feedback(self) -> bool:
        """Return True - the state characteristic reports every light change.

        This is what keeps the switch entity pessimistic: the box ACKs writes
        it silently ignores on an unbonded link, so command success proves
        nothing and only reported state may drive the displayed state.
        """
        return True

    @property
    def supports_memory_presets(self) -> bool:
        """Return True - Okin beds support memory presets 1-4."""
        return True

    @property
    def memory_slot_count(self) -> int:
        """Return 4 - Okin beds support memory slots 1-4."""
        return 4

    @property
    def supports_memory_programming(self) -> bool:
        """Return True - slots are programmed by the two-stage store hold."""
        return True

    @property
    def has_pillow_support(self) -> bool:
        """Return True - Okin beds have a pillow motor (0x10/0x20)."""
        return True

    @property
    def has_lumbar_support(self) -> bool:
        """Return True - Okin beds have lumbar motor control."""
        return True

    @property
    def motor_control_specs(self) -> tuple[MotorControlSpec, ...]:
        """Expose the four motors this frame has, in remote order.

        The base layout is Linak-shaped: back and legs unconditionally, head and
        feet gated on motor_count, plus whatever the has_*_support flags add. On
        this protocol that produced six covers for four motors, because
        move_back_* and move_legs_* are pure aliases of move_head_* and
        move_feet_*: "back" duplicated "head" (0x1/0x2), "legs" duplicated
        "feet" (0x4/0x8), and the pillow motor (0x10/0x20) was labelled "tilt".

        motor_count is deliberately not consulted. Every frame on this protocol
        drives all four keycodes, and the count is a user-entered value that
        cannot tell us otherwise.
        """
        return (
            MotorControlSpec(
                key="head",
                translation_key="head",
                open_fn=lambda ctrl: ctrl.move_head_up(),
                close_fn=lambda ctrl: ctrl.move_head_down(),
                stop_fn=lambda ctrl: ctrl.move_head_stop(),
            ),
            MotorControlSpec(
                key="pillow",
                translation_key="pillow",
                open_fn=lambda ctrl: ctrl.move_pillow_up(),
                close_fn=lambda ctrl: ctrl.move_pillow_down(),
                stop_fn=lambda ctrl: ctrl.move_pillow_stop(),
            ),
            MotorControlSpec(
                key="lumbar",
                translation_key="lumbar",
                open_fn=lambda ctrl: ctrl.move_lumbar_up(),
                close_fn=lambda ctrl: ctrl.move_lumbar_down(),
                stop_fn=lambda ctrl: ctrl.move_lumbar_stop(),
                max_angle=30,
            ),
            MotorControlSpec(
                key="feet",
                translation_key="feet",
                open_fn=lambda ctrl: ctrl.move_feet_up(),
                close_fn=lambda ctrl: ctrl.move_feet_down(),
                stop_fn=lambda ctrl: ctrl.move_feet_stop(),
                max_angle=45,
            ),
        )

    @property
    def stale_motor_entity_keys(self) -> frozenset[str]:
        """Remove the duplicate back/legs covers and the mislabelled tilt cover.

        Without this the three orphaned registry entries survive the upgrade as
        permanently unavailable "restored" entities that only the user can
        delete.
        """
        return frozenset({"back", "legs", "tilt"})

    def _build_command(self, command_value: int) -> bytes:
        """Build Okin binary command by delegating to build_okin_command.

        Args:
            command_value: 32-bit command value (0 to 0xFFFFFFFF)

        Returns:
            6-byte command: [0x04, 0x02, <4-byte-command-big-endian>]
        """
        return build_okin_command(command_value)

    # The motors this frame carries, and the keycode each direction asserts.
    # This table is what "a known motor" means here: _move_motor rejects any
    # name it does not list, so _motor_state can only ever hold these.
    _MOTOR_KEYCODES: dict[str, dict[MotorDirection, int]] = {
        "head": {
            MotorDirection.UP: LeggettOkinCommands.MOTOR_HEAD_UP,
            MotorDirection.DOWN: LeggettOkinCommands.MOTOR_HEAD_DOWN,
        },
        "feet": {
            MotorDirection.UP: LeggettOkinCommands.MOTOR_FEET_UP,
            MotorDirection.DOWN: LeggettOkinCommands.MOTOR_FEET_DOWN,
        },
        "pillow": {
            MotorDirection.UP: LeggettOkinCommands.MOTOR_PILLOW_UP,
            MotorDirection.DOWN: LeggettOkinCommands.MOTOR_PILLOW_DOWN,
        },
        "lumbar": {
            MotorDirection.UP: LeggettOkinCommands.MOTOR_LUMBAR_UP,
            MotorDirection.DOWN: LeggettOkinCommands.MOTOR_LUMBAR_DOWN,
        },
    }

    def _get_move_command(self) -> int:
        """Combine the currently held motor keycodes into one frame.

        No motors held is the resting state, not an error: it yields 0, which
        is this protocol's release frame, and that is exactly what the stop
        path has already cleared the state to mean.
        """
        command = 0
        for motor, direction in self._motor_state.items():
            command |= self._MOTOR_KEYCODES[motor][direction]
        return command

    async def _move_motor(self, motor: str | None, direction: MotorDirection) -> None:
        """Start holding a motor keycode, or stop the active hold.

        The coordinator serializes commands, so at most one hold exists at a
        time: a new movement replaces whatever was held rather than combining
        with it, and a stop ends whichever hold is active (the preempted
        hold's stream is already gone by the time the stop runs, so
        re-streaming a different motor here would restart it).

        A stop therefore has no motor, and says so by passing ``None``. This
        protocol has no per-motor stop to name: the release burst clears the
        whole key buffer, so a name here could only mislead a reader of the
        log into thinking one motor was singled out.

        Validation is total, which is what naming the absence buys. A motor
        that is present must be one of ``_MOTOR_KEYCODES``: an unknown name is
        a wiring mistake in this module, not a device condition, and its
        keycode would be 0 - the release frame - so the stream would push stop
        frames for the full hold cap rather than doing nothing. A motor that is
        absent is only meaningful for a stop; a movement without one has
        nothing to move.

        Raises:
            ValueError: If ``motor`` is not one of this bed's motors, or if a
                movement was asked for without one.
        """
        if motor is not None and motor not in self._MOTOR_KEYCODES:
            raise ValueError(
                f"Unknown Leggett Okin motor: {motor!r}; "
                f"expected one of {sorted(self._MOTOR_KEYCODES)}"
            )
        if direction == MotorDirection.STOP:
            await self._stop_movement("motor stop")
            return
        if motor is None:
            raise ValueError(
                f"A Leggett Okin {direction.value} movement needs a motor; "
                "only a stop has none"
            )
        self._motor_state = {motor: direction}
        await self._stream_movement(self._get_move_command())

    async def _stop_movement(self, context: str) -> None:
        """End the movement lifecycle with the protocol's release burst.

        An explicit stop must not report success when it never reached the
        bed, so failures propagate.
        """
        self._motor_state = {}
        await self._send_release_frames(context, raise_on_error=True)

    async def _stream_movement(self, command: int) -> None:
        """Stream a movement keycode like a held button until the hold ends.

        The box moves a motor only while its keycode keeps arriving, so this
        keeps writing frames instead of sending a finite burst. Exactly one
        release burst ends each movement lifecycle, sent by whichever path
        ends it:

        - Preemption by the next serialized command (the coordinator sets its
          cancel event, which makes the paced write return between frames, and
          cancels this task). No release is sent here: the successor owns the
          bed from this point - a stop sends the release, a movement streams
          its own keycode (the box swaps its key buffer between frames, the
          same way the app switches store->slot without a release), and the
          other command families end with their own terminators. Sending
          zeros here as well is the double-release defect, and on a
          same-direction re-trigger it would stop the bed mid-hold.
        - A caller-bounded hold (``caller_bounded_hold``): the caller ends the
          movement with its own stop, so that stop is the release and none is
          sent here on any path out.
        - The safety cap, for a stop that never arrives: release sent here.
        - A write failure: release attempted as cleanup, error propagates.
        - External cancellation (shutdown or unload, recognizable because the
          coordinator's cancel event is not set): no successor exists, so the
          release is sent before the cancellation propagates.
        """
        frame = self._build_command(command)
        # The configured motor pulse delay is this stream's target cadence:
        # frame k is scheduled at stream start + k * cadence, so a write round
        # trip is absorbed into the interval instead of added to it. The box's
        # keep-alive watchdog halts motion when the inter-frame gap exceeds
        # roughly 235ms (measured), and proxy round trips alone can approach
        # that - a post-response sleep would push every gap past it. A small
        # cadence cannot flood the link (each frame still waits for its write
        # response), so the floor only keeps the arithmetic sane.
        _, cadence_ms = self.motor_pulse_settings()
        cadence_ms = max(1, cadence_ms)
        # Paced, the frame budget and the timeout describe the same hold: the
        # budget is the bound in the normal case and the timeout is the
        # backstop for a link that runs slower than the cadence.
        hold_s = self._hold_seconds()
        frame_budget = max(1, round(hold_s * 1000 / cadence_ms))
        try:
            async with asyncio.timeout(hold_s):
                await self.write_command_paced(
                    frame,
                    repeat_count=frame_budget,
                    cadence_ms=cadence_ms,
                )
        except TimeoutError:
            # The hold ran to its limit; fall through to the release.
            pass
        except asyncio.CancelledError:
            if self._coordinator.cancel_command.is_set():
                raise  # Preempted: the successor command owns the release.
            await self._end_hold(raise_on_release_error=False)
            raise
        except (BleakError, ConnectionError):
            await self._end_hold(raise_on_release_error=False)
            raise
        else:
            if self._coordinator.cancel_command.is_set():
                # write_command_paced exits between frames when the cancel
                # event is set: preemption again, just observed before the task
                # cancellation landed.
                return
        # The lifecycle ended without a successor, so this release is the
        # operation's stop: losing it can leave the bed running and must
        # surface.
        await self._end_hold(raise_on_release_error=True)

    def _hold_seconds(self) -> float:
        """Return how long this movement may stream for.

        A caller that bounded the movement decides its duration; the safety cap
        still applies over it, because the cap exists so a lost stop can never
        run a motor forever and a caller-supplied duration is not a stop.
        """
        bounded_ms = self.caller_bounded_hold_ms
        if bounded_ms is None:
            return MOVEMENT_HOLD_CAP_S
        return min(bounded_ms / 1000, MOVEMENT_HOLD_CAP_S)

    async def _end_hold(self, *, raise_on_release_error: bool) -> None:
        """Drop the held keycode and release it, unless the caller does that.

        Under a caller-bounded hold the caller's own stop is the movement's
        single release burst. Adding one here would double it, and the second
        burst would land after the coordinator handed the bed to whatever ran
        next.
        """
        self._motor_state = {}
        if self.caller_bounded_hold_ms is not None:
            return
        await self._send_release_frames("movement stream", raise_on_error=raise_on_release_error)

    async def _send_release_frames(self, context: str, *, raise_on_error: bool = False) -> None:
        """Send the release burst that ends a held keycode.

        The app emits exactly four keycode-0 frames when a button is released,
        so mirror that rather than a single frame. The burst gets a fresh cancel
        event so a stop request cannot suppress the release itself.

        The burst is paced like every other frame family here: the app's output
        thread ticks these zeros on the same unconditional 100ms schedule as a
        held key, so the round trip belongs inside the interval. That drops the
        burst from four round trips plus 300ms of sleep to four round trips,
        which matters because the cancellation path below blocks the command
        lock until it finishes.

        ``raise_on_error`` is for callers where the burst *is* the operation, so
        a failure must reach the user. Cleanup callers leave it False: they are
        already unwinding and have their own error to report.
        """
        release = asyncio.ensure_future(
            self.write_command_paced(
                self._build_command(0),
                repeat_count=RELEASE_FRAME_COUNT,
                cadence_ms=RELEASE_FRAME_CADENCE_MS,
                cancel_event=asyncio.Event(),
            )
        )
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            # Returning here would hand the command lock back mid-burst: the
            # coordinator would start the replacement command while the shielded
            # task was still emitting zero frames, and those frames would stop
            # the movement it had just started. The burst is bounded (~300ms),
            # so wait it out before propagating.
            while not release.done():
                with contextlib.suppress(asyncio.CancelledError, BleakError, ConnectionError):
                    await asyncio.shield(release)
            raise
        except (BleakError, ConnectionError):
            # The release burst is this protocol's only stop, so a failure can
            # leave the bed still moving. That is worth surfacing, not hiding.
            _LOGGER.warning(
                "Failed to send release frames after %s; the bed may still be moving",
                context,
                exc_info=True,
            )
            if raise_on_error:
                raise

    # Motor control methods
    async def move_head_up(self) -> None:
        """Move head up."""
        await self._move_motor("head", MotorDirection.UP)

    async def move_head_down(self) -> None:
        """Move head down."""
        await self._move_motor("head", MotorDirection.DOWN)

    async def move_head_stop(self) -> None:
        """Stop head motor."""
        await self._move_motor(None, MotorDirection.STOP)

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
        await self._move_motor("feet", MotorDirection.UP)

    async def move_legs_down(self) -> None:
        """Move legs down."""
        await self._move_motor("feet", MotorDirection.DOWN)

    async def move_legs_stop(self) -> None:
        """Stop legs motor."""
        await self._move_motor(None, MotorDirection.STOP)

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
        """Stop all motors by sending the release burst.

        An explicit stop must not report success when it never reached the bed,
        so failures propagate here rather than being logged and swallowed.
        """
        await self._stop_movement("stop_all")

    # Preset methods
    _MEMORY_SLOTS = {
        1: LeggettOkinCommands.PRESET_MEMORY_1,
        2: LeggettOkinCommands.PRESET_MEMORY_2,
        3: LeggettOkinCommands.PRESET_MEMORY_3,
        4: LeggettOkinCommands.PRESET_MEMORY_4,
    }

    async def _recall(self, command: int) -> None:
        """Send a latched command once and confirm the box received it.

        A recall is one frame and then silence: the control box latches the
        keycode and drives the move to completion on its own. This is the one
        command family the app deliberately leaves unterminated, so no release
        frames follow - they could cancel the motion it just started.

        Repeating the frame would do the same. The box retriggers a ~235ms hold
        watchdog on each frame it receives; once that has expired it reads the
        next copy as a *second press*, and a second press during preset motion
        stops the bed. A ten-frame burst at a 100ms cadence was therefore ten
        chances for a link hiccup to cancel the move that its own first frame
        started - and every frame after the first was already redundant, since
        the box had latched.

        So the redundancy is confirmation instead of repetition. The box
        answers every frame it receives with a status frame, which is a
        receipt: if it arrives, the frame landed and there is nothing left to
        do. Only when no receipt arrives - the one case where there is no
        motion in progress to cancel - is another frame sent.

        ``motor_pulse_count`` now bounds the number of *attempts*, not a burst
        length: it is how many unanswered frames this command may spend before
        it reports failure. ``motor_pulse_delay_ms`` no longer applies here;
        retries are paced by ``RECALL_RECEIPT_TIMEOUT_S``, which is a property
        of the link rather than a user preference. A recall's run time still
        belongs entirely to the box.

        Boxes without the feedback characteristic never answer anything. On
        those, silence carries no information, so a single frame is sent and
        trusted rather than retried into a cancellation. The channel counts as
        proven once any status frame has been seen on this connection.

        Raises:
            BleakError: If every attempt went unanswered on a box that is
                known to answer.
        """
        attempts = max(1, self.motor_pulse_settings()[0])
        frame = self._build_command(command)
        channel_proven = self._status_receipt_channel_proven
        for attempt in range(1, attempts + 1):
            self._status_receipt.clear()
            await self.write_command(frame)
            if await self._status_receipt_arrived():
                return
            if not channel_proven:
                _LOGGER.debug(
                    "No status receipt for command %#x and this box has never "
                    "sent one; treating the frame as delivered",
                    command,
                )
                return
            _LOGGER.debug(
                "No status receipt for command %#x within %.0f ms (attempt %d/%d)",
                command,
                RECALL_RECEIPT_TIMEOUT_S * 1000,
                attempt,
                attempts,
            )
        raise BleakError(
            f"Control box did not acknowledge command {command:#x} "
            f"after {attempts} attempt(s)"
        )

    async def _status_receipt_arrived(self) -> bool:
        """Wait out the receipt window for the frame just written.

        The event is armed before the write, so a receipt that arrives while
        the write is still completing is already recorded here and costs no
        wait at all.
        """
        if self._status_receipt.is_set():
            return True
        try:
            async with asyncio.timeout(RECALL_RECEIPT_TIMEOUT_S):
                await self._status_receipt.wait()
        except TimeoutError:
            return False
        return True

    async def preset_flat(self) -> None:
        """Go to flat position.

        The control box latches FLAT and flattens on its own, exactly like a
        memory recall, so this is a single confirmed frame and not a hold. LP
        Control 2.11.0 ships a press-and-release mode whose whole FLAT command
        is a single frame, and its Okin implementation of
        ``setPressAndHoldMode`` is empty - it never tells the box which mode it
        is in, so the box has to be doing the work.

        Flat and the memory slots stay one delivery shape on purpose: they are
        the same command family, and a change that treated only one of them as
        latched would leave the other unexplained. See ``_recall``.
        """
        await self._recall(LeggettOkinCommands.PRESET_FLAT)

    async def preset_memory(self, memory_num: int) -> None:
        """Go to memory preset."""
        command = self._MEMORY_SLOTS.get(memory_num)
        if command is None:
            _LOGGER.warning("Invalid memory slot for recall: %d", memory_num)
            return
        await self._recall(command)

    async def program_memory(self, memory_num: int) -> None:
        """Store the current position into a memory slot.

        There is no program opcode. The box is armed by holding MEMORY_STORE
        for ~5s, then records whichever slot keycode is held for the following
        ~2s. The vendor app switches from the store keycode to the slot keycode
        directly, with no release frames between the stages, and sends the
        normal release burst only once the slot hold ends (decompile-derived;
        hardware verification pending).
        """
        command = self._MEMORY_SLOTS.get(memory_num)
        if command is None:
            _LOGGER.warning("Invalid memory slot for programming: %d", memory_num)
            return

        _LOGGER.debug("Arming memory store for slot %d", memory_num)
        completed = False
        try:
            await self._hold_keycode(LeggettOkinCommands.MEMORY_STORE, MEMORY_STORE_HOLD_S)
            await self._hold_keycode(command, MEMORY_SLOT_HOLD_S)
            completed = True
        finally:
            # On the success path this release ends the sequence, so a failure
            # means the slot keycode may still be asserted and must surface. If
            # we are already unwinding it is cleanup, and must not mask the
            # exception that got us here.
            await self._send_release_frames("memory store slot", raise_on_error=completed)

    async def _hold_keycode(self, command: int, hold_seconds: float) -> None:
        """Stream a keycode for a fixed duration, as a held button would.

        The frame count is derived from the cadence, so pacing is what lets
        ``hold_seconds`` mean seconds: unpaced, each frame cost a write round
        trip on top of the cadence, and the box measures its arm and record
        windows itself. Both stages are also long enough that an unpaced stream
        would almost certainly hit at least one gap past the ~235ms keep-alive
        watchdog, which drops the hold and makes the store look like the box
        rejected it - and nothing here parses acknowledgements, so a dropped
        hold is invisible.
        """
        repeat_count = max(1, round(hold_seconds * 1000 / MEMORY_PROGRAM_FRAME_CADENCE_MS))
        await self.write_command_paced(
            self._build_command(command),
            repeat_count=repeat_count,
            cadence_ms=MEMORY_PROGRAM_FRAME_CADENCE_MS,
        )

    async def preset_zero_g(self) -> None:
        """Go to zero gravity position (memory slot 1 on this protocol)."""
        await self._recall(LeggettOkinCommands.PRESET_ZERO_G)

    async def preset_anti_snore(self) -> None:
        """Go to anti-snore position (memory slot 3 on this protocol)."""
        await self._recall(LeggettOkinCommands.PRESET_ANTI_SNORE)

    async def _tap_keycode(self, command: int, context: str) -> None:
        """Send a keycode as a short press, then release it.

        Lights and massage are ordinary held keycodes in the app, not one-shot
        recalls: a tap is one frame followed by the zero burst. Sending the
        frame alone can leave the key asserted, so the next press of the same
        control may not register.
        """
        completed = False
        try:
            await self.write_command(self._build_command(command))
            completed = True
        finally:
            await self._send_release_frames(context, raise_on_error=completed)

    # State feedback
    #
    # The box emits a frame on every state change whatever caused it - BLE or the
    # wired remote - and a read of the same characteristic returns the current
    # frame. So: read once on connect, then let notifications keep it current.
    async def start_notify(self, callback: Callable[[str, float], None] | None = None) -> None:
        """Subscribe to the state characteristic.

        The position callback is unused; this bed reports no motor angles.
        """
        client = self.client
        if client is None or not client.is_connected:
            raise ConnectionError("Not connected to bed")
        if self._notify_started:
            return
        try:
            async with self._ble_lock:
                await client.start_notify(
                    LEGGETT_OKIN_NOTIFY_CHAR_UUID, self._handle_status_notification
                )
        except BleakError as err:
            # State is a convenience here, not a prerequisite for control: a box
            # without this characteristic must still drive its motors, and the
            # light falls back to a read before each command, then a blind toggle.
            _LOGGER.debug(
                "Could not subscribe to Okin state notifications for %s: %s",
                self._coordinator.address,
                err,
            )
            return
        self._notify_started = True
        _LOGGER.debug("Started Okin state notifications for %s", self._coordinator.address)

    async def stop_notify(self) -> None:
        """Unsubscribe from the state characteristic."""
        client = self.client
        if client is not None and client.is_connected and self._notify_started:
            with contextlib.suppress(Exception):
                async with self._ble_lock:
                    await client.stop_notify(LEGGETT_OKIN_NOTIFY_CHAR_UUID)
        self._notify_started = False

    def _handle_status_notification(self, _sender: Any, data: bytearray) -> None:
        """Handle a state frame pushed by the control box."""
        self._apply_status_frame(bytes(data))

    def _apply_status_frame(self, data: bytes) -> None:
        """Parse a state frame and publish any light-state change."""
        self.forward_raw_notification(LEGGETT_OKIN_NOTIFY_CHAR_UUID, data)
        status = LeggettOkinStatus.from_frame(data)
        if status is None:
            _LOGGER.debug("Ignoring unusable Okin state frame: %s", data.hex())
            return
        previous = self._status
        self._status = status
        self._last_status_frame = data
        if previous is None or previous.light != status.light:
            self.forward_controller_state_updates({"under_bed_lights_on": status.light})

    async def read_light_state(self) -> dict[str, Any]:
        """Read the state characteristic and return the light state it carries."""
        client = self.client
        if client is None or not client.is_connected:
            raise ConnectionError("Not connected to bed")
        async with self._ble_lock:
            data = bytes(await client.read_gatt_char(LEGGETT_OKIN_NOTIFY_CHAR_UUID))
        self._apply_status_frame(data)
        return self.get_light_state()

    def get_light_state(self) -> dict[str, Any]:
        """Return the cached light state, or nothing while it is still unknown."""
        if self._status is None:
            return {}
        return {"is_on": self._status.light}

    @property
    def last_feedback_frame(self) -> str | None:
        """Return the last state frame as hex, for diagnostics captures."""
        if self._last_status_frame is None:
            return None
        return self._last_status_frame.hex()

    @property
    def feedback_state_mask(self) -> int | None:
        """Return the bitmask parsed from the last state frame."""
        return None if self._status is None else self._status.mask

    # Light methods
    async def lights_toggle(self) -> None:
        """Toggle lights."""
        await self._tap_keycode(LeggettOkinCommands.TOGGLE_LIGHTS, "lights_toggle")

    async def _resolve_light_state(self) -> bool | None:
        """Return the reported light state, reading it when the cache is empty.

        The cache is not updated optimistically anywhere: the box reports every
        state change itself, whereas a toggle that never reached the light would
        leave an optimistic cache inverted with no later frame to correct it.
        Right after connect no frame has arrived yet, and a blind toggle sent
        then can physically invert the light - so an empty cache is resolved
        with a read first. A box without the state characteristic fails the
        read and keeps the pre-state-feedback blind toggle as its only option.
        """
        if self._status is None:
            try:
                await self.read_light_state()
            except (BleakError, ConnectionError, TimeoutError) as err:
                _LOGGER.debug(
                    "Could not read Okin light state before deciding for %s: %s",
                    self._coordinator.address,
                    err,
                )
        return None if self._status is None else self._status.light

    async def lights_on(self) -> None:
        """Turn on lights, skipping the toggle when the bed already reports them on."""
        if await self._resolve_light_state() is True:
            return
        await self.lights_toggle()

    async def lights_off(self) -> None:
        """Turn off lights, skipping the toggle when the bed already reports them off."""
        if await self._resolve_light_state() is False:
            return
        await self.lights_toggle()

    # Massage methods
    #
    # There is deliberately no ``massage_off`` override: massage power is a
    # single toggle keycode with no discrete off. ``supports_massage_off_control``
    # detects the capability by checking whether the subclass overrides
    # ``massage_off``, so overriding it just to raise NotImplementedError would
    # advertise a massage-off button that can only ever fail (issue #368).
    async def massage_head_up(self) -> None:
        """Increase head massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_HEAD_UP, "massage_head_up")

    async def massage_head_down(self) -> None:
        """Decrease head massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_HEAD_DOWN, "massage_head_down")

    async def massage_foot_up(self) -> None:
        """Increase foot massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_FOOT_UP, "massage_foot_up")

    async def massage_foot_down(self) -> None:
        """Decrease foot massage intensity."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_FOOT_DOWN, "massage_foot_down")

    async def massage_toggle(self) -> None:
        """Toggle massage / step through modes."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_STEP, "massage_toggle")

    async def massage_wave_step(self) -> None:
        """Step through massage wave patterns."""
        await self._tap_keycode(LeggettOkinCommands.MASSAGE_WAVE_STEP, "massage_wave_step")

    # Pillow motor control
    async def move_pillow_up(self) -> None:
        """Move pillow motor up."""
        await self._move_motor("pillow", MotorDirection.UP)

    async def move_pillow_down(self) -> None:
        """Move pillow motor down."""
        await self._move_motor("pillow", MotorDirection.DOWN)

    async def move_pillow_stop(self) -> None:
        """Stop pillow motor."""
        await self._move_motor(None, MotorDirection.STOP)

    # Lumbar motor control
    async def move_lumbar_up(self) -> None:
        """Move lumbar motor up."""
        await self._move_motor("lumbar", MotorDirection.UP)

    async def move_lumbar_down(self) -> None:
        """Move lumbar motor down."""
        await self._move_motor("lumbar", MotorDirection.DOWN)

    async def move_lumbar_stop(self) -> None:
        """Stop lumbar motor."""
        await self._move_motor(None, MotorDirection.STOP)
