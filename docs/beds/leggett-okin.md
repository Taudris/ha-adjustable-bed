# Leggett & Platt Okin (`leggett_okin`)

Control boxes sold as Leggett & Platt Prodigy Comfort Elite and similar, marked
`LP BED CONTROL` in BLE advertisements. Confirmed hardware: DewertOkin CU170.

Two vendor apps drive this hardware: **LP Control** (`com.leggett.android.universal`)
and the older, delisted **Prodigy CE** (`com.leggett.prodigy4`). Where they
disagree, this document follows the clean-room analysis of Prodigy CE 1.2.0 and
says so.

## Transport

| | |
|---|---|
| Service UUID | `62741523-52f9-8864-b1ab-3b3a8d65950b` |
| Write characteristic | `62741525-52f9-8864-b1ab-3b3a8d65950b` |
| Notify characteristic | `62741625-52f9-8864-b1ab-3b3a8d65950b` |
| Pairing | Required. Neither app calls `createBond`, but the write characteristic needs an encrypted link, so Android bonds reactively on the resulting ATT error 5. |
| Position feedback | None. See [Notifications](#notifications). |

### Two frame formats

The app picks its framing once per connection, purely on whether characteristic
`00001721-0000-1000-8000-00805f9b34fb` exists under the service:

| Characteristic present | Frame | Length |
|---|---|---|
| yes (revision 1) | `04 02 <keycode big-endian 32>` | 6 bytes |
| no (revision 0) | `E5 FE 16 <keycode big-endian 32> <checksum>` | 8 bytes |

The revision-0 checksum is `~sum(bytes[0..6])` truncated to 8 bits, giving the
invariant that all eight bytes sum to `0xFF`.

**The integration currently only emits the 6-byte revision-1 frame.** A
revision-0 control box would silently ignore every command. This is a known gap,
not a proven-absent case.

## Keycodes

Everything is a 32-bit keycode. The app keeps a bitmask of currently-held
buttons, so multiple simultaneous actions are one frame with several bits set.

> **Do not trust the `FBP_KEYCODE_*` constant names in decompiled output.**
> Several are demonstrably wrong for this hardware: `0x00800000` is declared
> `LIGHT_INTENSITY_DOWN` but the shipped massage screen binds it to head massage
> **down**. The layout binding and the write boundary are the authority. Both
> independent analyses of Prodigy CE reached this conclusion separately.

### Motors

| Action | Keycode |
|---|---|
| Head up / down | `0x00000001` / `0x00000002` |
| Feet up / down | `0x00000004` / `0x00000008` |
| Tilt (pillow) up / down | `0x00000010` / `0x00000020` |
| Lumbar up / down | `0x00000040` / `0x00000080` |
| Release / stop | `0x00000000` |

### Presets

| Action | Keycode | Kind |
|---|---|---|
| Memory 1 / Zero-G | `0x00001000` | recall |
| Memory 2 | `0x00002000` | recall |
| Memory 3 / Anti-snore | `0x00004000` | recall |
| Memory 4 | `0x00008000` | recall |
| Flat | `0x08000000` | recall (latched by the box) |
| Memory store (arm) | `0x00010000` | **not a recall** |

Zero-G and anti-snore are genuine aliases: the vendor apps ship those positions
pre-assigned to slots 1 and 3, and send the same keycode.

`0x00010000` arms the box to overwrite a slot. It must never appear in the
recall ladder. Earlier releases of this integration used a ladder shifted one
step up (`0x2000`…`0x10000`), so every memory button recalled its neighbour and
"Memory 4" streamed the store-arm keycode for 30 seconds - which could
reprogram a slot with whatever position the bed happened to be in.

### Massage and lights

| Action | Keycode |
|---|---|
| Head massage up / down | `0x00000800` / `0x00800000` |
| Foot massage up / down | `0x00000400` / `0x01000000` |
| Massage on/off (toggle) | `0x00000100` |
| Massage wave mode step | `0x10000000` |
| Under-bed light toggle | `0x00020000` |

Massage power is a **toggle** with no discrete off, so the integration exposes
no massage-off button.

There is no massage timer. `0x00000200` appears in the app as a constant
(`FBP_KEYCODE_M5_IN`, a fifth actuator channel) but is never bound to a control
and never written; it must not be reconstructed as a command.

## Timing and release semantics

This protocol treats held buttons and recalls very differently, and getting the
distinction wrong is the main way to break it.

**Held keycodes** (motors, light, massage) stream every ~100 ms while the
button is down. On release the app emits **exactly four** keycode-`0` frames and
then goes silent. There is no distinct stop opcode; the release frame is an
ordinary frame carrying zero.

**Recalls** (the memory slots and flat) are a **single frame** with **no
terminator at all**. The control box latches the keycode and drives the move to
completion by itself. Appending a release frame here risks cancelling the motion
the recall just started, so the integration deliberately does not.

Repeating the keycode is the same hazard. The box retriggers its hold watchdog
(~235 ms, measured) on every frame it receives; once that has expired, another
copy of the keycode is a **second press**, and a second press during preset
motion stops the bed. The integration used to send ten frames at ~100 ms here,
which on a link whose round trip measures ~180 ms with a tail past 250 ms was
ten chances to cancel the move its own first frame had started.

Because the box owns the motion, extra frames buy nothing anyway: the first
frame that lands does the work, and sending more cannot make the bed travel
further or longer. So delivery is **confirmed rather than repeated**. The box
answers every frame it receives with a status frame on the feedback
characteristic, and that answer is a receipt: one frame, wait for the receipt,
done. Only when no receipt arrives - the one case where no motion is in progress
to cancel - is another frame sent.

`motor_pulse_count` therefore bounds the number of **attempts** for a latched
command, not a burst length: how many unanswered frames it may spend before the
command reports failure (default 10 for this bed). `motor_pulse_delay_ms` does
not apply to recalls at all; a retry waits for the receipt window instead, which
is a property of the link rather than a user preference. Held keycodes are
unaffected: they are still wall-clock bounded and still paced by
`motor_pulse_delay_ms`.

A box that has never sent a status frame - one without the feedback
characteristic - is never retried, because its silence carries no information.
Such a box gets a single frame, exactly as the physical remote sends.

Flat looks like a held button in `com.leggett.prodigy4`, but only because that
app streams *every* key the same way and never special-cases flat. LP Control
2.11.0, a later app on the same wire protocol, settles it: its press-and-release
mode sends flat as a **single frame**, and its Okin `setPressAndHoldMode` is an
empty method, so it never tells the box which mode it is in. A shipped mode
that sends one flat frame to an unmodified box can only work if the box
latches. The physical remote emits one radio transmission for flat, and
`smartbed-mqtt` sends it as a single frame. The residual uncertainty is whether
a box left in the older app's press-and-hold mode still latches; nothing in
either app can answer that.

LP Control 2.9.0 uses a 200 ms cadence for held commands where Prodigy CE uses
100 ms. The integration defaults to 100 ms: a shorter refresh cannot fall
outside a keep-alive window that a longer one satisfies, and users on 200 ms
reported stuttering movement.

Every one of these intervals is a **target cadence**, not a sleep between
writes: frame *k* is scheduled at stream start + *k* × cadence, so the BLE
write round trip is absorbed into the interval instead of added to it. This
matters through a proxy: the control box halts motion when the inter-frame gap
exceeds roughly **235 ms**, and proxied write-with-response round trips of
105-300 ms plus a fixed sleep would push gaps past that window. When a round
trip overruns its tick the next frame is sent immediately.

The 235 ms figure is measured: streaming at an achieved gap of p50 232 / max
237 ms moved the bed smoothly apart from occasional stutter, so the box gives
up just under ~237 ms. An earlier "~300 ms" figure recorded here was measuring
when the bed *looked* stopped rather than when the hold broke.

**Reading the cadence back.** The gap between consecutive frames is the only
timing figure this hardware reacts to - the watchdog retriggers on the last
frame received, so drift from the schedule a stream started on says nothing
about whether the bed kept moving. A stream therefore measures the gaps
between its own packets: it warns once, on the first gap past the window, logs
one summary line when it ends, and files a bucketed histogram of the gaps under
`stream_cadence` in the support bundle. The buckets put 235 ms on an edge, so
"how many gaps could the box have noticed" is read off directly rather than
interpolated, and `breaches` states it outright.

Pacing applies to every repeated-frame path: motor movement, the memory-store
arm and slot holds, flat, the release burst and the recall trigger. That
mirrors the app, whose output thread is one unconditional 100 ms scheduler for
every key including releases and recalls. Single-frame taps (light, massage)
have no repeats, so they are unaffected either way.

The inter-frame interval becomes `max(cadence, round trip)` rather than
`cadence + round trip`, so on a link that keeps up with the cadence a burst of
*n* frames takes *n* × cadence; on a slower proxied link it is round-trip
bound, and never slower than the unpaced path. Two consequences beyond the
watchdog: a frame count derived from a hold duration (memory programming, flat)
runs to that duration instead of stretching well past it, and the bursts that
only *trigger* something (release, recall) stop holding the command lock for
whole seconds of pure sleep.

## Memory programming

There is no program opcode. Storing a position is two held keycodes in
sequence:

1. stream `0x00010000` for ~5 s
2. switch **directly** to the slot keycode for ~2 s - the app swaps its key
   buffer within one 100 ms tick, so no release frames separate the stages
3. release once: the normal four keycode-`0` frames

This is decompile-derived (`MainActivityBase.onPositionSetMessage`) and not yet
verified on hardware. The shipped user guide describes the same two stages for
the hand control - "Touch Save… the massage motors will buzz once. Within 5
seconds, touch the Favorite Position being edited." - which implies the box's
store window survives a release between the stages; the integration still
mirrors the app's byte stream exactly.

## Notifications

The notify characteristic carries an LED/status bitmask, not positions. The
vendor app parses it into exactly two live indicators - sleep timer (`0x8000`)
and alarm (`0x4000`) - and no parsed value ever influences a later command.

There is **no position, angle, percentage, motor-state or error feedback of any
kind** in either app. Under-bed light state is *not* among the bits either app
reads, so the integration exposes the light as a blind toggle. Users have
reported that the physical remote does show light state; confirming that would
need a BLE capture of the notify characteristic while toggling the light.

## Provenance

Command values, framing, timing and release semantics come from a clean-room
analysis of `com.leggett.prodigy4` 1.2.0 (versionCode 18, artifact SHA-256
`f32978d8…`), traced from layout binding to the GATT write boundary. Two
independent analyst runs agreed on every value recorded here. The flat latching
finding comes from a second app on the same wire protocol, LP Control 2.11.0
(`com.leggett.lpbtsuitesdk.controlbox.OkinControlBoxInterface`).

The per-frame status receipt, the hold watchdog and the second-press
cancellation were measured on hardware (CU170 over an ESPHome proxy): a recall
sent as a repeated burst was observed cancelling its own motion, and the
physical remote reproduced the same stop with a second press mid-preset.

Unverified against hardware, and worth a capture if you have the equipment:
which frame revision real units use, whether a single latched flat frame
flattens fully on a box left in the older app's press-and-hold mode, and whether
the `0x08010000` chord resets memory to factory defaults as the vendor guide
states.
