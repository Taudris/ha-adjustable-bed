# Change inventory: card-hold-samples

Tier 0 extraction pass. Range `ce8296d..d7a51f7`, 9 commits, which is the range the
tier-1 review read. Commits after it are that review's fixes and are not covered here:

| Commit | Subject |
|---|---|
| `7ad0458` | refactor: press ownership as its own primitive |
| `50fcbd8` | feat: the card's intent sender |
| `bb261ba` | feat: the card's sample-set hold gesture |
| `7f66035` | feat: discovery carries each control's roster name |
| `ec9102c` | feat: the card emits intent samples for hold-capable controls |
| `03601e6` | build: rebuild the card bundle |
| `cec01e5` | feat: goto_preset accepts the flat and dummy presets |
| `e6138b1` | feat: entities publish their hold control |
| `d7a51f7` | docs: the card's hold gesture |

`custom_components/adjustable_bed/frontend/dist/` is rebuilt output (commit `03601e6`, from
`bun install && bun run build`); its contents are not inventoried below.

No judgment, no recommendations. Every claim below is either read directly off the diff or
traced to the surrounding file.

## 1. Public surface, added

### TypeScript modules

| Module | File |
|---|---|
| `intents.ts` | `custom_components/adjustable_bed/frontend/src/intents.ts` (new) |
| `intent-hold.ts` | `custom_components/adjustable_bed/frontend/src/intent-hold.ts` (new) |
| `press-ownership.ts` | `custom_components/adjustable_bed/frontend/src/press-ownership.ts` (new) |

### TypeScript types, classes and constants

| Name | Kind | File:line |
|---|---|---|
| `ControlId` | type alias, `string` | `intents.ts:10` |
| `IntentId` | type alias, `string` | `intents.ts:12` |
| `IntentSample` | interface; fields `intent_id: IntentId`, `control: ControlId`, `action: "hold"`, `ttl_ms: number` | `intents.ts:14-22` |
| `IntentSampleSet` | interface; fields `sender_id: string`, `seq: number`, `samples: IntentSample[]` | `intents.ts:24-28` |
| `IntentSenderDeps` | interface; fields `send`, `schedule`, `mintId`, `now` | `intents.ts:30-38` |
| `REFRESH_MS` | `const`, `250` | `intents.ts:42` |
| `TTL_MS` | `const`, `750` | `intents.ts:48` |
| `MIN_PRESS_MS` | `const`, `200` | `intents.ts:55` |
| `ROLL_MARGIN_MS` | `const`, `500` | `intents.ts:60` |
| `mintIntentId` | function, `() => string` | `intents.ts:66-69` |
| `LiveIntent` | class, module-private (not exported); fields `retiring: IntentId \| null`, `cancelPendingRelease: (() => void) \| null`, constructor params `control`, `ttlMaxMs`, `floorAt`, `intentId`, `began` | `intents.ts:73-89` |
| `IntentHandle` | type alias, `LiveIntent` (opaque to callers outside the module) | `intents.ts:93` |
| `IntentSender` | class | `intents.ts:95` |
| `IntentHold` | class | `intent-hold.ts:11` |
| `PressOwnership` | class | `press-ownership.ts:6` |
| `HoldControl` | interface; fields `control: ControlId`, `ttlMaxMs: number` | `types.ts:92-95` |
| `PresetEntity` | interface; fields `id: string`, `hold?: HoldControl` | `types.ts:111-114` |

### TypeScript methods

| Signature | File:line |
|---|---|
| `IntentSender.constructor(deps: IntentSenderDeps)` | `intents.ts:101-103` |
| `IntentSender.holding` (getter, `boolean`) | `intents.ts:105-107` |
| `IntentSender.hold(control: ControlId, ttlMaxMs: number) -> IntentHandle` | `intents.ts:112-125` |
| `IntentSender.release(handle: IntentHandle) -> void` | `intents.ts:130-140` |
| `IntentSender.drop(handle: IntentHandle) -> void` | `intents.ts:144-148` |
| `IntentSender.abandon() -> void` | `intents.ts:153-162` |
| `IntentSender._end(handle: LiveIntent) -> void` (private) | `intents.ts:166-174` |
| `IntentSender._refresh() -> void` (private) | `intents.ts:176-181` |
| `IntentSender._roll() -> void` (private) | `intents.ts:186-194` |
| `IntentSender._armRefresh() -> void` (private) | `intents.ts:196-199` |
| `IntentSender._cancelRefreshTimer() -> void` (private) | `intents.ts:201-204` |
| `IntentSender._send(ending?: ReadonlySet<LiveIntent>) -> void` (private) | `intents.ts:209-234` |
| `IntentHold.constructor(sender: () => IntentSender \| null, stopBed: () => void)` | `intent-hold.ts:16-18` |
| `IntentHold.heldKey` (getter, `string \| null`) | `intent-hold.ts:20-22` |
| `IntentHold.start(control: ControlId, key: string, ttlMaxMs: number, pointerId: number \| null) -> void` | `intent-hold.ts:28-37` |
| `IntentHold.endFromPointer(key: string, pointerId: number, isPrimaryButtonRelease: boolean) -> void` | `intent-hold.ts:41-47` |
| `IntentHold.end(key: string) -> void` | `intent-hold.ts:52-58` |
| `IntentHold.cancel(key: string) -> boolean` | `intent-hold.ts:61-67` |
| `IntentHold.stopAll() -> void` | `intent-hold.ts:71-78` |
| `IntentHold.abandon() -> void` | `intent-hold.ts:82-86` |
| `PressOwnership.heldKey` (getter, `string \| null`) | `press-ownership.ts:17-19` |
| `PressOwnership.claim(key: string, pointerId: number \| null) -> boolean` | `press-ownership.ts:24-29` |
| `PressOwnership.mayEnd(key: string, pointerId: number, isPrimaryButtonRelease: boolean) -> boolean` | `press-ownership.ts:33-41` |
| `PressOwnership.release(key: string) -> boolean` | `press-ownership.ts:46-50` |
| `PressOwnership.clear() -> void` | `press-ownership.ts:54-57` |
| `AdjustableBedCard._stopBed() -> void` (private) | `adjustable-bed-card.ts:725-727` |
| `AdjustableBedCard._senderForDevice() -> IntentSender \| null` (private) | `adjustable-bed-card.ts:732-753` |
| `AdjustableBedCard._pressOwner(e) -> { pointerId: number \| null } \| null` (private) | `adjustable-bed-card.ts:759-778` |
| `AdjustableBedCard._startTileHold(e, entityId, hold) -> void` (private) | `adjustable-bed-card.ts:798-806` |
| `AdjustableBedCard._tapWithoutPointer(e, entityId, hold) -> void` (private) | `adjustable-bed-card.ts:810-818` |
| `AdjustableBedCard._endTilePointerHold(e, entityId) -> void` (private) | `adjustable-bed-card.ts:820-826` |
| `AdjustableBedCard._endTileKeyHold(e, entityId) -> void` (private) | `adjustable-bed-card.ts:828-831` |
| `AdjustableBedCard._holdTile(entityId: string, hold: HoldControl) -> TemplateResult` (private) | `adjustable-bed-card.ts:521-538` |
| `AdjustableBedCard._stopAll() -> void` (private) | `adjustable-bed-card.ts:901-907` |

### TypeScript fields (existing classes, new members)

| Member | Kind | File:line |
|---|---|---|
| `AdjustableBedCard._sender` | private field, `IntentSender \| null` | `adjustable-bed-card.ts:52` |
| `AdjustableBedCard._intentHold` | private readonly field, `IntentHold` | `adjustable-bed-card.ts:57-60` |
| `MotorEntity.holdUp` | new optional field, `HoldControl \| undefined` | `types.ts:106` |
| `MotorEntity.holdDown` | new optional field, `HoldControl \| undefined` | `types.ts:107` |
| `MemorySlot.gotoHold` | new optional field, `HoldControl \| undefined` | `types.ts:122` |

### Python functions and methods

| Signature | File:line |
|---|---|
| `_preset_control_name(description: AdjustableBedButtonEntityDescription) -> str \| None` | `button.py:735-749` |
| `AdjustableBedButton.extra_state_attributes` (property, `dict[str, Any] \| None`) | `button.py:786-804` |
| `AdjustableBedCover.extra_state_attributes` (property, `dict[str, Any] \| None`) | `cover.py:317-337` |
| `_goto_memory_slot(coordinator, slot: int, duration_ms: int \| None) -> None` (async) | `services.py:340-375` |
| `_goto_named_preset(coordinator, name: str, duration_ms: int \| None) -> None` (async) | `services.py:378-400` |

### Python constants and entity attributes

| Name | Value | File:line |
|---|---|---|
| `NAMED_PRESETS` | `("flat", "dummy")` | `services.py:118` |
| `AdjustableBedButton._unrecorded_attributes` | `frozenset({"hold_control", "hold_ttl_max_ms"})` | `button.py:758` |
| `AdjustableBedCover._unrecorded_attributes` | `frozenset({"hold_control_up", "hold_control_down", "hold_ttl_max_ms"})` | `cover.py:282-284` |

New published entity attributes (via the two `extra_state_attributes` properties above): `hold_control`, `hold_control_up`, `hold_control_down`, `hold_ttl_max_ms`.

### Translation / string keys

| Key path | File |
|---|---|
| `exceptions.preset_name_not_supported` | `strings.json:932-934`, `translations/en.json:937-939` |

### Test files

| File | Status | New/changed test functions |
|---|---|---|
| `custom_components/adjustable_bed/frontend/src/intents.test.ts` | new | 18 |
| `custom_components/adjustable_bed/frontend/src/intent-hold.test.ts` | new | 16 |
| `custom_components/adjustable_bed/frontend/src/press-ownership.test.ts` | new | 8 |
| `custom_components/adjustable_bed/frontend/src/discovery.test.ts` | changed | 4 new, 1 changed |
| `tests/test_entities.py` | changed | 4 new (one new class, `TestPublishedHoldControls`) |
| `tests/test_hold_service.py` | changed | 5 new |

(Function-level detail in §6.)

### Documentation (non-code)

| File | What changed |
|---|---|
| `AGENTS.md` | Key-modules list gains entries for `intents.ts`, `intent-hold.ts`, `press-ownership.ts`, and augments the existing `discovery.ts`/`adjustable-bed-card.ts`/`hold.ts` entries. `bun test` comment line names the added suites. |
| `README.md` | Memory paragraph gains a sentence on hold-capable tiles and states `goto_preset` accepts `flat`/`dummy` by name. |
| `docs/beds/leggett-okin.md` | New "Reaching a preset" paragraph in the Presets section. |

## 2. Public surface, changed or widened

| Member | Before | After | File:line | Commit |
|---|---|---|---|---|
| `preset_control_name(preset)` | `preset_control_name(slot: int) -> str`, returns `f"preset-{slot}"` | `preset_control_name(preset: int \| str) -> str`, same format string, now accepts a slot number or a bed's own name for a fixed position | `hold_roster.py:147-153` | `cec01e5` |
| `handle_goto_preset(call: ServiceCall) -> None` | Validated `preset: int` against `supports_memory_presets`/`memory_slot_count` inline, then either submitted a hold (`HoldCapable`) or called `ctrl.preset_memory(p)`. Log line used `"preset=%d"`. | Reads `preset: int \| str`; dispatches to `_goto_memory_slot` (`isinstance(preset, int)`) or `_goto_named_preset` (else); the two memory-slot validations moved into `_goto_memory_slot` unchanged. Log line now `"preset=%s"`. | `services.py:403-424` | `cec01e5` |
| `goto_preset` service schema, field `preset` (`ATTR_PRESET`) | `vol.All(vol.Coerce(int), vol.Range(min=1))` | `vol.Any(vol.All(vol.Coerce(int), vol.Range(min=1)), vol.In(NAMED_PRESETS))` — integer branch tried first | `services.py:1103-1109` | `cec01e5` |
| `goto_preset` service field `preset`, `services.yaml` | `name: Preset Number`; `description: Memory slot number (1-6, device-dependent).`; `selector: number: {min: 1, max: 6, mode: box}` | `name: Preset`; `description:` names the slot-number-or-name contract; `selector: text:` (no min/max/mode) | `services.yaml:13-19` | `cec01e5` |
| `goto_preset` field `preset`, `strings.json` / `translations/en.json` | `name: "Preset Number"`, `description: "Memory slot number (1-4)."` | `name: "Preset"`, `description:` names the slot-number-or-name contract | `strings.json:777-780`, `translations/en.json:779-782` | `cec01e5` |
| `AdjustableBedCover` (entity) | Published no `extra_state_attributes` and no `_unrecorded_attributes`. | Publishes `hold_control_up`, `hold_control_down`, `hold_ttl_max_ms` when the roster declares both directions of the cover's motor; publishes nothing otherwise. Declares the three names `_unrecorded_attributes`. | `cover.py:282-284`, `317-337` | `e6138b1` |
| `AdjustableBedButton` (entity) | Published no `extra_state_attributes` and no `_unrecorded_attributes`. | Publishes `hold_control`, `hold_ttl_max_ms` when `_preset_control_name` resolves and the roster declares that control; publishes nothing for a save (program) button or an unresolved name. Declares the two names `_unrecorded_attributes`. | `button.py:758`, `786-804` | `e6138b1` |
| `discovery.ts` — `bedEntitiesForDevice` output shape | `BedEntities.presets: string[]` (bare entity ids); a `cover`/direction-button case recorded only `m.cover`/`m[dir]`. | `BedEntities.presets: PresetEntity[]` (`{id, hold?}`); the `cover` and `(.+)_(up\|down)` button cases additionally call `recordMotorHold`, and the preset/`preset_memory_N` button cases additionally call `holdControl` to populate `hold`/`gotoHold`. | `discovery.ts:127-241`, `types.ts:138` | `7f66035` |
| `types.ts` — `BedEntities.presets` | `string[]` | `PresetEntity[]` | `types.ts:138` | `7f66035` |
| `MotorHold` (class) | Owned `_key: string \| null` and `_pointerId: number \| null` directly; `start`/`endFromPointer`/`cancel`/`abandon`/`_reset` read/wrote those fields. Public contract (method signatures, `heldKey`) unchanged before and after. | Delegates ownership tracking to a `PressOwnership` instance (`_ownership`); same method signatures and `heldKey` getter, internals only. `hold.test.ts` is untouched by this diff, evidencing no behavior change. | `hold.ts:9, 25, 34, 40, 80, 103, 125, 133` | `7ad0458` |
| `adjustable-bed-card.ts` — `_startHold(e, m, dir)` (motor row press) | Inlined the pointer/keyboard filtering that is now `_pressOwner`; unconditionally called `this._hold.start(m, dir, ownerPointerId)`. | Calls `_pressOwner(e)`; if `dir`'s `HoldControl` is present on `m`, routes to `this._intentHold.start(...)` and returns; otherwise falls through to `this._hold.start(...)` unchanged. | `adjustable-bed-card.ts:783-796` | `ec9102c` |
| `adjustable-bed-card.ts` — `_activateWithoutPointer(e, m, dir)` (tap-without-pointer) | Guard was `if (e.detail !== 0 \|\| this._hold.heldKey !== null) return;`, then checked `m.cover`. | Guard split: `if (e.detail !== 0) return;` first; if `dir`'s `HoldControl` is present, delegates to `_tapWithoutPointer` and returns; otherwise the original `this._hold.heldKey !== null` guard and `m.cover` branch run unchanged. | `adjustable-bed-card.ts:839-857` | `ec9102c` |
| `adjustable-bed-card.ts` — `_endPointerHold(e, m)` | Called `this._hold.endFromPointer(m, e.pointerId, isPrimaryRelease)` unconditionally. | Routes to `this._intentHold.endFromPointer(...)` when `this._intentHold.heldKey === m.key`, else falls through to `this._hold.endFromPointer(...)`. | `adjustable-bed-card.ts:862-871` | `ec9102c` |
| `adjustable-bed-card.ts` — `_endKeyHold(e, m)` | Called `this._hold.end(m)`. | Calls `this._endHold(m)` (the new routing wrapper). | `adjustable-bed-card.ts:873-876` | `ec9102c` |
| `adjustable-bed-card.ts` — `_endHold(m)` | Called `this._hold.end(m)` unconditionally. | Routes to `this._intentHold.end(m.key)` when `this._intentHold.heldKey === m.key`, else `this._hold.end(m)`. | `adjustable-bed-card.ts:878-884` | `ec9102c` |
| `adjustable-bed-card.ts` — `_motorStop(m)` | Cover branch called `this._hold.cancel(m)`; button-backed branch called `this._hold.stopAll()` directly. | Cover branch routes `cancel` to `this._intentHold`/`this._hold` by `heldKey`; button-backed branch calls the new `this._stopAll()` wrapper. | `adjustable-bed-card.ts:886-897` | `ec9102c` |
| `adjustable-bed-card.ts` — `MotorHold` actions object, `stopBed` | Inline arrow: `() => { if (this._bed?.stop) this._press(this._bed.stop); }` | `() => this._stopBed()` (extracted method, also called from `IntentHold`'s constructor argument) | `adjustable-bed-card.ts:79` (call site); method at `725-727` | `ec9102c` |
| `adjustable-bed-card.ts` — `setConfig(config)` | Set `this._config = config` only. | First checks `this._sender && config.device_id !== this._config?.device_id`; if true, calls `this._intentHold.abandon()` and sets `this._sender = null`, before assigning `this._config = config`. | `adjustable-bed-card.ts:95-104` | `ec9102c` |
| `adjustable-bed-card.ts` — `disconnectedCallback()` | Called `this._hold.abandon()`. | Also calls `this._intentHold.abandon()`. | `adjustable-bed-card.ts:110-118` | `ec9102c` |
| `adjustable-bed-card.ts` — stop-all button `@click` | `() => this._hold.stopAll()` | `() => this._stopAll()` | `adjustable-bed-card.ts:276` | `ec9102c` |
| `adjustable-bed-card.ts` — `_presets(bed)` | Rendered every preset as `this._tile(id, () => this._press(id))`. | Renders `this._holdTile(p.id, p.hold)` when `p.hold` is set, else the original tile/`_press` path. | `adjustable-bed-card.ts:346-358` | `ec9102c` |
| `adjustable-bed-card.ts` — `_memoryTile(slot)` | (Outside save mode) computed `canRecall` and rendered a plain/disabled tile. | Adds `if (slot.gotoHold) return this._holdTile(slot.goto!, slot.gotoHold);` ahead of the `canRecall` computation. | `adjustable-bed-card.ts:406-438` (guard at `424`) | `ec9102c` |
| `adjustable-bed-card.ts` — `_collectWatched(bed)` | `bed.presets.forEach((x) => ids.add(x));` | `bed.presets.forEach((p) => ids.add(p.id));` | `adjustable-bed-card.ts:688-720` area | `7f66035` |
| CSS — `.tile` rules | No `.hold-tile` variant. | New `.tile.hold-tile { touch-action: none; }` rule, with a comment explaining the touch-gesture-arbitration reason. | `adjustable-bed-card.ts:1191-1198` | `ec9102c` |

## 3. Removed

| Item | Replaced by | File:line | Commit |
|---|---|---|---|
| `MotorHold._key: string \| null` (private field) | `PressOwnership._key` (held via `MotorHold._ownership`) | was `hold.ts:26` before this diff (`git show 7ad0458~1:.../hold.ts`) | `7ad0458` |
| `MotorHold._pointerId: number \| null` (private field) | `PressOwnership._pointerId` (held via `MotorHold._ownership`) | was `hold.ts:31` before this diff (`git show 7ad0458~1:.../hold.ts`) | `7ad0458` |
| `goto_preset` `preset` field's `number` selector (`min: 1`, `max: 6`, `mode: box`) | a bare `text:` selector | `services.yaml:13-19` | `cec01e5` |

No module, class, function, method, constant, service, translation key, or test function was
deleted outright; every other `-` line in the diff is part of an in-place restructuring already
captured in §2 (`handle_goto_preset`'s validation moving into `_goto_memory_slot`, `MotorHold`'s
ownership fields moving into `PressOwnership`, the card's routing methods gaining branches).

## 4. Decisions the code introduces

### hold_roster.py — commit `cec01e5`

- **Signature widening, `preset_control_name`** (`hold_roster.py:147-153`): parameter type
  changes from `int` to `int | str`; the format string (`f"preset-{preset}"`) is unchanged, so an
  integer call site behaves identically.

### services.py — commit `cec01e5`

- **Constant**: `NAMED_PRESETS = ("flat", "dummy")` (`services.py:118`) — the two preset names
  `goto_preset`'s schema accepts alongside a slot number.
- **Guard, `_goto_memory_slot`** (`services.py:340-375`): raises `ServiceValidationError`
  (`translation_key="memory_presets_not_supported"`) if `not controller.supports_memory_presets`;
  raises `ServiceValidationError` (`translation_key="invalid_preset_number"`) if `slot >
  slot_count`; otherwise `if isinstance(controller, HoldCapable): await _submit_hold(coordinator,
  preset_control_name(slot), duration_ms)` else `await
  coordinator.async_execute_controller_command(lambda ctrl, p=slot: ctrl.preset_memory(p))`.
  Requires a controller instance (`await _get_controller_for_service(coordinator)`).
- **Guard, `_goto_named_preset`** (`services.py:378-400`): resolves `control_name =
  preset_control_name(name)`; raises `ServiceValidationError`
  (`translation_key="preset_name_not_supported"`) if `coordinator.control_roster.find(control_name)
  is None`; otherwise `await _submit_hold(coordinator, control_name, duration_ms)`. Reads the
  roster only — no controller instance is fetched, and no `supports_memory_presets`/slot-count
  check runs on this path.
- **Routing branch, `handle_goto_preset`** (`services.py:403-424`): per `device_id`, raises
  `device_not_found` if no coordinator resolves; otherwise `if isinstance(preset, int):
  await _goto_memory_slot(...)` else `await _goto_named_preset(...)`.
- **Schema ordering, `async_register_services`** (`services.py:1103-1109`): `ATTR_PRESET`'s
  `vol.Any` tries the integer-coercion branch before `vol.In(NAMED_PRESETS)`, so a numeric string
  (`"3"`) coerces to a slot number and only a non-numeric string reaches the name branch.

### button.py — commit `e6138b1`

- **Precedence, `_preset_control_name`** (`button.py:735-749`): returns `None` if
  `description.is_program_button`; else `preset_control_name(description.memory_slot)` if
  `memory_slot is not None`; else `preset_control_name(description.key.removeprefix("preset_"))`
  if `description.key.startswith("preset_")`; else `None`. Order matters: a program button's
  `memory_slot` is never reached because the `is_program_button` check runs first.
- **Guard, `AdjustableBedButton.extra_state_attributes`** (`button.py:786-804`): returns `None`
  if `_preset_control_name` returns `None`; returns `None` if `roster.find(name)` is `None`;
  otherwise returns `{"hold_control": control.name, "hold_ttl_max_ms":
  roster.declaration(control).ttl_max_ms}`. Reads `self._coordinator.control_roster` on every
  property access (no caching).
- **Known gap, documented in the commit message, not in code**: the CU170's `preset_anti_snore`
  button recalls memory slot 3, so its control should be `preset-3`, but
  `_preset_control_name`'s key-derivation branch would compute `preset-anti_snore`. Because that
  button description sets `memory_slot=3` this resolves correctly via the `memory_slot` branch
  taking precedence — the commit message flags this as a known gap for a case with no
  `memory_slot` set, which does not presently exist in `BUTTON_DESCRIPTIONS`.

### cover.py — commit `e6138b1`

- **Guard, `AdjustableBedCover.extra_state_attributes`** (`cover.py:317-337`): computes
  `up = roster.find(motor_control_name(key, "up"))` and `down = roster.find(motor_control_name(key,
  "down"))`; returns `None` if either is `None`; otherwise returns `{"hold_control_up": up.name,
  "hold_control_down": down.name, "hold_ttl_max_ms": roster.declaration(up).ttl_max_ms}` — the ttl
  is read from the `up` control's declaration only, not `down`'s.

### intents.ts — commit `50fcbd8`

- **Constants and their derivations** (comments in the source, `intents.ts:40-60`):
  `REFRESH_MS = 250` (`T`); `TTL_MS = 750` (`= T × 3`, so a ttl rides out two consecutive lost
  messages); `MIN_PRESS_MS = 200` (inside the bracket `F = 100 ms` streamer tick to the
  streamer's own `223 ms` press floor, referenced in prose only — not imported from any module);
  `ROLL_MARGIN_MS = 500` (`= 2 × REFRESH_MS`).
- **Id construction, `mintIntentId`** (`intents.ts:66-69`): 16 bytes from
  `crypto.getRandomValues`, hex-encoded (32 hex chars); explicitly not
  `crypto.randomUUID()` (secure-context gated).
- **Construction site, `IntentSender.hold`** (`intents.ts:112-125`): builds a `new LiveIntent(...)`
  with `floorAt = now + MIN_PRESS_MS` and `began = now`; adds it to `_active`; calls `_send()`
  then `_armRefresh()`, in that order; returns the handle.
- **Branch, `IntentSender.release`** (`intents.ts:130-140`): if `handle.floorAt - now <= 0`, ends
  immediately (`_end`); otherwise schedules `_end` via `deps.schedule(wait, ...)` and stores the
  cancel function on `handle.cancelPendingRelease`.
- **Ordering, `IntentSender.drop`** (`intents.ts:144-148`): cancels any pending scheduled release
  first, then calls `_end` — bypasses the press floor.
- **Guard + ordering, `IntentSender.abandon`** (`intents.ts:153-162`): returns early if
  `_active.size === 0`; otherwise cancels the refresh timer, cancels every intent's pending
  release, sends one message with `ending = this._active` (every sample at `ttl_ms = 0`), then
  clears `_active`.
- **Ordering, `IntentSender._end`** (`intents.ts:166-174`): cancels the refresh timer *before*
  sending (comment: prevents a refresh from interleaving with the ttl-0 send on the single-thread
  runtime), sends with `ending = {handle}`, deletes the handle from `_active`, then re-arms the
  refresh only if `_active.size > 0`.
- **Ordering, `IntentSender._refresh`** (`intents.ts:176-181`): clears
  `_cancelRefresh`, calls `_roll()`, then `_send()`, then `_armRefresh()` — roll happens before
  the send that reports it.
- **Threshold, `IntentSender._roll`** (`intents.ts:186-194`): for each active intent, skips
  (`continue`) unless `now >= intent.began + intent.ttlMaxMs - ROLL_MARGIN_MS`; when triggered,
  sets `retiring = intentId`, mints a new `intentId`, and resets `began = now`.
- **Invariant relied on, `IntentSender._send`** (`intents.ts:209-234`, comment at `206-208`): the
  set sent is never empty, because a hold adds to `_active` before sending, an end sends before
  removing, and the refresh timer only runs while `_active` is non-empty — this is stated as a
  comment, not asserted in code.
- **Rejection handling, `IntentSender._send`** (`intents.ts:229-233`): the returned promise is
  swallowed via `.catch(() => undefined)`; the module-level comment on `IntentSenderDeps.send`
  states reporting a rejection is the caller's responsibility (see the card's own `.catch` at
  `adjustable-bed-card.ts:741-744`, which already handles it before this second, always-present
  catch runs).

### intent-hold.ts — commit `bb261ba`

- **Guard, `IntentHold.start`** (`intent-hold.ts:28-37`): returns if `sender()` is `null`;
  returns if `this._ownership.claim(key, pointerId)` is `false`; otherwise calls
  `sender.hold(control, ttlMaxMs)` and stores the handle in `_live`.
- **Guard, `IntentHold.end`** (`intent-hold.ts:52-58`): returns if `_live === null` or
  `this._ownership.release(key)` is `false`; otherwise clears `_live` and calls
  `this.sender()?.release(handle)`.
- **Guard, `IntentHold.cancel`** (`intent-hold.ts:61-67`): same two-part guard as `end`, but
  calls `this.sender()?.drop(handle)` and returns whether a matching hold existed.
- **Ordering, `IntentHold.stopAll`** (`intent-hold.ts:71-78`): clears ownership and `_live`
  *before* calling `this.sender()?.drop(handle)`, then calls `this.stopBed()` last — comment
  states dropping first stops the card re-asserting a press the server-side stop has already
  fenced.
- **Ordering, `IntentHold.abandon`** (`intent-hold.ts:82-86`): clears ownership and `_live`
  before calling `this.sender()?.abandon()`.

### press-ownership.ts — commit `7ad0458`

- **Guard, `PressOwnership.claim`** (`press-ownership.ts:24-29`): returns `false` (no mutation)
  if `_key !== null`; otherwise sets `_key`/`_pointerId` and returns `true` — one control held at
  a time.
- **Three-part check, `PressOwnership.mayEnd`** (`press-ownership.ts:33-41`): `false` if
  `_key !== key`; `false` if `_pointerId !== null && pointerId !== _pointerId`; otherwise returns
  `isPrimaryButtonRelease` directly.
- **Guard, `PressOwnership.release`** (`press-ownership.ts:46-50`): `false` if `_key !== key`
  (no mutation); otherwise calls `clear()` and returns `true`.

### discovery.ts — commit `7f66035`

- **Attribute-name constants** (`discovery.ts:68-71`): `HOLD_CONTROL = "hold_control"`,
  `HOLD_CONTROL_UP = "hold_control_up"`, `HOLD_CONTROL_DOWN = "hold_control_down"`,
  `HOLD_TTL_MAX_MS = "hold_ttl_max_ms"` — the wire names read from `hass.states[...].attributes`.
- **Type guards, `textAttribute`/`numberAttribute`** (`discovery.ts:75-91`): each reads
  `hass.states?.[entityId]?.attributes?.[name]` and returns the value only if `typeof value`
  matches (`"string"`/`"number"`), else `undefined` — the attribute is trusted only when its
  runtime type matches, since `HassEntityAttributes` types the value as `unknown` at the index
  signature.
- **Pairing guard, `holdControl`** (`discovery.ts:96-106`): returns `undefined` unless both
  `textAttribute(..., nameAttribute)` and `numberAttribute(..., HOLD_TTL_MAX_MS)` are defined —
  a half-published pair (one attribute present, the other absent) yields no `HoldControl`.
- **Merge rule, `recordMotorHold`** (`discovery.ts:145-155`): for each direction, only overwrites
  `m.holdUp`/`m.holdDown` when the corresponding attribute name argument is not `undefined`, and
  falls back to the existing value (`?? m.holdUp`) when `holdControl` itself returns `undefined`
  — a motor discovered from two entities (a cover and a direction button) never has an earlier
  direction's control erased by the later entity's absence of that attribute.
- **Call site, `case "cover"`** (`discovery.ts:178-183`): calls
  `recordMotorHold(m, id, HOLD_CONTROL_UP, HOLD_CONTROL_DOWN)` — both directions from the one
  cover entity.
- **Call site, `case "button"`, direction match** (`discovery.ts:231-238`): `if (dir === "up")
  recordMotorHold(m, id, HOLD_CONTROL)` (up-only argument position) `else recordMotorHold(m, id,
  undefined, HOLD_CONTROL)` (down-only argument position) — a direction button records only its
  own direction's control, reading the same `HOLD_CONTROL` attribute name regardless of which
  direction it drives.
- **Call site, `preset_memory_N` button** (`discovery.ts:199-202`): sets `s.gotoHold =
  holdControl(hass, id, HOLD_CONTROL)` unconditionally (may be `undefined`).
- **Call site, other preset button** (`discovery.ts:203-208`): builds
  `{id, hold: holdControl(hass, id, HOLD_CONTROL)}` for `presetMap`.
- **No-op, `program_memory_N` button** (`discovery.ts:209-212`): the save-button branch calls
  neither `holdControl` nor `recordMotorHold` — a save button never publishes a control by
  construction of the switch arm, not by a filtered-out read.

### adjustable-bed-card.ts — commit `ec9102c`

- **Field initialization, `_intentHold`** (`adjustable-bed-card.ts:57-60`): constructed eagerly
  as a class field (`new IntentHold(() => this._senderForDevice(), () => this._stopBed())`),
  alongside the pre-existing eager `_hold` (`MotorHold`) field — both strategies exist for the
  life of the card element regardless of which one a given bed uses.
- **Lazy construction, `_senderForDevice`** (`adjustable-bed-card.ts:732-753`): returns `null` if
  `this._config?.device_id` is falsy; otherwise `this._sender ??= new IntentSender({...})` —
  built once, on first use, not at discovery time; the `send` dependency wraps
  `this.hass?.callService(...)` with its own `.catch` that `console.warn`s and swallows the
  rejection.
- **Reset condition, `setConfig`** (`adjustable-bed-card.ts:95-104`): the sender is abandoned and
  discarded only when a sender already exists (`this._sender` truthy) *and* the incoming
  `config.device_id` differs from the current `this._config?.device_id`.
- **Filter, `_pressOwner`** (`adjustable-bed-card.ts:759-778`): for a `KeyboardEvent`, returns
  `null` if `e.repeat` or the key is not `"Enter"`/`" "`; for a `PointerEvent`, returns `null`
  unless `e.button === 0 && e.isPrimary`; on the pointer path it also calls
  `setPointerCapture?.(e.pointerId)` and `e.preventDefault()` as a side effect before returning
  the owner.
- **Routing, `_startHold`** (`adjustable-bed-card.ts:783-796`): after `_pressOwner` passes,
  branches on `dir === "up" ? m.holdUp : m.holdDown`; present routes to
  `this._intentHold.start(hold.control, m.key, hold.ttlMaxMs, owner.pointerId)` and returns;
  absent falls through to `this._hold.start(m, dir, owner.pointerId)`.
- **Guard, `_tapWithoutPointer`** (`adjustable-bed-card.ts:810-818`): returns if `e.detail !== 0`
  or `this._intentHold.heldKey !== null`; otherwise starts then immediately ends the intent hold
  (`start(...)` then `end(entityId)` back to back), relying on the sender's press floor to make
  the pair register as one press.
- **Routing, `_endPointerHold`** (`adjustable-bed-card.ts:862-871`): computes
  `isPrimaryRelease` once; routes to `this._intentHold.endFromPointer(m.key, e.pointerId,
  isPrimaryRelease)` when `this._intentHold.heldKey === m.key`, else
  `this._hold.endFromPointer(m, ...)`.
- **Routing, `_endHold`** (`adjustable-bed-card.ts:878-884`) and **`_motorStop`**
  (`adjustable-bed-card.ts:886-897`): both branch on `this._intentHold.heldKey === m.key` to pick
  which strategy's `end`/`cancel` to call; `_motorStop`'s button-backed (non-cover) branch calls
  the new `_stopAll()` instead of `this._hold.stopAll()` directly.
- **Routing, `_stopAll`** (`adjustable-bed-card.ts:901-907`): calls
  `this._intentHold.stopAll()` when `this._intentHold.heldKey !== null`, else
  `this._hold.stopAll()` — never both.
- **Render guard, `_presets`** (`adjustable-bed-card.ts:346-358`): `p.hold ? this._holdTile(p.id,
  p.hold) : this._tile(p.id, () => this._press(p.id))`, per preset.
- **Render guard, `_memoryTile`** (`adjustable-bed-card.ts:406-438`): `if (slot.gotoHold) return
  this._holdTile(slot.goto!, slot.gotoHold);` runs after the save-mode branch and before the
  `canRecall`/plain-tile path — a slot with `save` set but no `goto` never reaches this guard
  because `gotoHold` is only ever set alongside `goto` (discovery.ts).
- **Wiring, `_holdTile`** (`adjustable-bed-card.ts:521-538`): binds `@pointerdown`/`@keydown` to
  `_startTileHold`, `@pointerup`/`@pointercancel` to `_endTilePointerHold`, `@keyup` to
  `_endTileKeyHold`, `@blur` to `this._intentHold.end(entityId)` directly (not routed through a
  wrapper), and `@click` to `_tapWithoutPointer`.

## 5. Cross-references

| New/changed type | Referenced by |
|---|---|
| `IntentSender` | `AdjustableBedCard._sender` (held) / `_senderForDevice` (constructs, lazily); `IntentHold`'s `sender` constructor parameter (a getter closure, not a held reference — comment states the reference would go stale across a `device_id` change) |
| `IntentHold` | `AdjustableBedCard._intentHold` (held, eager field); every routing method in `adjustable-bed-card.ts` listed in §4 |
| `PressOwnership` | `MotorHold._ownership` (held); `IntentHold._ownership` (held) — the one shared primitive both hold strategies compose |
| `IntentHandle` | `IntentHold._live` (held); `IntentSender.hold`/`release`/`drop` (produced/consumed) |
| `HoldControl` | `MotorEntity.holdUp`/`holdDown`, `PresetEntity.hold`, `MemorySlot.gotoHold` (held); `discovery.ts`'s `holdControl` function (constructs it); `adjustable-bed-card.ts`'s `_startHold`, `_startTileHold`, `_tapWithoutPointer`, `_holdTile`, `_presets`, `_memoryTile` (read it) |
| `PresetEntity` | `BedEntities.presets` (held, replacing `string[]`); `discovery.ts`'s `presetMap` (`Map<string, PresetEntity>`) and its two construction sites; `adjustable-bed-card.ts`'s `_presets`, `_collectWatched` (read `.id`/`.hold`) |
| `ControlId` | `HoldControl.control`, `IntentSample.control`, `LiveIntent.control`, `IntentSender.hold`'s `control` parameter, `IntentHold.start`'s `control` parameter |
| `preset_control_name` (widened) | `button.py`'s `_preset_control_name` (two call sites); `services.py`'s `_goto_memory_slot` and `_goto_named_preset`; pre-existing `tests/test_hold_roster.py:103` (unchanged, exercises the int path) |
| `motor_control_name` (unchanged, new caller) | `cover.py`'s new `extra_state_attributes` (two calls, "up"/"down"); pre-existing caller `services.py:766` (`handle_timed_move`, untouched by this diff) |
| `_submit_hold` (unchanged, new callers) | `services.py`'s new `_goto_memory_slot` and `_goto_named_preset` (both call it, in addition to the pre-existing `handle_timed_move` call site) |
| `NAMED_PRESETS` | `services.py`'s `async_register_services` schema (`vol.In(NAMED_PRESETS)`) — `_goto_named_preset` does not read this constant; it resolves purely through `coordinator.control_roster.find` |
| TS `IntentSample`/`IntentSampleSet` (new, client-side) | Same names as the pre-existing, unrelated Python dataclasses `hold_intent.IntentSample`/`IntentSampleSet` (`custom_components/adjustable_bed/hold_intent.py:45,54`, not touched by this diff). No import relationship crosses the language boundary; the TS interfaces describe the JSON wire shape the Python `handle_send_intents`/`HoldReconstructor` side (also untouched here) consumes. |

## 6. Test inventory

### custom_components/adjustable_bed/frontend/src/intents.test.ts (18 new tests, new file)

| Test | Slug |
|---|---|
| `client-sample-sets: every message carries the complete active set under one seq` | `client-sample-sets` |
| `client-sample-sets: a ttl-0 sample rides one message beside the live one, then leaves the set` | `client-sample-sets` |
| `client-sample-sets: no empty set ever leaves, and the refresh stops with the last hold` | `client-sample-sets` |
| `client-sample-sets: the set is re-sent every T while anything is held` | `client-sample-sets` |
| `client-sample-sets: a lost message costs nothing — the next refresh re-asserts inside the ttl` | `client-sample-sets` |
| `client-sample-sets: the card sends no activate, so the one-message clause is vacuous here` | `client-sample-sets` |
| `intents-are-parameterized: a sample carries a control, an action and a ttl, and nothing else` | `intents-are-parameterized` |
| `intents-are-time-bounded: every sample on every path carries a ttl` | `intents-are-time-bounded` |
| `key-registration: a release inside the press floor waits for it` | `key-registration` |
| `key-registration: drop bypasses the press floor` | `key-registration` |
| `key-registration: a release after the floor rides a message at once` | `key-registration` |
| `hold-lifetime-from-press-start: the roll sends both ids in one message and the old one leaves` | `hold-lifetime-from-press-start` |
| `hold-lifetime-from-press-start: the successor's own lifetime runs from its own press start` | `hold-lifetime-from-press-start` |
| `a rejected send neither stops the refresh nor retries out of band` | none |
| `an abandoned sender ends every live intent at once, floor or not` | none |
| `abandoning with nothing held sends no message` | none |
| `abandoning cancels a release still waiting on the press floor` | none |
| `an intent id is 128 bits of randomness, hex-encoded` | none |

### custom_components/adjustable_bed/frontend/src/intent-hold.test.ts (16 new tests, new file)

| Test | Slug |
|---|---|
| `presets-hold-only: a preset gesture is a hold on the tile's control` | `presets-hold-only` |
| `press-state-fidelity: one gesture is one intent id from finger down to release` | `press-state-fidelity` |
| `press-state-fidelity: a re-render mid-gesture does not split the press` | `press-state-fidelity` |
| `press-state-fidelity: a second pointer's release does not end the gesture` | `press-state-fidelity` |
| `a non-primary button release does not end the gesture` | none |
| `releasing one control does not end another's gesture` | none |
| `a second start while a control is held is ignored` | none |
| `stopAll drops the live intent before it presses the bed's stop` | none |
| `stopAll with nothing held still presses the bed's stop` | none |
| `cancel ends the gesture at once, without the bed-wide stop` | none |
| `cancel ignores a control that does not hold` | none |
| `abandoning mid-gesture ends the intent at once, floor or not` | none |
| `abandoning with nothing held touches nothing` | none |
| `a start with no sender does nothing, and leaves nothing held` | none |
| `a keyboard gesture has no owning pointer, so any release ends it` | none |
| `a tap's release waits out the press floor before it rides a message` | none |

### custom_components/adjustable_bed/frontend/src/press-ownership.test.ts (8 new tests, new file)

| Test | Slug |
|---|---|
| `a claim while another control holds is ignored` | none |
| `ownership is by key, so a rebuilt entity object still owns its hold` | none |
| `releasing one control does not end another's hold` | none |
| `only the owning pointer may end a hold` | none |
| `a non-primary button release may not end a hold` | none |
| `a release naming another control may not end the hold` | none |
| `a keyboard hold has no owning pointer, so any pointer may end it` | none |
| `clear gives up ownership whatever holds it` | none |

### custom_components/adjustable_bed/frontend/src/discovery.test.ts (4 new, 1 changed)

| Test | Slug |
|---|---|
| `2-motor bed with light switch and no massage/climate` (changed: assertion on `bed.presets` now maps `.id`) | none |
| `a bed publishing hold controls carries them on its covers, presets and memory tiles` | none |
| `presets-hold-only: a save-only memory slot carries no control` | `presets-hold-only` |
| `a bed publishing nothing yields no controls, and is no more empty than before` | none |
| `hold-capable up/down buttons carry the control of the direction they drive` | none |

### tests/test_entities.py — class `TestPublishedHoldControls` (4 new tests)

| Test | Slug |
|---|---|
| `test_a_declared_bed_publishes_its_controls` | none |
| `test_an_undeclared_bed_publishes_nothing` | none |
| `test_a_motor_the_roster_does_not_declare_publishes_nothing` | none |
| `test_a_save_button_publishes_no_control` | `operation-controls-command-path-only` |

### tests/test_hold_service.py — class `TestDirectSubmissionDoors` (5 new tests)

| Test | Slug |
|---|---|
| `test_goto_preset_submits_a_hold_on_a_named_presets_control` | `roster-declares-actions` |
| `test_a_named_preset_without_a_duration_holds_for_the_press_minimum` | `roster-declares-actions` |
| `test_a_slot_number_as_a_string_still_takes_the_slot_path` | none |
| `test_a_named_preset_the_bed_does_not_declare_is_a_caller_error` | `roster-declares-actions` |
| `test_a_preset_name_the_service_does_not_accept_is_refused` | none |

Also in this file: the shared `_declarations()` fixture (`tests/test_hold_service.py:67-76`) gains
a `PRESET_FLAT = Control("preset-flat")` declaration (`:43`), used by the five new tests above;
no pre-existing test in the file asserts the roster's exact control set, so this widening does not
change any existing test's outcome.

### Total

56 new/changed test functions across 6 files (5 new files, 3 changed files — `discovery.test.ts`,
`test_entities.py` and `test_hold_service.py` are changed, not new); 34 carry no kebab-slug in
their name or docstring (listed above as "none" — each still has a descriptive test name).
