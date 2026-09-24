# Leggett & Platt Okin (`leggett_okin`)

Control boxes sold as Leggett & Platt Prodigy Comfort Elite and similar, marked
`LP BED CONTROL` in BLE advertisements. Confirmed hardware: DewertOkin CU170.

Select the app profile matching the bed's remote application. The default remains
**Prodigy CE / Prodigy 4**, preserving existing configurations. The accepted
APK Protocol Audit cluster-005 reports establish these distinct BLE control surfaces:

| Profile | Accepted package/version | Movement axes | Direct memories |
|---|---|---|---|
| Prodigy 2L | `com.leggett.prodigy2L` 1.2 (15) | Head, foot, lumbar | Four favorites, including fixed Snore |
| Prodigy 2 | `com.leggett.prodigy2` 2.2.0 (44) | Head, foot, pillow | Four favorites, including fixed Snore |
| Prodigy CE / Prodigy 4 | `com.leggett.prodigy4` 1.2.0 (18) | Head, foot, pillow, lumbar | Four favorites, including fixed Snore |
| U / Ultra Series | `com.leggett.useries` 2.1 (16) | Head, foot, pillow | Two held memory controls and separate held SET |

App profile and wire revision are independent. Device names and shared service
UUIDs cannot establish the correct physical actuator layout. Configure each side
before combining two separately addressed beds; shared paired options preserve
each side's app profile. Unpair temporarily to change that setting.

The new profiles are statically verified and hardware unverified. The CU170
pairing and unconfirmed-write policies below come from existing hardware testing,
not from APK calls that explicitly select bonding or ATT write mode. This
integration supports BLE; Classic RFCOMM paths found in Prodigy 2 and U Series
remain outside its transport support.

## Transport

| | |
|---|---|
| Service UUID | `62741523-52f9-8864-b1ab-3b3a8d65950b` |
| Write characteristic | `62741525-52f9-8864-b1ab-3b3a8d65950b` (accepts unconfirmed writes) |
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

The integration applies this same characteristic check after service discovery
and records the selected revision in protocol diagnostics. Writes require a
resolved revision from the current connection's GATT service; unavailable service
discovery does not justify guessing either encoding.

## Keycodes

Ordinary controls use a 32-bit keycode. The app keeps a bitmask of currently-held
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
| Favorite 1 | `0x00001000` | editable recall |
| Favorite 2 | `0x00002000` | editable recall |
| Snore | `0x00004000` | fixed recall |
| Favorite 3 | `0x00008000` | editable recall |
| Flat | `0x08000000` | **held button**, not a recall |
| Memory store (arm) | `0x00010000` | **not a recall** |

The three Prodigy profiles initialize Favorite 1, Favorite 2 and Favorite 3 as editable entries.
It initializes the third wire slot as the fixed Snore entry. The integration
therefore exposes no separate Zero-G action, and never offers or accepts a save
operation for the Snore slot.

U Series instead exposes two held memory keys, a held Snore control and a held
SET key. It does not inherit the newer apps' composite favorite-programming
sequence or their four direct favorite entries. Its sleep timer independently
offers a third memory action.

**Reaching a preset.** On this bed a preset is held rather than activated: the
card renders each preset and memory tile as a hold tile, so the bed travels
while you hold the tile down and a tap is a short hold. The automation's door is
`adjustable_bed.goto_preset`, which takes a memory slot number or the name of a
fixed position — `flat` or `dummy`. Its optional `duration_ms` says how long to
hold the key; leave it out for the shortest press the box registers, which is
what a box in latch mode needs, because there the down edge latches the whole
travel and the release is a no-op.

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
no massage-off button. The wave keycode is exposed as the massage mode-step
button.

There is no massage timer. `0x00000200` appears in the app as a constant
(`FBP_KEYCODE_M5_IN`, a fifth actuator channel) but is never bound to a control
and never written; it must not be reconstructed as a command.

## Timing and release semantics

The control box reads one frame as the OR of every key held down, so every
command on this bed is a key held for as long as it is wanted. The integration
streams one frame every 100 ms carrying the whole set, and ends the stream with
**one** keycode-`0` frame written as a confirmed write. There is no distinct
stop opcode; the release frame is an ordinary frame carrying zero.

CU170 hardware testing measured a 217-218 ms motion watchdog: the box carries a
key that long past the frame asserting it, and a wider gap is a release. The
stream's frames are Write Commands, unconfirmed and paced from the start of each
write, so no BLE round trip rides in the gap. Awaiting a confirmed write and then
sleeping 100 ms repeatedly crosses the watchdog over a WiFi Bluetooth proxy and
makes the motor stop and restart. The exceptions are the barriers, described
later in this section, and the release: each goes as a confirmed write, and
nothing waits on the release's completion.

Explicit **Stop all** drops every bit, writes that one release, and then presses
the keycode `0x00040000` that has no function of its own. That press is what
interrupts a latched preset or preset-save sequence, which zero alone does not
stop; the
[September hardware report](https://github.com/kristofferR/ha-adjustable-bed/issues/368#issuecomment-5747783066)
confirmed it on a CU170.

How far the stream may run ahead of the box is the box's own answer: it
acknowledges each frame it receives with a status notification, and the
integration spends one of eight credits per frame and takes one back per
acknowledgement. At zero credit the next frame goes as a barrier: a confirmed
write that nothing follows until it completes. Acknowledgements are positive
evidence only: their absence advances nothing. When ten frames stand
unacknowledged, net of one forgiven per second, the next frame also goes as a
barrier. Any confirmed write's completion, a barrier's or the release's, proves
every earlier frame reached the box, so credit refills, the unacknowledged count
clears, and the stream goes on. If a barrier fails, the stream and the link end.

**A preset is a held key too**, not a burst. In hold mode the box travels while
the key is down and stops at the release; in latch mode the box latches the
whole travel at the press and the release is a no-op. One press shape serves
both modes, so nothing here prices a recall's length.

LP Control 2.9.0 uses a 200 ms cadence for held commands where Prodigy CE uses
100 ms. The integration follows the 100 ms one: a shorter refresh cannot fall
outside a keep-alive window that a longer one satisfies, and users on 200 ms
reported stuttering movement.

U Series memory controls have no one-shot preset buttons in this profile. Use
`adjustable_bed.leggett_hold_control` with an explicit duration for Memory 1,
Memory 2 and Snore there. That action also exposes bounded holds for flat,
Snore, lighting and massage buttons on every profile, and U Series adds SET; it
writes through the command path rather than the streamed set.

## Memory programming

There is no program opcode. Storing a position is two held keys the box
acknowledges one at a time, by flashing the under-bed light:

1. hold `0x00010000` alone until the box answers with **one** light pulse, about
   1-2 s in. It arms only while that key is held on its own, so nothing else
   may ride the frame.
2. drop the key: the set goes empty and one keycode-`0` frame ends the stage.
   The next stage begins one clear floor, 223 ms, after that edge.
3. hold the slot keycode until the box answers with **three** pulses, which is
   the save.

Each stage fails at a 5.5 s ceiling rather than advancing on elapsed time. A
slot key against a box that never armed is a plain recall, so a lost cue has to
fail the store rather than move the bed. A stage that ends without the saved cue
- a ceiling, a stop, a preemption - is followed by one press of `0x00040000`,
the key with no function of its own: the box counts it as a button, and a button
clears a pending store. On a Prodigy CE bed, the profile the key is confirmed
on, the bed-wide stop presses it for the same reason. A connect presses nothing:
in latch mode any press during an autonomous travel is taken as that travel's
stop, so a press the integration sends on reconnect can cut short a travel the
hand control started.

The shipped user guide describes the same two stages for the hand control:
"Touch Save… the massage motors will buzz once. Within 5 seconds, touch the
Favorite Position being edited."

U Series has only the standalone held SET path, available through
`leggett_hold_control`.

## Notifications

The notify characteristic acknowledges accepted writes and also emits status
updates for physical-remote actions. The vendor app reconstructs a generic
LED/status bitmask from operations 6, 7, 8, 9, and 11, but only assigns UI
meaning to sleep timer (`0x8000`) and alarm (`0x4000`). No parsed value ever
influences a later command.

There is **no position, angle, percentage or motor-state feedback**. The
[CU170 hardware report in #368](https://github.com/kristofferR/ha-adjustable-bed/issues/368#issuecomment-5481326088)
provides the physical meaning absent from the app analysis. In the Prodigy CE
profile, the integration recognizes a 20-byte status frame on the main status
characteristic: `09 0B`, two identical big-endian status dwords, `FF`, and nine
uninterpreted trailing bytes. Invalid length, footer or duplicated fields are
ignored. Other app profiles retain their artifact-derived parser.

| CU170 status mask | Meaning |
|---|---|
| `0x00020000` | Under-bed light on |
| `0x00400000` | Alarm armed |
| `0x00800000` | Sleep timer armed |

The receipt for a toggle contains the **old** light state. A separate spontaneous
notification contains the **new** state, including changes from the physical
remote. Every valid notification is processed; notifications are not counted as
one receipt per write. Alarm and sleep indicators use the hardware masks after
a CU170 status frame is recognized, rather than the app's `0x4000`/`0x8000`
labels. CSS settings notifications cannot overwrite this hardware status.

Prodigy CE exposes an under-bed **light entity** backed by those notifications.
On/off actions only toggle when the known state differs from the requested
state, then wait up to three seconds for confirmation. A missing confirmation
leaves the light unknown and reports an error without retrying the toggle.
State is not restored from an earlier Home Assistant session and is cleared on
disconnect. After subscribing, startup sends the app's four idle zero frames
to request live status without toggling the light. The September hardware
report confirms that a keycode write refreshes status. If no valid reply arrives,
state stays unknown. The redundant **Toggle Light** button is removed from the
entity registry; `light.toggle` remains available even before the first status
update, as does the physical remote.

An authentication failure during connection clears the cached bond marker and
allows automatic pairing retries. A successful verified retry suppresses the
pairing repair entirely. Failed, inconclusive or cancelled recovery still raises
the repair, and command-time authentication errors remain immediately visible.

Light and massage taps now leave one 100 ms command interval before the release
burst. Previously the first zero could follow the press immediately, making
the light toggle too short to register. This uses the accepted app cadence;
the precise minimum hardware debounce interval is not established.

The integration also retains raw LED-mask and signed-status sensors for the
app parser, and reads available standard Device Information strings for
diagnostics. CU170 frames have no decoded signed-status field. Other profiles
retain the app's alarm and sleep masks; U Series applies its app's low-byte
suppression while preserving the raw mask.

### Remaining hardware observations

The reporter measured one light pulse when memory storage arms and three when
a preset is saved. Those pulses also affect the real light state. Programming
continues to use the accepted app's bounded five-second arm and two-second slot
sequence. An acknowledgement-driven optimization needs paired raw traces of
both initial light states to distinguish the initial dark interval, pulses,
and final restoration safely. It must never combine SET with FLAT, the reported
factory-reset chord. The existing store path sends each key separately.

The CU170 can be configured for latched or hold-required presets. Memory recalls
use the app's short recall burst; `adjustable_bed.leggett_hold_control` supplies
an explicit held duration for boxes in hold-required mode. Flat retains its
bounded held-command behavior. Hardware validation of this change remains for
users after a beta or release; maintainers do not need to acquire a bed.

## Sleep and alarm timers

`adjustable_bed.leggett_sleep_timer` and `adjustable_bed.leggett_alarm_timer`
start or cancel native timers. Prodigy sleep
selects a favorite and a delay of 1–1439 minutes. Its sleep command is a compact
six-byte packet under either wire revision. U Series sleep selects flat or
memory 1–3 and uses 15-minute steps through 90 minutes; its timer key follows the
selected R0/R1 encoding. All profiles support an alarm delay of 1–1440 minutes.
U Series has a different alarm-stop key from the Prodigy profiles.

The services validate the selected profile's actions and ranges before writing.
See their Home Assistant action descriptions for field names and cancellation.

## Control mode

The three Prodigy profiles expose two persistent control-box settings. U Series
does not expose these settings or initialize the optional CSS channel. Each
setting is one held chord the box acknowledges with its own count of light
pulses, and each fails at a 7 s ceiling if that answer never comes:

| Mode | Keycode | Answer |
|---|---|---|
| Press-and-hold | `0x08010000` | 2 pulses |
| Press-and-release | `0x01800000` | 4 pulses |

**Prodigy CE exception:** #368 reports that SET+FLAT (`0x08010000`) factory-resets
the CU170 and wipes its saved presets. For this profile, Home Assistant removes
the press-and-hold configuration button and refuses the command even when
called through an old entity. Use the physical remote's documented procedure
for mode changes. The press-and-release button remains available.

For the other Prodigy profiles, Home Assistant exposes both configuration
buttons rather than a select,
because neither notification channel reports the currently active mode. On
boxes with the optional Smart Remote CSS service, notification setup also sends
the app's raw `01 02` initialization write.

**We recommend neither mode, and their consequences are asymmetric.**
Press-and-hold restores the box's factory defaults, which is what restores hold
mode, and it wipes the stored favorites with them. Press-and-release is one-way:
the only route back is that reset.

Latch mode also changes two things that reach anything built on the box. A press
arriving while the box runs an autonomous travel is consumed as that travel's
stop and does nothing else, so a light toggle sent during a preset travel stops
the bed and does not toggle the light. And a held key's release is a no-op: the
travel the press latched runs to its end whatever the frames say afterwards.

## Provenance

Command values, framing, timing, notification parsing and release semantics
come from the accepted APK Protocol Audit clean-room analysis of `com.leggett.prodigy4`
1.2.0 (versionCode 18, artifact SHA-256 `45922c518c9e8070…`), traced from layout
binding to the GATT boundary and independently audited. The report is COMPLETE;
the hardware status mapping above supplements that frozen app evidence and does
not modify or replace the accepted analysis. The [whole-cluster disposition](leggett-app-disposition.md)
records all four accepted report identities, previously implemented behavior,
remaining findings, exact app differences and transport exclusions.

CU170 hardware testing has since settled the release semantics, the motion
watchdog, the light bit in the notification mask, and the pulse count each
staged gesture is acknowledged by.

Unverified against hardware, and worth a capture if you have the equipment:
which frame revision real units use, and whether changing control mode also
resets editable favorites on all firmware.
