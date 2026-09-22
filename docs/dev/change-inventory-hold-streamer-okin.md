# Change inventory: hold-streamer-okin

Tier 0 extraction pass. Range `hold-contract-and-reconstructor..4471b52`, 11 commits (10
implementation, 1 merge):

| Commit | Subject |
|---|---|
| `6f3bca8` | feat: the hold streamer |
| `c5402d6` | feat: release_wire, the ordered-disconnect release |
| `ffbb344` | feat: the Okin encoder, writer and control declarations |
| `9cf61d1` | feat: the Okin's receipt credit gate, deficit guard and cue counter |
| `941a326` | feat: the Okin controller expresses through the streamer |
| `b997892` | feat: the store and the mode gestures as cue-driven staged operations |
| `e2c1720` | feat: the anti-snore preset answers the card's derived control name |
| `dfe5ba8` | feat: entities render and reach the held set |
| `1a089c3` | feat: hold diagnostics in the config-entry payload |
| `1049089` | docs: the Okin page's streamed lifecycle and latch-mode consequences |
| `4471b52` | merge: review fixes from hold-contract-and-reconstructor (conflict in `coordinator.py`; resolution in §4) |

No judgment, no recommendations. Every claim below is either read directly off
`git diff hold-contract-and-reconstructor...HEAD` and the commit bodies, or traced to the
surrounding file. `hold-contract-and-reconstructor` below refers to that branch's tip at the merge
(`4654dde`), which is this diff's merge-base.

## 1. Public surface, added

### Modules

| Module | File |
|---|---|
| `hold_streamer` | `custom_components/adjustable_bed/hold_streamer.py` |
| `hold_operation` | `custom_components/adjustable_bed/hold_operation.py` |
| `beds.leggett_okin_hold` | `custom_components/adjustable_bed/beds/leggett_okin_hold.py` |
| `beds.leggett_okin_evidence` | `custom_components/adjustable_bed/beds/leggett_okin_evidence.py` |

### Classes, protocols, enums, dataclasses

| Name | Kind | File:line |
|---|---|---|
| `CueRequest` | frozen dataclass, slots; field `pulses: int` | `hold_operation.py:21-29` |
| `OperationStage` | frozen dataclass, slots; fields `controls: frozenset[Control]`, `cue: CueRequest \| None`, `ceiling_ms: int`, `recovery: bool = False` | `hold_operation.py:32-39` |
| `StagedOperation` | dataclass, slots; fields `stages`, `outcome: Future[bool]`, `index: int = 0`, `resume_at: float = 0.0`, `ceiling_at: float \| None = None`, `frames: int = 0`, `failed: bool = False`, `done: bool = False` | `hold_operation.py:42-57` |
| `PingRecord` | dataclass, slots; fields `began: float`, `submissions/completions: int = 0`, `round_trip_total_s/round_trip_max_s: float = 0.0`, `ended_at: float \| None = None`, `end_reason: str \| None = None`, `pending: dict[int, float]` | `hold_operation.py:131-142` |
| `StreamProfile` | frozen dataclass, slots; fields `frame_interval_ms: int`, `sustain_window_ms: float`, `send_margin_ms: int` | `hold_streamer.py:47-58` |
| `StreamOptions` | frozen dataclass, slots; field `deficit_trip: int` | `hold_streamer.py:61-65` |
| `PressState` | dataclass, slots; fields `began: float`, `frames: int = 0` | `hold_streamer.py:68-73` |
| `FramePlan` | frozen dataclass, slots; fields `controls: frozenset[Control]`, `is_release: bool`, `confirmed: bool` | `hold_streamer.py:76-86` |
| `SendVerdict` | frozen dataclass, slots; fields `send: bool`, `confirmed: bool`, `barrier: bool` | `hold_streamer.py:89-95` |
| `FrameEncoder` | `Protocol`; one method `encode` | `hold_streamer.py:98-108` |
| `FrameWriter` | `Protocol`; one method `submit` | `hold_streamer.py:110-120` |
| `StreamFeedback` | `Protocol`; six methods (`begin_lifecycle`, `before_send`, `after_send`, `after_confirmation`, `is_sick`, `begin_cue`, `cue_met`) | `hold_streamer.py:122-152` |
| `_ConfirmedWrites` | private class; default `StreamFeedback` — confirmed writes, one outstanding, no cue | `hold_streamer.py:154-187` |
| `HoldStreamer` | class | `hold_streamer.py:189` |
| `OkinFrameEncoder` | class; implements `FrameEncoder` | `beds/leggett_okin_hold.py:252` |
| `OkinFrameWriter` | class; implements `FrameWriter` | `beds/leggett_okin_hold.py:274` |
| `ReceiptCreditGate` | class | `beds/leggett_okin_evidence.py:46` |
| `ReceiptDeficitGuard` | class | `beds/leggett_okin_evidence.py:127` |
| `LightPulseCounter` | class | `beds/leggett_okin_evidence.py:175` |
| `OkinStreamFeedback` | class; implements `StreamFeedback` | `beds/leggett_okin_evidence.py:214` |
| `ControlDeclarationInputs` | frozen dataclass, slots; fields `bed_type: str`, `protocol_variant: str \| None`, `motor_pulse_count: int`, `motor_pulse_delay_ms: int`, `has_massage: bool` | `hold_roster.py:50-63` |

`LeggettOkinCommands` (the keycode constant table) moves from `beds/leggett_okin.py` into
`beds/leggett_okin_hold.py:43-90`, unchanged in content — see §3.

### Functions and methods

| Signature | File:line |
|---|---|
| `StagedOperation.__post_init__(self) -> None` | `hold_operation.py:59-62` |
| `StagedOperation.stage(self) -> OperationStage` (property) | `hold_operation.py:64-71` |
| `StagedOperation.controls(self, now: float) -> frozenset[Control]` | `hold_operation.py:73-77` |
| `StagedOperation.owed_recovery(self) -> tuple[OperationStage, ...]` | `hold_operation.py:79-89` |
| `StagedOperation.note_frame(self, now: float) -> None` | `hold_operation.py:91-95` |
| `StagedOperation.expired(self, now: float) -> bool` | `hold_operation.py:97-99` |
| `StagedOperation.advance(self, now: float, gap_s: float) -> None` | `hold_operation.py:101-103` |
| `StagedOperation.fail(self, now: float, gap_s: float) -> None` | `hold_operation.py:105-108` |
| `StagedOperation._enter(self, index: int, now: float, gap_s: float) -> None` | `hold_operation.py:110-116` |
| `StagedOperation._next_wanted(self, start: int) -> int` | `hold_operation.py:118-129` |
| `PingRecord.note_submission(self, key: int, now: float) -> None` | `hold_operation.py:144-147` |
| `PingRecord.note_completion(self, key: int, now: float) -> None` | `hold_operation.py:149-157` |
| `PingRecord.end(self, now: float, reason: str) -> None` | `hold_operation.py:159-164` |
| `PingRecord.summary(self) -> dict[str, object]` (property) | `hold_operation.py:166-177` |
| `FrameEncoder.encode(self, controls: frozenset[Control]) -> bytes` (protocol method) | `hold_streamer.py:101-108` |
| `FrameWriter.submit(self, frame: bytes, *, confirmed: bool) -> Future[None] \| None` (protocol method) | `hold_streamer.py:113-119` |
| `StreamFeedback.{begin_lifecycle,before_send,after_send,after_confirmation,is_sick,begin_cue,cue_met}` (protocol methods) | `hold_streamer.py:125-152` |
| `_ConfirmedWrites.{begin_lifecycle,before_send,after_send,after_confirmation,is_sick,begin_cue,cue_met}` | `hold_streamer.py:162-187` |
| `HoldStreamer.__init__(self, *, roster, encoder, writer, profile, read_options, clock, feedback=None) -> None` | `hold_streamer.py:198-233` |
| `HoldStreamer.hold(self, held: Mapping[Control, float]) -> None` | `hold_streamer.py:234-243` |
| `HoldStreamer.release_wire(self) -> None` | `hold_streamer.py:244-259` |
| `HoldStreamer.stage(self, stages: Sequence[OperationStage]) -> Future[bool]` | `hold_streamer.py:260-286` |
| `HoldStreamer.run_operation(self, stages) -> bool` (async) | `hold_streamer.py:287-289` |
| `HoldStreamer.note_sick(self) -> None` | `hold_streamer.py:291-298` |
| `HoldStreamer.diagnostics(self) -> dict[str, Any]` (property) | `hold_streamer.py:300-315` |
| `HoldStreamer._start_pump/_stop_pump/_run/_act/_idle/_emit/_settle_write/_write_release/_release_completed/_plan/_expressed/_floor_met/_clear_floor_met/_record/_open_lifecycle/_track_benchmark/_end_benchmark/_settle_operation/_begin_stage_cue/_stage_floor_met/_clear_floor_s/_finish_operation/_wait` (21 private methods) | `hold_streamer.py:317-567` |
| `build_frame(command_value: int, revision: int \| None) -> bytes` | `beds/leggett_okin_hold.py:239-250` |
| `okin_store_stages(slot: int) -> tuple[OperationStage, ...]` | `beds/leggett_okin_hold.py:187-206` |
| `okin_mode_stages(control: Control) -> tuple[OperationStage, ...]` | `beds/leggett_okin_hold.py:209-217` |
| `okin_dummy_stage() -> tuple[OperationStage, ...]` | `beds/leggett_okin_hold.py:220-226` |
| `_dummy_stage(*, recovery: bool = False) -> OperationStage` | `beds/leggett_okin_hold.py:229-236` |
| `OkinFrameEncoder.__init__(self, revision: int \| None) -> None` | `beds/leggett_okin_hold.py:255-257` |
| `OkinFrameEncoder.encode(self, controls: frozenset[Control]) -> bytes` (raises `KeyError`) | `beds/leggett_okin_hold.py:259-272` |
| `OkinFrameWriter.__init__(self, *, client, ble_lock, characteristic_uuid, release_frame, record_trace) -> None` | `beds/leggett_okin_hold.py:282-298` |
| `OkinFrameWriter.submit(self, frame: bytes, *, confirmed: bool) -> Future[None] \| None` | `beds/leggett_okin_hold.py:300-317` |
| `OkinFrameWriter._write(self, frame: bytes, *, confirmed: bool) -> None` (async, raises `ConnectionError`) | `beds/leggett_okin_hold.py:319-330` |
| `_log_unconfirmed_failure(task: Task[None]) -> None` (module function) | `beds/leggett_okin_hold.py:332-338` |
| `control_declarations(inputs: ControlDeclarationInputs) -> tuple[ControlDeclaration, ...]` | `beds/leggett_okin_hold.py:341-371` |
| `_motor_declarations(inputs) -> list[ControlDeclaration]` | `beds/leggett_okin_hold.py:373-393` |
| `_activate_duration_ms(inputs) -> int` | `beds/leggett_okin_hold.py:395-405` |
| `_preset_declarations() -> list[ControlDeclaration]` | `beds/leggett_okin_hold.py:407-428` |
| `_store_declarations() -> list[ControlDeclaration]` | `beds/leggett_okin_hold.py:430-436` |
| `_operation(control: Control, *, deliberate_only: bool = False) -> ControlDeclaration` | `beds/leggett_okin_hold.py:438-452` |
| `_tap(control: Control) -> ControlDeclaration` | `beds/leggett_okin_hold.py:454-463` |
| `ReceiptCreditGate.__init__(self, address: str) -> None` | `beds/leggett_okin_evidence.py:55-65` |
| `ReceiptCreditGate.before_send(self, now: float) -> SendVerdict` | `beds/leggett_okin_evidence.py:66-74` |
| `ReceiptCreditGate.after_send(self, now, *, confirmed) -> None` | `beds/leggett_okin_evidence.py:75-78` |
| `ReceiptCreditGate.after_confirmation(self, now, *, sent_since) -> None` | `beds/leggett_okin_evidence.py:80-92` |
| `ReceiptCreditGate.note_receipt(self) -> None` | `beds/leggett_okin_evidence.py:94-98` |
| `ReceiptCreditGate.diagnostics(self) -> dict[str, Any]` (property) | `beds/leggett_okin_evidence.py:100-110` |
| `ReceiptCreditGate._open_barrier(self, now: float) -> None` | `beds/leggett_okin_evidence.py:112-125` |
| `ReceiptDeficitGuard.{__init__,use_trip,note_frame,note_receipt,is_sick,diagnostics,_leak}` | `beds/leggett_okin_evidence.py:135-172` |
| `LightPulseCounter.{__init__,begin_cue,note_led_mask,met,diagnostics}` | `beds/leggett_okin_evidence.py:186-212` |
| `OkinStreamFeedback.__init__(self, address: str, on_sick: Callable[[], None]) -> None` | `beds/leggett_okin_evidence.py:222-229` |
| `OkinStreamFeedback.note_notification(self, led_mask: int, now: float) -> None` | `beds/leggett_okin_evidence.py:230-234` |
| `OkinStreamFeedback.{begin_lifecycle,before_send,after_send,after_confirmation,is_sick,begin_cue,cue_met,diagnostics}` | `beds/leggett_okin_evidence.py:236-281` |
| `LeggettOkinController.hold(self, held: Mapping[Control, float]) -> None` | `beds/leggett_okin.py:173-182` |
| `LeggettOkinController._disarm_before_first_preset(self, held) -> None` | `beds/leggett_okin.py:183-189` |
| `LeggettOkinController.release_wire(self) -> None` | `beds/leggett_okin.py:190-193` |
| `LeggettOkinController._read_stream_options(self) -> StreamOptions` | `beds/leggett_okin.py:194-202` |
| `LeggettOkinController._record_stream_trace(self, frame: bytes) -> None` | `beds/leggett_okin.py:203-215` |
| `LeggettOkinController._on_stream_sick(self) -> None` | `beds/leggett_okin.py:216-227` |
| `LeggettOkinController._submit(self, control: Control) -> None` | `beds/leggett_okin.py:228-239` |
| `LeggettOkinController._stop_motor(self, motor: str) -> None` | `beds/leggett_okin.py:240-243` |
| `AdjustableBedCover.async_added_to_hass(self) -> None` (override) | `cover.py:294-301` |
| `AdjustableBedCover._held_set_changed(self) -> None` | `cover.py:303-306` |
| `AdjustableBedCover._hold_controls(self) -> tuple[Control, Control] \| None` (property) | `cover.py:308-322` |
| `_preset_control_name(description: AdjustableBedButtonEntityDescription) -> str \| None` (module function) | `button.py:743-757` |
| `AdjustableBedButton._cancel_running(self) -> bool` (property) | `button.py:791-807` |
| `AdjustableBedButton._refuse_a_hold_only_preset(self) -> None` (raises `ServiceValidationError`) | `button.py:809-830` |
| `AdjustableBedButton._hold_only_preset(self) -> Control \| None` | `button.py:832-841` |
| `bed_module_name(bed_type: str, protocol_variant: str \| None = None) -> str \| None` | `controller_factory.py:324-335` |
| `AdjustableBedCoordinator._declaration_inputs(self) -> ControlDeclarationInputs` | `coordinator.py:1356-1364` |

### Constants

| Name | Value | File:line |
|---|---|---|
| `PING` | `Control("ping")` | `hold_streamer.py:43` |
| `_PING_ONLY` | `frozenset({PING})` | `hold_streamer.py:44` |
| `PRESS_FLOOR_MS` | `223` | `beds/leggett_okin_hold.py:92` |
| `HOLD_TTL_MAX_MS` | `30000` | `beds/leggett_okin_hold.py:95` |
| `PING_TTL_MAX_MS` | `120000` | `beds/leggett_okin_hold.py:97` |
| `PROGRAMMABLE_MEMORY_SLOTS` | `(1, 2, 4)` | `beds/leggett_okin_hold.py:99` |
| `CU170_STREAM_PROFILE` | `StreamProfile(frame_interval_ms=LEGGETT_OKIN_PULSE_DEFAULTS[1], sustain_window_ms=217.5, send_margin_ms=5)` | `beds/leggett_okin_hold.py:103-107` |
| `_MOTORS` | `(("head","back"),("feet","legs"),("pillow","tilt"),("lumbar",None))` | `beds/leggett_okin_hold.py:110` |
| `PING`, `LIGHT_TOGGLE`, `PRESET_FLAT`, `PRESET_DUMMY`, `FACTORY_RESET`, `LATCH_MODE_ENABLE` | `Control("ping"/"light-toggle"/"preset-flat"/"preset-dummy"/"factory-reset"/"latch-mode-enable")` | `beds/leggett_okin_hold.py:112-117` |
| `_KEYCODES` | 27-entry `dict[str, int]`, control name → `LeggettOkinCommands` value | `beds/leggett_okin_hold.py:119-147` |
| `_MASSAGE_CONTROLS` | 6-tuple of massage control names | `beds/leggett_okin_hold.py:149-156` |
| `_PRESET_ALIASES` | `{"preset-3": frozenset({"preset-anti_snore"})}` | `beds/leggett_okin_hold.py:161-163` |
| `PRESET_CONTROLS` | `frozenset` of `PRESET_FLAT`, `PRESET_DUMMY`, `preset-1..4` | `beds/leggett_okin_hold.py:167-169` |
| `STORE_ARM_CUE` | `CueRequest(1)` | `beds/leggett_okin_hold.py:175` |
| `STORE_SAVED_CUE` | `CueRequest(3)` | `beds/leggett_okin_hold.py:176` |
| `STORE_STAGE_CEILING_MS` | `5500` | `beds/leggett_okin_hold.py:177` |
| `MODE_STAGE_CEILING_MS` | `7000` | `beds/leggett_okin_hold.py:180` |
| `_MODE_CUES` | `{"factory-reset": CueRequest(2), "latch-mode-enable": CueRequest(4)}` | `beds/leggett_okin_hold.py:181-184` |
| `CREDIT_WINDOW` | `4` | `beds/leggett_okin_evidence.py:27` |
| `CREDIT_EXTRA_EVERY` | `9` | `beds/leggett_okin_evidence.py:30` |
| `DEFICIT_LEAK_S` | `1.0` | `beds/leggett_okin_evidence.py:32` |
| `DEFAULT_DEFICIT_TRIP` | `5` | `beds/leggett_okin_evidence.py:33` |
| `LIGHT_STATE_BIT` | `0x00020000` | `beds/leggett_okin_evidence.py:36` |
| `TRANSITIONS_PER_PULSE` | `2` | `beds/leggett_okin_evidence.py:37` |
| `_WARNED_ADDRESSES` | `set[str]`, module-level, mutated by `ReceiptCreditGate._open_barrier` | `beds/leggett_okin_evidence.py:43` |
| `_MOTOR_CONTROLS` | `dict[str, tuple[Control, Control]]`, one entry per of `head/feet/pillow/lumbar` | `beds/leggett_okin.py:76-81` |
| `__all__` | `["LeggettOkinCommands", "LeggettOkinController", "control_declarations"]` | `beds/leggett_okin.py:73` |
| `_VARIANT_MODULES` | `{(BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_OKIN): "leggett_okin", (BED_TYPE_LEGGETT_PLATT, LEGGETT_VARIANT_MLRM): "leggett_wilinke"}` | `controller_factory.py:318-321` |
| `_OPERATION_BUTTON_KEYS` | `frozenset({"control_mode_press_and_hold", "control_mode_press_and_release"})` | `button.py:40-42` |

### Diagnostics and published state

| Item | Detail | File:line |
|---|---|---|
| Diagnostics payload key `"hold"` | Set to the pre-existing `coordinator.hold_diagnostics` (that property is not new; only this call site is) | `diagnostics.py:220-223` |
| `LeggettOkinController.protocol_diagnostics["hold_stream"]` | `{**self._streamer.diagnostics, **self._feedback.diagnostics}` — merges `HoldStreamer.diagnostics` (`expressed`, `target`, `lifecycle_open`, `frames`, `releases`, `withheld_wakes`, `sick`, `staging`, `operations_completed`, `operations_failed`, `benchmark`) with `OkinStreamFeedback.diagnostics` (`credit`, `receipts`, `barrier_outstanding`, `stalls`, `clears`, `last_clear_ms`, `deficit`, `trip`, `cue_transitions`, `cue_target`). `diagnostics.py:126-128` (pre-existing, untouched by this diff) places `controller.protocol_diagnostics` under the payload key `"protocol_state"`, which is why the test reads `result["controller"]["protocol_state"]["hold_stream"]`. | `beds/leggett_okin.py:311-317` (dict literal), sourced from `hold_streamer.py:300-315` and `beds/leggett_okin_evidence.py:100-110,160-163,208-212,274-281` |

### Strings / translations

| Key | File |
|---|---|
| `exceptions.preset_press_not_supported` (message: `Preset "{control}" on device "{device_name}" is held, not pressed. Use the card's press-and-hold gesture, or the adjustable_bed.goto_preset service.`) | `strings.json`, `translations/en.json` (identical) |

### Test files (new)

| File | New test functions |
|---|---|
| `tests/test_hold_streamer.py` | 36 |
| `tests/test_leggett_okin_hold.py` | 37 |

(Function-level detail in §6.)

### AGENTS.md

Doc-only (commit `1049089`): two lines added to the `beds/` module list —
`leggett_okin_hold.py` and `leggett_okin_evidence.py` — and two lines to the top-level module
list — `hold_streamer.py` and `hold_operation.py`. No code.

## 2. Public surface, changed or widened

| Member | Before | After | File:line | Commit |
|---|---|---|---|---|
| `HoldCapable` | One abstract method, `hold(self, held) -> None`. | Gains a second abstract method, `release_wire(self) -> None` — every existing and future implementer must define it. | `hold_capability.py:22-42` | `c5402d6` |
| `ControlDeclaration` | Fields: `control`, `actions`, `ttl_max_ms`, `activate_duration_ms`, `press_min_frames`, `press_min_ms`, `marks` (no `aliases`). `__post_init__` raised `ValueError` only when `(ActionKind.ACTIVATE in actions) != (activate_duration_ms is not None)`. | Adds field `aliases: frozenset[str] = frozenset()`. `__post_init__` gains a branch: when `ControlMark.OPERATION in marks`, it instead raises `ValueError` (`"...stages its Activate, so it declares no duration"`) if `activate_duration_ms is not None`, and returns — skipping the generic check. **Behavior reversal for OPERATION-marked controls**: the prior increment's own `_store()` test helper and roster required `activate_duration_ms=223` for an OPERATION control; this diff's test change (`tests/test_hold_roster.py`, `tests/test_hold_service.py`) flips that same helper to `activate_duration_ms=None`, and a new test (`test_an_operation_control_prices_no_activate_duration`) asserts the old value now raises. | `hold_roster.py:66-102` | `ffbb344` (declaration/roster), `b997892` (test flip) |
| `ControlRoster.__init__` | Built only `self._declarations`. | Also builds `self._aliases: dict[str, ControlDeclaration]` from every declaration's `aliases` field. | `hold_roster.py:113-122` | `ffbb344` |
| `ControlRoster.find(self, name) -> Control \| None` | `self._declarations.get(name)`. | `self._declarations.get(name) or self._aliases.get(name)` — a name now also resolves through the alias table. | `hold_roster.py:134-141` | `ffbb344` |
| `load_control_declarations` | `async def load_control_declarations(coordinator: AdjustableBedCoordinator) -> tuple[...]`; body `del coordinator; return ()` — a stub, unconditionally empty. | `async def load_control_declarations(hass: HomeAssistant, inputs: ControlDeclarationInputs) -> tuple[...]`; resolves the bed module via `bed_module_name(inputs.bed_type, inputs.protocol_variant)`, imports it off the event loop (`hass.async_add_import_executor_job`), and calls its module-level `control_declarations(inputs)` hook if the module defines one, else returns `()`. Full parameter-list change (`coordinator` → `hass`, `inputs`), no longer a stub. | `hold_roster.py:192-213` | `ffbb344` |
| `HoldStreamer.__init__` | New this diff — no prior code state to diff against. Recorded here because the commit message (`6f3bca8`) itself states six deviations from "the plan['s]" stated shape: `__init__` takes `roster: ControlRoster` (the plan's signature omitted it); `StreamFeedback` gained `begin_lifecycle(options)`; `OperationStage` gained `recovery: bool`; `PressState` carries only `began`/`frames` (the plan's `de_asserted_at` was dropped as unread); `PingRecord` counts submissions/completions/round trips; a stop reaches the streamer as `release_wire()` rather than a shrunk push. | — | `hold_streamer.py:198-233` | `6f3bca8` (commit message documents the deviations; no plan document is present in this worktree to diff against directly) |
| `LeggettOkinController` (class) | `class LeggettOkinController(BedController):` | `class LeggettOkinController(BedController, HoldCapable):` | `beds/leggett_okin.py:127` | `941a326` |
| `LeggettOkinController.__init__` | Built `self._motor_state: dict[str, MotorDirection] = {}` plus the pre-existing protocol/notification bookkeeping. | Drops `_motor_state`; adds `self._disarmed = False`; constructs `OkinFrameEncoder`, `OkinStreamFeedback` (as `self._feedback`), and `HoldStreamer` (as `self._streamer`), the latter wired to a new `OkinFrameWriter` over `self.client`/`self._ble_lock`/`self.control_characteristic_uuid`, `CU170_STREAM_PROFILE`, `self._read_stream_options`, `coordinator.hass.loop.time`, and `self._feedback`. | `beds/leggett_okin.py:142-170` | `941a326` |
| `LeggettOkinController.protocol_diagnostics` (property) | Dict without a `hold_stream` key. | Adds `"hold_stream": {**self._streamer.diagnostics, **self._feedback.diagnostics}`. | `beds/leggett_okin.py:300-317` | `1a089c3` |
| `LeggettOkinController._handle_notification` | Forwarded raw data and parsed/retained the LED/status pair; no other side effect. | Same, plus: when the notifying characteristic is the main status characteristic (`LEGGETT_OKIN_NOTIFY_CHAR_UUID`), calls `self._feedback.note_notification(self._notification_led_mask, self._coordinator.hass.loop.time())`. | `beds/leggett_okin.py:489-505` | `941a326` |
| `LeggettOkinController._build_command` | Two-branch body: revision 0 called the module-local `_build_revision_0_command`; else called `build_okin_command` directly. | Delegates entirely to `build_frame(command_value, self._protocol_revision)` (now defined in `beds/leggett_okin_hold.py`); externally identical output. | `beds/leggett_okin.py:412-416` | `ffbb344` |
| `LeggettOkinController.stop_all` | Cleared `_motor_state`; awaited `_send_release_frames("stop_all", raise_on_error=True)`, which propagated a `BleakError` on failure. | Calls `self.release_wire()` (synchronous, no wait), then `await self._streamer.run_operation(okin_dummy_stage())`. No longer raises on a failed release (the two tests asserting the old surfacing are deleted, not rewritten — see §3, §6). | `beds/leggett_okin.py:578-587` | `941a326` (mechanism), `b997892` (dummy-stage press added) |
| `LeggettOkinController.preset_flat` | Computed a floored `repeat_count`/`repeat_delay_ms` and streamed `PRESET_FLAT` for ~30 s, releasing in a `finally`. | `self._submit(PRESET_FLAT)` — one non-blocking hold-intent submission at the roster's configured Activate duration. | `beds/leggett_okin.py:589-597` | `941a326` |
| `LeggettOkinController.preset_memory` | Looked up `self._MEMORY_SLOTS.get(memory_num)`; on a miss, warned and returned; else awaited `self._recall(command)` (10-frame unterminated burst). | Bounds `memory_num` against `self.memory_slot_count` directly (no per-controller slot table); on an out-of-range slot, warns and returns; else `self._submit(Control(preset_control_name(memory_num)))`. | `beds/leggett_okin.py:598-604` | `941a326` |
| `LeggettOkinController.program_memory` | Awaited `_hold_keycode(MEMORY_STORE, 5.0)` then `_hold_keycode(slot, 2.0)` (open-loop, elapsed-time-based), releasing in a `finally` that could itself raise. | `if not await self._streamer.run_operation(okin_store_stages(memory_num)): log debug` — cue-driven (arm cue, saved cue), never raises. | `beds/leggett_okin.py:605-621` | `b997892` |
| `LeggettOkinController.preset_anti_snore` | `await self._recall(LeggettOkinCommands.PRESET_ANTI_SNORE)` | `self._submit(Control(preset_control_name(3)))` | `beds/leggett_okin.py:622-625` | `941a326` |
| `LeggettOkinController.preset_dummy` | `await self._recall(LeggettOkinCommands.DUMMY)` | `self._submit(PRESET_DUMMY)` | `beds/leggett_okin.py:626-633` | `941a326` |
| `LeggettOkinController._set_control_mode` | Signature `(self, command: int, context: str)`; streamed `command` for 55 frames at 100 ms, then one release frame. | Signature `(self, control: Control, context: str)`; `if not await self._streamer.run_operation(okin_mode_stages(control)): log debug`. | `beds/leggett_okin.py:634-638` | `b997892` |
| `LeggettOkinController.set_control_mode_press_and_hold` | Called `_set_control_mode(LeggettOkinCommands.CONTROL_MODE_PRESS_AND_HOLD, "press-and-hold control mode")`. | Calls `_set_control_mode(FACTORY_RESET, "press-and-hold control mode")`; docstring now states the chord is a factory reset. | `beds/leggett_okin.py:639-646` | `b997892` |
| `LeggettOkinController.set_control_mode_press_and_release` | Called `_set_control_mode(LeggettOkinCommands.CONTROL_MODE_PRESS_AND_RELEASE, "press-and-release control mode")`. | Calls `_set_control_mode(LATCH_MODE_ENABLE, ...)`; docstring now states the chord is one-way. | `beds/leggett_okin.py:647-654` | `b997892` |
| `LeggettOkinController.lights_toggle` | `await self._tap_keycode(LeggettOkinCommands.TOGGLE_LIGHTS, "lights_toggle")` | `self._submit(LIGHT_TOGGLE)` | `beds/leggett_okin.py:655-658` | `941a326` |
| `LeggettOkinController.massage_head_up/massage_head_down/massage_foot_up/massage_foot_down/massage_toggle/massage_mode_step` (6 methods) | Each `await self._tap_keycode(LeggettOkinCommands.<CONST>, "<name>")`. | Each `self._submit(Control("<massage-name>"))`. | `beds/leggett_okin.py:674-699` | `941a326` |
| `LeggettOkinController.move_head_up/move_head_down/move_legs_up/move_legs_down/move_pillow_up/move_pillow_down/move_lumbar_up/move_lumbar_down` (8 methods) | Each `await self._move_motor(<motor>, MotorDirection.UP\|DOWN)` — awaited the full pulse stream and release. | Each `self._submit(_MOTOR_CONTROLS[<motor>][0\|1])` — non-blocking hold-intent submission. | `beds/leggett_okin.py:530, 534, 554, 558, 700, 704, 722, 726` | `941a326` |
| `LeggettOkinController.move_head_stop/move_legs_stop/move_pillow_stop/move_lumbar_stop` (4 methods) | Each `await self._move_motor(<motor>, MotorDirection.STOP)`. | Each `self._stop_motor("<motor>")` — fences both direction controls at the reconstructor. | `beds/leggett_okin.py:538, 562, 708, 730` | `941a326` |
| `AdjustableBedCover.is_opening` / `is_closing` | `self._is_moving and self._move_direction == "open"/"close"`. | On a hold-declaring bed (`self._hold_controls is not None`): `controls[0\|1] in self._coordinator.hold_reconstructor.held`; on every other bed the prior expression, unchanged. | `cover.py:341-346, 349-354` | `dfe5ba8` |
| `AdjustableBedCover._async_start_movement` | Always called `async_execute_controller_command` with the entity description's default `cancel_running`. | Computes `cancel_running = self._hold_controls is None` and passes it explicitly to both the `open_fn` and `close_fn` calls, so a hold-capable bed's press never cancels a staged operation. | `cover.py:388-421` (branch at `:413`) | `dfe5ba8` |
| `AdjustableBedCover._async_stop_movement` | Always ran the generation-tracked stop-command path. | On a hold-declaring bed: calls `self._coordinator.hold_reconstructor.stop(controls)` and returns immediately — no lock, no command path; every other bed keeps the prior generation-tracked path. | `cover.py:440-461` (branch at `:448`) | `dfe5ba8` |
| `AdjustableBedButton.async_press` | Passed `cancel_running=self.entity_description.cancel_movement` directly to `async_execute_controller_command`; no preset refusal. | Calls `self._refuse_a_hold_only_preset()` before dispatch (may raise `ServiceValidationError`); passes `cancel_running=self._cancel_running` (the new property, below). | `button.py:875-895` (calls at `:878`, `:889`) | `dfe5ba8` |
| `AdjustableBedCoordinator.async_build_hold_pieces` | `self._control_roster = ControlRoster(await load_control_declarations(self))`. | `declarations = await load_control_declarations(self.hass, self._declaration_inputs()); self._control_roster = ControlRoster(declarations)` — matches `load_control_declarations`'s new signature. | `coordinator.py:1345-1354` | `ffbb344` (in this diff); further changed by the merge, see §4 |
| `AdjustableBedCoordinator._async_disconnect_locked` | Inside the `if self._controller is not None:` block, went straight to the `stop_keepalive`/`stop_notify` teardown. | First checks `isinstance(self._controller, HoldCapable)` and, if so, calls `self._controller.release_wire()` before the `stop_keepalive`/`stop_notify` calls. An unsolicited drop (`_on_disconnect`) is a different code path and gains no such call. | `coordinator.py:3294-3301` | `c5402d6` |

`controller_factory.py`: no existing member's signature, return type, accepted values, published
state, or raising behavior changed. The diff there is purely additive (`_VARIANT_MODULES`,
`bed_module_name` — §1).

## 3. Removed

### `beds/leggett_okin.py`

| Item | Kind | File it lived at (pre-diff `beds/leggett_okin.py`) |
|---|---|---|
| `MotorDirection` | `Enum` (`UP`, `DOWN`, `STOP`) | removed outright — no replacement type; direction is now which of a motor's two `Control`s is held |
| `RELEASE_FRAME_COUNT = 4` | constant | removed — superseded by the streamer's single confirmed release frame |
| `RELEASE_FRAME_DELAY_MS = 100` | constant | removed — superseded by `StreamProfile.frame_interval_ms` |
| `RECALL_FRAME_COUNT = 10` | constant | removed — a preset is now a held key, not a fixed burst |
| `RECALL_FRAME_DELAY_MS = 100` | constant | removed |
| `MEMORY_STORE_HOLD_S = 5.0` | constant | removed — superseded by `STORE_ARM_CUE`/`STORE_STAGE_CEILING_MS` (cue-driven, not elapsed-time) |
| `MEMORY_SLOT_HOLD_S = 2.0` | constant | removed — superseded by `STORE_SAVED_CUE`/`STORE_STAGE_CEILING_MS` |
| `MEMORY_PROGRAM_FRAME_DELAY_MS = 100` | constant | removed |
| `FLAT_HOLD_S = 30.0` | constant | removed — `preset-flat`'s Activate duration now comes from the roster (`HOLD_TTL_MAX_MS`-capped hold, not a fixed 30 s stream) |
| `CONTROL_MODE_FRAME_COUNT = 55` | constant | removed — superseded by cue-driven mode stages |
| `CONTROL_MODE_FRAME_DELAY_MS = 100` | constant | removed |
| `_build_revision_0_command(command_value: int) -> bytes` | module function | removed — logic moved into `build_frame` (`beds/leggett_okin_hold.py:239-250`) |
| `LeggettOkinController._motor_state: dict[str, MotorDirection]` | instance attribute | removed |
| `LeggettOkinController._MEMORY_SLOTS: dict[int, int]` | class attribute | removed — superseded by `preset_control_name`/the roster |
| `LeggettOkinController.motor_pulse_settings()` | method (override of the `BedController` base) | removed — no replacement override; the base class's implementation applies if anything still calls it, but nothing in `LeggettOkinController` does |
| `LeggettOkinController._get_move_command()` | method | removed |
| `LeggettOkinController._move_motor(self, motor, direction)` (async) | method | removed |
| `LeggettOkinController._send_release_frames(self, context, *, raise_on_error=False, repeat_count=RELEASE_FRAME_COUNT)` (async) | method | removed |
| `LeggettOkinController._recall(self, command: int)` (async) | method | removed |
| `LeggettOkinController._hold_keycode(self, command, hold_seconds)` (async) | method | removed |
| `LeggettOkinController._tap_keycode(self, command, context)` (async) | method | removed |
| Imports `contextlib`, `Enum` (from `enum`) | import | removed (no longer used in this file) |
| Import `LEGGETT_OKIN_PULSE_DEFAULTS` (from `..const`) | import | removed from this file (still used, now imported in `beds/leggett_okin_hold.py`) |
| Import `build_okin_command` (from `.okin_protocol`) | import | removed from this file (still used, now imported only in `beds/leggett_okin_hold.py`) |

`LeggettOkinCommands` itself is not deleted — it moves file (`beds/leggett_okin.py` →
`beds/leggett_okin_hold.py:43-90`), values unchanged; `beds/leggett_okin.py` keeps a re-export via
its new `__all__` (§1).

### Tests deleted (`tests/test_leggett.py`, `tests/test_hold_roster.py`)

| Test | File | Notes |
|---|---|---|
| `test_no_bed_module_declares_a_control_yet` | `test_hold_roster.py` | replaced by `test_a_bed_module_declaring_nothing_gets_an_empty_roster` |
| `test_massage_wave_mode_is_advertised_and_sends_release` | `test_leggett.py` | replaced by `test_massage_wave_mode_is_advertised` (release-burst assertions dropped) |
| `test_motor_streams_then_sends_four_release_frames` | `test_leggett.py` | superseded by `test_the_controller_is_hold_capable` + `test_a_one_shot_submits_its_controls_fixed_duration_intent` |
| `test_motor_stream_uses_the_proven_pulse_delay` | `test_leggett.py` | equivalent floor behavior now covered in `test_hold_roster.py` (`test_an_unsafe_pulse_delay_floors_at_the_proven_cadence`) |
| `test_memory_recall_ladder_and_burst` | `test_leggett.py` | superseded by `test_each_preset_recalls_only_its_own_slot` and `test_leggett_okin_hold.py`'s `test_the_memory_slot_ladder_is_exact` |
| `test_dummy_preset_sends_the_dummy_key_as_a_recall` | `test_leggett.py` | superseded by the parametrized `test_a_one_shot_submits_its_controls_fixed_duration_intent` |
| `test_only_memory_1_recalls_the_memory_1_keycode` | `test_leggett.py` | replaced by `test_only_memory_1_asserts_the_memory_1_control` |
| `test_control_modes_use_the_exact_special_command_lifecycle` | `test_leggett.py` | replaced by `test_the_control_mode_gestures_are_advertised` |
| `test_program_memory_is_a_two_stage_hold` | `test_leggett.py` | no direct replacement in this file — cue-driven staging is covered by `test_a_staged_operation_carries_its_own_cues` and `test_hold_streamer.py`'s `TestCueOrCeiling` |
| `test_stop_all_propagates_write_failures` | `test_leggett.py` | deleted, not rewritten — `stop_all` no longer raises (commit `941a326`'s message states this explicitly) |
| `test_cleanup_release_failures_do_not_mask_the_real_error` | `test_leggett.py` | deleted, not rewritten, same reason |
| `test_preset_flat_floors_an_unsafe_pulse_delay` | `test_leggett.py` | equivalent floor behavior now covered in `test_hold_roster.py` |
| `test_program_memory_aborts_when_the_arm_hold_fails` | `test_leggett.py` | old open-loop failure semantics; no direct replacement |
| `test_light_and_massage_taps_end_with_a_release_burst` | `test_leggett.py` | old tap/release-burst mechanism; no direct replacement |
| `test_program_memory_surfaces_a_failed_final_release` | `test_leggett.py` | old surfacing-on-release-failure semantics; no direct replacement (`program_memory` no longer raises) |
| `test_release_failure_surfaces_after_a_successful_command` (parametrized) | `test_leggett.py` | old surfacing semantics; no direct replacement |

## 4. Decisions the code introduces

### `hold_operation.py` — commit `6f3bca8`

- **Derived index, `StagedOperation.__post_init__`** (`:59-62`): `self.index = self._next_wanted(0)`,
  so a freshly constructed operation skips any leading `recovery` stages (none is owed at
  construction unless the caller pre-pended owed ones — see `HoldStreamer.stage` in §4 below).
- **Selection rule, `StagedOperation._next_wanted`** (`:118-129`): returns the first stage index
  `>= start` whose `stage.recovery == self.failed` — i.e., a non-failed operation runs only its
  non-recovery stages in order, and a failed one runs only its remaining recovery stages; `fail()`
  therefore jumps straight to whatever recovery stages are left.
- **Guard, `StagedOperation.controls`** (`:73-77`): returns `frozenset()` (no bits) while
  `now < self.resume_at` (the post-stage clear-floor gap) or once `self.done` — the gap is a real
  gate on what the streamer may express, not merely bookkeeping.
- **Filter, `owed_recovery`** (`:79-89`): returns the remaining recovery stages with
  `recovery=False` — a `replace(stage, recovery=False)` — so a caller that re-runs them treats them
  as ordinary stages, not as a further failure's recovery.
- **Timer arming, `note_frame`** (`:91-95`): a stage's ceiling is armed (`ceiling_at = now +
  ceiling_ms/1000`) only on its *first* counted frame, not at stage entry — `_enter` clears
  `ceiling_at` to `None` and it stays `None` until `note_frame` is next called.
- **Ordering, `advance`/`fail`** (`:101-108`): both call `_enter(self._next_wanted(index+1), now,
  gap_s)`; `fail` additionally sets `self.failed = True` first, so the very next `_next_wanted` call
  inside the same `_enter` sees the failed state.
- **Reset, `PingRecord.end`** (`:159-164`): a no-op if `self.ended_at is not None` — the record keeps
  the *first* reason it ended for.

### `hold_streamer.py` — commit `6f3bca8` (constructor/state), `9cf61d1`/`941a326` (feedback wiring), `b997892` (staging)

- **Default construction, `HoldStreamer.__init__`** (`:198-233`): `self._feedback: StreamFeedback =
  feedback if feedback is not None else _ConfirmedWrites()` — a bed supplying no evidence model gets
  confirmed writes, one outstanding at a time, no cue (`_ConfirmedWrites`, `:154-187`).
- **Idempotent push, `hold`** (`:234-243`): synchronous; replaces `self._target` outright and calls
  `self._start_pump()`, which is itself idempotent (below) — no suspension point between a caller's
  stop and the target-set replacement.
- **Ordering, `release_wire`** (`:244-259`): clears `self._target`; if an operation is live, captures
  `self._owed_recovery = operation.owed_recovery()` and finishes it `succeeded=False` *before*
  writing the release frame (`if self._open: self._write_release(...)`); the pump is stopped last.
- **Preemption, `stage`** (`:260-286`): a live operation is finished `succeeded=False` and its owed
  recovery captured before the new one is constructed; the new `StagedOperation.stages` is
  `self._owed_recovery + tuple(stages)` — the preempted operation's owed recovery leads the new
  operation's own stages.
- **Guard, `_start_pump`** (`:317-323`): no-op if a pump task exists and is not done — at most one
  pump task per streamer.
- **Guard, `_stop_pump`** (`:325-330`): does not cancel the task if it *is* the currently running
  task (`pump is not asyncio.current_task()`) — the pump never cancels itself mid-iteration.
- **Loop shape, `_run`** (`:332-342`): every iteration re-reads `now`, settles the operation, plans
  the frame, acts on the plan, checks `_idle`, and sleeps to the next wake — one frame decision per
  wake, unconditionally.
- **Termination, `_idle`** (`:354-362`): the pump exits only when the lifecycle is closed
  (`not self._open`), no operation is staging, nothing is currently expressed, and no target deadline
  is still in the future.
- **Gate, `_emit`** (`:364-381`): asks `self._feedback.before_send(now)`; on `send=False` increments
  `self._withheld` and returns `None` without submitting anything (the target set is untouched — a
  withheld wake changes no bit); `confirmed = verdict.confirmed or plan.confirmed`; awaits the
  submitted future only `if future is not None and (verdict.barrier or plan.confirmed)`; after
  emitting, calls `self._feedback.is_sick(now)` and, if true, `self.note_sick()`.
- **Failure handling, `_settle_write`** (`:383-394`): catches bare `Exception` from the awaited
  future, logs at debug, and returns without further action — a failed confirmed write is not
  retried and does not itself trip the deficit guard beyond what `after_send`/`note_frame` already
  counted.
- **Ordering, `_write_release`** (`:396-407`): submits the empty-set frame, increments
  `self._releases`, sets `self._open = False`, resets `self._sent_since_release = 0`, marks every
  currently-pressed control's `_cleared_at[control] = now`, clears `self._presses`, ends the
  benchmark (`"released"`), and only then attaches `self._release_completed` to the future.
- **Credit callback, `_release_completed`** (`:409-413`): ignores a cancelled or failed future; on
  success, calls `self._feedback.after_confirmation(now, sent_since=self._sent_since_release)`.
- **Plan derivation, `_plan`**/`_expressed`** (`:415-443`): while an operation is staging, the
  expressed set is `draining or self._operation.controls(now)` — any control still draining its
  floor takes priority over the operation's own bits, so an operation's frame never overlaps a
  draining hold's bits; otherwise the expressed set is `pressable | (draining - live)` — a
  control's bits appear once its clear floor has passed (or it is already mid-press) and disappear
  only once fully drained.
- **Floor checks, `_floor_met`/`_clear_floor_met`** (`:445-458`): both read the *declaration's*
  `press_min_frames`/`press_min_ms` via `self._roster.declaration(control)` — the roster is the sole
  source of a control's floor; the streamer holds no per-control timing of its own beyond what it
  reads there.
- **Bookkeeping, `_record`** (`:460-477`): opens the lifecycle (`_open_lifecycle`) on the first
  frame of one; increments `self._sent_since_release`; only counts a frame against the staged
  operation (`operation.note_frame(now)`) when `plan.controls == operation.stage.controls` exactly —
  a frame carrying draining bits instead of the operation's bits does not advance the operation's
  ceiling clock.
- **Latch-once, `_open_lifecycle`** (`:479-483`): reads `self._read_options()` and calls
  `self._feedback.begin_lifecycle(...)` exactly once per lifecycle, at the first frame — a config
  change mid-lifecycle is invisible until the next lifecycle (`options-latch-per-lifecycle`).
- **Benchmark lifecycle, `_track_benchmark`/`_end_benchmark`** (`:485-497`): the benchmark record is
  (re)started only when the plan's controls equal exactly `_PING_ONLY`; any other plan — including an
  empty one — ends it with a reason (`"another control held"`, `"released"`).
- **Cue evaluation order, `_settle_operation`** (`:499-522`): if the current stage has no cue,
  advancing depends on `_stage_floor_met`; if it has a cue, `self._feedback.cue_met()` is checked
  *before* `operation.expired(now)` — a cue that arrives in the same wake the ceiling would also
  trip still wins.
- **Reset-before-express, `_begin_stage_cue`** (`:524-527`): called after every `advance`/`fail`,
  before the next stage's first frame goes out — the feedback's cue counter is armed ahead of any
  frame the new stage could produce.
- **All-or-nothing, `_stage_floor_met`** (`:529-538`): requires every control in `stage.controls` to
  both be currently pressed and have its floor met — a stage with more than one control (none exist
  in this diff's declarations, per §1's `operations-express-alone` test) would need all of them
  floor-met simultaneously.
- **Resolution, `_finish_operation`** (`:548-559`): sets the future's result only
  `if not operation.outcome.done()` — a cancelled caller's future is left alone.
- **Pacing, `_wait`** (`:561-567`): `due = (sent_at or now) + interval; sleep(max(0.0, due - now))`
  — the next wake is always relative to *this* frame's own send time, never to a fixed schedule, so
  a late wake never triggers a catch-up burst.

### `beds/leggett_okin_hold.py` — commit `ffbb344` (encoder/writer/declarations), `b997892` (stage builders)

- **Revision branch, `build_frame`** (`:239-250`): revision `0` takes the checksummed
  `E5 FE 16` framing; every other value, including `None` (unresolved), takes the plain
  revision-1 frame — an unresolved revision is not a third case.
- **Coverage guard, `OkinFrameEncoder.encode`** (`:259-272`): raises bare `KeyError` (the dict
  lookup's own exception, undecorated) when a control's name is absent from `_KEYCODES`.
- **Trace throttling, `OkinFrameWriter.submit`** (`:300-317`): records a trace entry only
  `if self._quiet` (i.e., the *previous* frame was the release frame); sets
  `self._quiet = frame == self._release_frame` unconditionally after — so one trace entry is filed
  per wire lifecycle (its first frame), not per frame.
- **Fire-and-forget task, `submit`** (`:300-317`): every submission — confirmed or not — becomes its
  own `asyncio.Task`; the task is tracked in `self._pending` (a `set`, discarded on completion via
  `add_done_callback`) so it is not garbage-collected mid-flight; only a confirmed write's task is
  returned to the caller, and only an unconfirmed one gets `_log_unconfirmed_failure` attached.
- **Raise site, `_write`** (`:319-330`): raises `ConnectionError("Not connected to bed")` when
  `client is None or not client.is_connected`, before acquiring `self._ble_lock`; otherwise writes
  under the lock with `response=confirmed`.
- **Debug-only logging, `_log_unconfirmed_failure`** (`:332-338`): returns immediately if the task
  was cancelled; otherwise reads `task.exception()` and logs it at `debug` if not `None` — no
  exception reaches any caller for an unconfirmed write.
- **Cue/ceiling constants**: `STORE_ARM_CUE = CueRequest(1)`, `STORE_SAVED_CUE = CueRequest(3)`,
  `STORE_STAGE_CEILING_MS = 5500` — "the vendors' 5-6 s holds are the ceiling, not the arming time"
  (commit `b997892`); `MODE_STAGE_CEILING_MS = 7000` — sized to clear the observed 5.0-5.2 s answer
  window plus its 281-625 ms burst span; `PRESS_FLOOR_MS = 223` — the 217-218 ms motion watchdog plus
  a margin covering client-side pacing jitter; `HOLD_TTL_MAX_MS = 30000` — bounds a hold/preset to
  the `timed_move` service's own maximum; `PING_TTL_MAX_MS = 120000` — sized to the hardware
  checklist's 60 s benchmark run.
- **Recovery stage placement, `okin_store_stages`** (`:187-206`): the disarming press
  (`_dummy_stage(recovery=True)`) is the *third* stage in the returned tuple, unconditionally
  present — it only actually runs when an earlier stage's cue is missed (per `StagedOperation`'s
  selection rule above).
- **Single stage, `okin_mode_stages`** (`:209-217`): looks up `_MODE_CUES[control.name]` — raises
  `KeyError` for any `Control` other than `FACTORY_RESET`/`LATCH_MODE_ENABLE` (not otherwise
  guarded).
- **Guarded gate, `control_declarations`** (`:341-371`): massage declarations
  (`_MASSAGE_CONTROLS`) are appended only `if inputs.has_massage` — mirrors the button platform's
  own massage gate, so a sample cannot drive a massage keycode on a bed the entry says has none.
- **Floor computation, `_activate_duration_ms`** (`:395-405`): `delay_ms =
  max(inputs.motor_pulse_delay_ms, LEGGETT_OKIN_PULSE_DEFAULTS[1])`; returns
  `max(inputs.motor_pulse_count * delay_ms, PRESS_FLOOR_MS)` — a configured delay of `0`, negative,
  or `1` cannot produce a sub-floor duration, and neither can a `motor_pulse_count` of `1`.
- **Slot exclusion, `_preset_declarations`/`_store_declarations`** (`:407-436`): every slot
  `1..4` gets a preset declaration; only `PROGRAMMABLE_MEMORY_SLOTS` (`1, 2, 4`) get a paired
  `store-preset-N` declaration — slot 3 (the fixed snore entry) declares a preset but no store
  control.
- **Marks, `_operation`** (`:438-452`): every operation control gets `ControlMark.OPERATION`;
  `ControlMark.DELIBERATE_ONLY` is added only when `deliberate_only=True` (used for `FACTORY_RESET`
  and `LATCH_MODE_ENABLE`, not for the store controls).
- **Uniform floor, `_tap`** (`:454-463`): every tap control's `activate_duration_ms` equals its
  `press_min_ms`, both `PRESS_FLOOR_MS` — a tap is priced at exactly the press floor, never longer.

### `beds/leggett_okin_evidence.py` — commit `9cf61d1`

- **Credit ladder, `ReceiptCreditGate.before_send`** (`:66-74`): `if self._barrier_outstanding:
  send=False`; `elif self._credit > 0: send=True, confirmed=False`; else opens the barrier and
  returns `send=True, confirmed=True, barrier=True` — a stalled gate lets nothing through until the
  barrier's own completion clears it.
- **Floor, `after_send`** (`:75-78`): `self._credit = max(0, self._credit - 1)` — credit never goes
  negative.
- **Monotonic-non-decreasing, `after_confirmation`** (`:80-92`): `self._credit = max(self._credit,
  CREDIT_WINDOW - sent_since)` — a completion can only raise credit, never lower it; also clears
  `_barrier_outstanding` and records `_last_clear_s = now - stall_began` only
  `if self._barrier_outstanding` was true.
- **Bonus schedule, `note_receipt`** (`:94-98`): `extra = 1 if self._receipts % CREDIT_EXTRA_EVERY ==
  0 else 0`; `self._credit = min(CREDIT_WINDOW, self._credit + 1 + extra)` — every 9th receipt
  (`CREDIT_EXTRA_EVERY = 9`) returns a bonus credit, sized (per the module docstring) to survive up
  to `1/(K+1) = 10%` receipt loss against the measured 2.5-4.5% band.
- **Once-per-address warning, `_open_barrier`** (`:112-125`): logs at `warning` only the first time
  `self._address` is not already in the module-level `_WARNED_ADDRESSES` set — one warning per bed
  device per Home Assistant process start, not per stall and not per reconnect.
- **Leak-then-count, `ReceiptDeficitGuard.note_frame`/`note_receipt`/`is_sick`** (`:145-158`): every
  entry point calls `self._leak(now)` first, then adjusts `self._deficit`.
- **Leak mechanics, `_leak`** (`:165-172`): on the first call, only records `self._last_leak = now`
  (no decrement); thereafter, a `while` loop decrements `self._deficit` (floored at 0) once per full
  `DEFICIT_LEAK_S = 1.0` elapsed, advancing `_last_leak` by exactly that increment each time (not
  snapping to `now`) — a leak never over- or under-counts a partial second.
- **Cue reset, `LightPulseCounter.begin_cue`** (`:192-196`): resets `self._transitions = 0` and sets
  `self._target = cue.pulses * TRANSITIONS_PER_PULSE` (`TRANSITIONS_PER_PULSE = 2`) — discards
  whatever the previous stage's notifications counted.
- **Edge detection, `note_led_mask`** (`:197-203`): counts a transition only when
  `self._previous is not None and lit != self._previous` — the very first notification after
  `begin_cue` never itself counts as a transition, only establishes the baseline.
- **Met condition, `met`** (`:204-206`): `self._target > 0 and self._transitions >= self._target` —
  a cue with `target == 0` (no cue ever begun) can never report met.
- **Fan-out, `OkinStreamFeedback.note_notification`** (`:230-234`): every status notification feeds
  all three counters unconditionally — `note_receipt()` (credit), `note_receipt(now)` (deficit), and
  `note_led_mask(led_mask)` (pulse counter).
- **Once-only callback, `is_sick`** (`:253-264`): `sick = self._deficit.is_sick(now)`; calls
  `self._on_sick()` only `if sick and not self._reported_sick`, then sets `self._reported_sick =
  True` — the controller's disconnect ordering fires exactly once per feedback instance regardless
  of how many further `is_sick` calls return `True`.

### `beds/leggett_okin.py` — commit `941a326`, `b997892`, `1a089c3`, `c5402d6`

- **Construction site, `__init__`** (`:142-170`): `self._streamer` and `self._feedback` are both
  built inside the controller's constructor (not lazily), wiring `OkinFrameWriter.release_frame =
  encoder.encode(frozenset())` — the writer is handed the release frame's bytes at construction, not
  computed per release.
- **Idempotence guard, `_disarm_before_first_preset`** (`:183-189`): returns immediately if
  `self._disarmed` is already `True`, or if `PRESET_CONTROLS.isdisjoint(held)` (the pushed set
  touches no preset control) — the disarming press is staged (`self._streamer.stage(okin_dummy_stage())`)
  at most once per controller instance, and only ahead of this link's *first* preset key.
- **Ordering, `hold`** (`:173-182`): calls `self._disarm_before_first_preset(held)` before
  `self._streamer.hold(held)` — the disarming press's `stage()` call (synchronous) is guaranteed to
  precede the push that provoked it.
- **Disconnect trigger, `_on_stream_sick`** (`:216-227`): logs a `warning`, then calls
  `self._coordinator.hass.async_create_task(self._coordinator.async_disconnect())` — the disconnect
  itself is scheduled as a background task, not awaited from inside the notification-handling path.
- **Duration fallback, `_submit`** (`:228-239`): reads `declaration.activate_duration_ms`; if `None`,
  falls back to `declaration.press_min_ms` — a control with no Activate duration of its own (a
  Hold-only preset reached through a `Control` that also has no Activate) still gets a bounded
  submission.
- **Options stub, `_read_stream_options`** (`:194-202`): always returns `StreamOptions(deficit_trip=
  DEFAULT_DEFICIT_TRIP)` — the config entry exposes no such option in this diff; the docstring states
  the read still happens per lifecycle, which is where a future option would land.
- **Trace payload site, `_record_stream_trace`** (`:203-215`): calls
  `self._coordinator.record_command_trace(...)` with `characteristic_handle=None`,
  `command_origin="hold_stream"`, `repeat_count=1`, `repeat_delay_ms=CU170_STREAM_PROFILE.frame_interval_ms`
  — one synthetic trace entry per wire lifecycle, shaped to look like a single-frame command.

### `hold_roster.py` — commit `ffbb344`

- **Guard split, `ControlDeclaration.__post_init__`** (`:84-102`): the `ControlMark.OPERATION`
  branch `return`s immediately after its own check — an operation control's `actions`/
  `activate_duration_ms` pairing is never checked against the generic
  `(ActivateInActions) == (duration is not None)` rule at all, not merely exempted from raising.
- **Import shape change**: the `TYPE_CHECKING`-guarded import of `AdjustableBedCoordinator` is
  replaced by a concrete `from homeassistant.core import HomeAssistant` and
  `from .controller_factory import bed_module_name` — `hold_roster.py` now imports
  `controller_factory` at module load, not only for type-checking.

### `controller_factory.py` — commit `ffbb344`

- **Two-tier resolution, `bed_module_name`** (`:324-335`): checks `_SIMPLE_CONTROLLERS.get(bed_type)`
  first (the registry, covering every "import this class" bed); only if that misses and
  `protocol_variant is not None` does it fall back to `_VARIANT_MODULES.get((bed_type,
  protocol_variant))` — a bed type resolved by a branch in `create_controller` (Leggett & Platt,
  Malouf's RichMat variant) is otherwise invisible to the registry.

### `coordinator.py` — commit `ffbb344`, `c5402d6`; further changed by the merge (see below)

- **Value object, `_declaration_inputs`** (`:1356-1364`): reads `self._bed_type`,
  `self._protocol_variant`, `self._motor_pulse_count`, `self._motor_pulse_delay_ms`,
  `self._has_massage` — exactly the four values a bed module's `control_declarations` hook needs,
  rather than passing the coordinator itself (avoiding a conduit).
- **Placement, `_async_disconnect_locked`** (`:3294-3301`): the `release_wire()` call is inside the
  existing `if self._controller is not None:` guard, and precedes both the `stop_keepalive` and
  `stop_notify` calls — "release-before-disconnect" per the commit's own slug.

### `beds/leggett_okin.py` / `beds/leggett_okin_hold.py` cross-cutting — commit `1a089c3`

- **Merge site, `protocol_diagnostics`**: `{**self._streamer.diagnostics, **self._feedback.diagnostics}` —
  a plain dict merge with no key-collision handling; the two source dicts' keys are disjoint by
  construction (verified by reading both `diagnostics` properties — no shared key name).

### `cover.py` / `button.py` — commit `dfe5ba8`, `e2c1720`

- **Roster-not-controller read, `AdjustableBedCover._hold_controls`** (`cover.py:308-322`): reads
  `self._coordinator.control_roster` (never `self._coordinator._controller`), so an idle bed answers
  identically to a connected one — the same rule `AdjustableBedButton._hold_only_preset` and the
  service handler (prior increment) follow.
- **Guarded subscription, `async_added_to_hass`** (`cover.py:294-301`): calls
  `self.async_on_remove(self._coordinator.hold_reconstructor.async_add_listener(...))` only
  `if self._hold_controls is not None` — a cover the bed declares no motor for (e.g. `tv_lift`,
  `bed_height` on this bed type) never subscribes, and keeps using its own `_is_moving` flag.
- **Untouched flag, `_async_start_movement`**: the docstring/decision explicitly kept
  `_async_start_movement` writing the per-entity `_is_moving` flag even on a hold-capable bed — "It
  is not read there, and branching the try/finally around it would buy nothing" (commit message).
- **Alias resolution, `_PRESET_ALIASES`** (`beds/leggett_okin_hold.py:161-163`): `"preset-3"` aliases
  `"preset-anti_snore"` — the name `AdjustableBedButton._preset_control_name` derives from the
  `preset_anti_snore` button's own entity key (`f"preset-{key.removeprefix('preset_')}"` producing
  `"preset-anti_snore"`) resolves through `ControlRoster.find`'s alias fallback to the same
  `Control("preset-3")` the numbered slot uses — one control, two names, no second bit.
- **Refusal ordering, `AdjustableBedButton._hold_only_preset`** (`button.py:832-841`): returns
  `None` (no refusal) whenever `roster.supports(control, ActionKind.ACTIVATE)` is `True` — a preset
  that *does* support Activate (none do, on this bed, but the check is generic) is never refused.
- **Cancel-running carve-out, `_cancel_running`** (`button.py:791-807`): returns `False` outright if
  `not self.entity_description.cancel_movement`; returns `True` outright if the bed's roster does not
  `declares_hold`; on a hold-declaring bed, returns
  `self.entity_description.is_program_button or self.entity_description.key in
  _OPERATION_BUTTON_KEYS` — only the three save buttons and the two mode-chord buttons may cancel a
  running operation; every other button (motion taps, presets, light, massage) may not.

### Merge commit `4471b52` — conflict resolution in `coordinator.py`

The commit message declares the sole conflict as `custom_components/adjustable_bed/coordinator.py`.
Relative to this branch's own pre-merge tip (`1049089`), the merge **removed** a mechanism that
predates this increment's ten commits (introduced by `f48b7cc`/`1210c69` on
`hold-contract-and-reconstructor`, before `hold-streamer-okin` forked, so it never appears in the
`hold-contract-and-reconstructor...HEAD` three-dot diff read for §§1-3):

- `self._hold_roster_rebuild: asyncio.Task[None] | None = None` (an `__init__` instance attribute)
- the block inside `_apply_runtime_bed_type_correction` that scheduled
  `self._hold_roster_rebuild = self.hass.async_create_task(self.async_build_hold_pieces(), name=...)`
  when the bed type changed
- `async def _async_await_hold_roster_rebuild(self) -> None`, which cleared
  `self._hold_roster_rebuild` and awaited the captured task
- its one call site inside the connect path, `await self._async_await_hold_roster_rebuild()`

In its place, the merge result (matching what `hold-contract-and-reconstructor`'s own later "review
fixes" commit — the merge's second parent, `4654dde` — had independently done to the same area)
reads, at the former call site:

```
if self._bed_type != previous_bed_type:
    await self.async_build_hold_pieces()
```

(`coordinator.py:2538-2542`). Net effect: the two-step "schedule a background task at
correction-time, then await a stored reference to it later" pattern is replaced by a single direct
`await` of `async_build_hold_pieces()`, gated on whether `_apply_runtime_bed_type_correction`
actually changed `self._bed_type`, at the point in the connect path where the roster is needed. This
branch's own `_declaration_inputs()` addition and the two-argument `load_control_declarations` call
(both introduced by `ffbb344`, §§1-2 above) are preserved unchanged by the merge. No test in this
diff's own file list references `_hold_roster_rebuild` or `_async_await_hold_roster_rebuild`; the
merge's own file list (`git show --stat 4471b52`, distinct from the three-dot diff) also touched
`hold_reconstructor.py`, `services.py`, `strings.json`, `translations/en.json`,
`docs/dev/change-inventory-hold-contract-and-reconstructor.md`, and four test files, all identically
to `hold-contract-and-reconstructor`'s tip (`4654dde`) — a clean merge-in of that branch's later
commits, not part of this increment's own work.

## 5. Cross-references

| New/changed type | Referenced by |
|---|---|
| `HoldStreamer` | `LeggettOkinController.__init__` (constructs, holds as `self._streamer`); `LeggettOkinController.hold`/`release_wire`/`stop_all`/`preset_flat`/`preset_memory`/`program_memory`/`preset_anti_snore`/`preset_dummy`/`_set_control_mode`/every `move_*`/`massage_*`/`lights_toggle` method (calls `hold`, `release_wire`, `stage`/`run_operation`); `LeggettOkinController.protocol_diagnostics` (reads `.diagnostics`) |
| `StreamFeedback` (protocol) | Implemented by `OkinStreamFeedback` and the test doubles `_ConfirmedWrites` (default) and `_Feedback` (`tests/test_hold_streamer.py`); consumed by `HoldStreamer` (`self._feedback`) |
| `FrameEncoder` / `FrameWriter` (protocols) | Implemented by `OkinFrameEncoder`/`OkinFrameWriter` and the test doubles `_Encoder`/`_Writer` (`tests/test_hold_streamer.py`); consumed by `HoldStreamer` (`self._encoder`, `self._writer`) |
| `OperationStage` / `CueRequest` / `StagedOperation` | Constructed by `okin_store_stages`, `okin_mode_stages`, `okin_dummy_stage`, `_dummy_stage` (`beds/leggett_okin_hold.py`) and directly in `tests/test_hold_streamer.py`; consumed by `HoldStreamer.stage`/`_settle_operation`/`_expressed`/`_record` |
| `PingRecord` | Constructed and held by `HoldStreamer._benchmark`; read via `HoldStreamer.diagnostics["benchmark"]` |
| `OkinStreamFeedback` | Constructed in `LeggettOkinController.__init__` (`self._feedback`), fed by `LeggettOkinController._handle_notification`; read by `LeggettOkinController.protocol_diagnostics`; its `on_sick` callback is `LeggettOkinController._on_stream_sick` |
| `ReceiptCreditGate` / `ReceiptDeficitGuard` / `LightPulseCounter` | Held privately inside `OkinStreamFeedback`; not referenced anywhere else |
| `ControlDeclarationInputs` | Constructed by `AdjustableBedCoordinator._declaration_inputs`; consumed by `load_control_declarations` (passes through) and by `beds/leggett_okin_hold.py`'s `control_declarations`/`_motor_declarations`/`_activate_duration_ms` |
| `bed_module_name` | Called only by `hold_roster.load_control_declarations` |
| `Control("ping")` (`hold_streamer.PING`) | Compared against in `HoldStreamer._plan`/`_track_benchmark` via `_PING_ONLY`; the CU170's own `PING` (`beds/leggett_okin_hold.py:112`) is a separately constructed but value-equal `Control("ping")` declared in `control_declarations` |
| `PRESET_CONTROLS` | Read only by `LeggettOkinController._disarm_before_first_preset` |
| `okin_dummy_stage` | Called by `LeggettOkinController.stop_all`, `_disarm_before_first_preset`, and as the trailing recovery stage inside `okin_store_stages` (via `_dummy_stage`) |
| `AdjustableBedCoordinator._declaration_inputs` | Called only by `async_build_hold_pieces` |
| `HoldCapable.release_wire` | Called by `AdjustableBedCoordinator._async_disconnect_locked`; implemented by `LeggettOkinController` and the test doubles `_HoldingController`/`_RecordingController`/`_HoldCapableController` across `tests/test_coordinator.py`, `tests/test_entities.py`, `tests/test_hold_reconstructor.py`, `tests/test_hold_service.py`, `tests/test_leggett.py` |

## 6. Test inventory

### `tests/test_hold_streamer.py` (36 new tests, new file)

| Test | Slug |
|---|---|
| `test_a_bed_with_no_feedback_writes_confirmed_one_at_a_time` | `feedback-is-optional` |
| `test_a_bed_with_no_feedback_waits_on_no_cue` | `feedback-is-optional` |
| `test_an_unconfirmed_frame_yields_no_completion_to_read` | `ha-api-only` |
| `test_only_the_barrier_and_the_release_read_a_completion` | `ha-api-only` |
| `test_no_path_cancels_a_submitted_frame` | `ha-api-only` |
| `test_a_de_assertion_under_the_minimum_drains_first` | `press-registration-floors` |
| `test_expression_never_begins_after_de_assertion` | `press-registration-floors` |
| `test_the_frame_cell_counts_submissions` | `press-registration-floors` |
| `test_a_stop_mid_drain_drops_the_bit_at_once` | `floors-are-interruptible` |
| `test_no_floor_delays_the_stop` | `floors-are-interruptible` |
| `test_a_varying_cadence_does_not_cut_the_press_short` | `key-registration` |
| `test_a_stop_before_the_first_frame_emits_none` | `key-registration` |
| `test_each_press_is_expressed_once_in_the_order_it_was_held` | `press-state-fidelity` |
| `test_a_stall_splits_a_press_and_it_resumes` | `press-state-fidelity` |
| `test_the_frame_carries_the_whole_expressed_set` | `frame-is-the-or` |
| `test_a_clear_floor_gated_control_is_absent_from_the_frame` | `frame-is-the-or` |
| `test_exhausted_credit_withholds_the_send_never_a_bit` | `frame-is-the-or` |
| `test_the_set_emptying_writes_exactly_one_confirmed_zero_frame` | `single-release-per-lifecycle` |
| `test_pings_zero_bit_frames_end_no_lifecycle` | `single-release-per-lifecycle` |
| `test_ping_alone_writes_confirmed_bit_empty_frames` | `ping` |
| `test_another_control_beside_ping_ends_the_benchmark` | `ping` |
| `test_a_stop_ends_the_benchmark_like_any_hold` | `ping` |
| `test_a_control_drops_on_the_streamers_own_clock` | `deadlines-cross-every-tier` |
| `test_the_lapse_push_drops_it_first_when_it_arrives_first` | `deadlines-cross-every-tier` |
| `test_no_two_frames_leave_closer_than_the_emission_floor` | `paced-within-the-sustain-window` |
| `test_a_missed_wake_does_not_burst` | `paced-within-the-sustain-window` |
| `test_a_re_press_waits_for_the_clear_floor` | `clear-floor-spacing` |
| `test_a_running_lifecycle_keeps_the_value_it_read` | `options-latch-per-lifecycle` |
| `test_a_stage_advances_at_once_on_its_cue` | `cue-or-ceiling` |
| `test_a_ceiling_with_no_cue_fails_the_operation` | `cue-or-ceiling` |
| `test_a_cue_less_stage_completes_at_its_press_floor` | `cue-or-ceiling` |
| `test_a_held_control_is_withheld_and_pressed_again_after` | none |
| `test_an_operation_opens_once_an_unmet_press_floor_has_drained` | none |
| `test_a_preempting_operation_runs_the_owed_recovery_first` | none |
| `test_a_failed_stage_runs_the_recovery_stage_last` | `cancellation-leaves-bed-ready` |
| `test_a_frame_plan_names_the_fact_the_pump_branches_on` (module-level, not in a class) | `frame-is-the-or` |

### `tests/test_leggett_okin_hold.py` (37 new tests, new file)

| Test | Slug |
|---|---|
| `test_a_frame_is_the_or_of_the_controls_it_carries` | `frame-is-the-or` |
| `test_the_empty_set_encodes_as_the_release_frame` | `single-release-per-lifecycle` |
| `test_ping_carries_no_bit_of_its_own` | `ping` |
| `test_the_memory_slot_ladder_is_exact` | none |
| `test_the_store_arm_bit_is_never_a_slot_bit` | none |
| `test_revision_zero_takes_the_checksummed_framing` | none |
| `test_a_control_this_bed_has_no_keycode_for_is_a_defect` | none |
| `test_an_unconfirmed_frame_returns_no_completion` | `ha-api-only` |
| `test_a_confirmed_frame_returns_its_completion` | `release-is-a-write-request` |
| `test_one_trace_entry_per_wire_lifecycle` | none |
| `test_a_failed_unconfirmed_frame_raises_at_no_caller` | `receipts-positive-only` |
| `test_a_failed_confirmed_frame_carries_its_error` | `release-is-a-write-request` |
| `test_a_send_spends_one_credit_and_a_receipt_returns_it` | `receipt-paced-write-commands` |
| `test_credit_never_rises_above_the_window` | `receipt-paced-write-commands` |
| `test_every_ninth_receipt_returns_an_extra_credit` | `receipt-paced-write-commands` |
| `test_exhausted_credit_sends_the_barrier_and_then_nothing` | `receipt-paced-write-commands` |
| `test_the_barriers_completion_resets_credit_and_times_the_clear` | `receipt-paced-write-commands` |
| `test_a_completion_raises_credit_by_what_it_proved_and_never_lowers_it` | `release-is-a-write-request` |
| `test_elapsed_time_returns_no_credit` | `receipt-paced-write-commands` |
| `test_the_first_stall_per_bed_device_warns` | `receipt-paced-write-commands` |
| `test_a_silent_box_trips_the_guard` | `deficit-stops-the-stream` |
| `test_receipts_and_the_leak_keep_a_healthy_stream_clear` | `deficit-stops-the-stream` |
| `test_one_frame_leaks_per_second` | `deficit-guard` |
| `test_the_trip_is_the_latched_option` | `options-latch-per-lifecycle` |
| `test_a_pulse_is_two_transitions_of_the_light_bit` | `cue-or-ceiling` |
| `test_a_stage_starts_from_no_transitions` | `cue-or-ceiling` |
| `test_a_notification_that_moves_no_bit_counts_nothing` | none |
| `test_no_cue_is_met_before_one_begins` | `cue-or-ceiling` |
| `test_a_notification_is_a_receipt_and_a_cue_transition` | `receipts-positive-only` |
| `test_the_sick_transition_reaches_the_controller_once` | `deficit-stops-the-stream` |
| `test_a_store_arms_alone_then_presses_its_slot_key` | `store-cue-backstop` |
| `test_a_store_that_ends_without_its_cue_presses_dummy` | `dummy-disarms-a-pending-store` |
| `test_each_stage_carries_its_bits_alone` | `operations-express-alone` |
| `test_a_mode_chord_runs_one_stage_to_its_own_pulse_count` (parametrized ×2) | `cue-or-ceiling` |
| `test_the_disarming_press_waits_on_no_cue` | `cue-or-ceiling` |
| `test_the_store_stages_encode_the_arm_bit_and_then_the_slot_bit` | `release-edges-end-lifecycles` |
| `test_the_mode_chords_encode_the_two_captured_values` | none |

### `tests/test_hold_roster.py` (16 new, 1 removed)

| Test | Slug |
|---|---|
| `test_an_operation_control_prices_no_activate_duration` (new) | `operation-controls-command-path-only` |
| `test_a_bed_module_declaring_nothing_gets_an_empty_roster` (new; replaces `test_no_bed_module_declares_a_control_yet`) | `roster-declares-actions` |
| `test_a_motor_holds_and_activates_and_a_preset_only_holds` (new) | `roster-declares-actions` |
| `test_the_leggett_variant_resolves_the_same_bed_module` (new) | `roster-declares-actions` |
| `test_the_bed_declares_its_twenty_seven_controls` (new) | `roster-declares-actions` |
| `test_a_motors_activate_duration_is_the_configured_pulse` (new) | `roster-declares-actions` |
| `test_an_unsafe_pulse_delay_floors_at_the_proven_cadence` (new, parametrized ×3) | `roster-declares-actions` |
| `test_a_pulse_shorter_than_the_press_floor_lifts_to_it` (new) | `roster-declares-actions` |
| `test_a_motor_and_a_preset_hold_for_thirty_seconds` (new) | `roster-declares-actions` |
| `test_massage_controls_follow_the_entrys_massage_flag` (new) | `roster-declares-actions` |
| `test_an_older_motor_name_resolves_to_the_same_control` (new) | `roster-declares-actions` |
| `test_the_anti_snore_preset_is_memory_slot_three` (new) | `roster-declares-actions` |
| `test_every_momentary_and_latching_control_declares_the_same_floor` (new) | `press-floor-covers-debounce` |
| `test_the_staging_controls_carry_their_marks` (new) | `operation-controls-command-path-only` |
| `test_slot_three_declares_no_store_control` (new) | `operation-controls-command-path-only` |
| `test_ping_holds_for_two_minutes_and_prices_no_press` (new) | `ping` |

### `tests/test_leggett.py` (14 new, 15 removed — see §3 for the removed list and replacements)

| Test | Slug |
|---|---|
| `test_massage_wave_mode_is_advertised` (new) | none |
| `test_the_controller_is_hold_capable` (new) | `one-expression-door` |
| `test_a_one_shot_submits_its_controls_fixed_duration_intent` (new, parametrized ×12) | `one-expression-door` |
| `test_each_preset_recalls_only_its_own_slot` (new, parametrized ×4) | none |
| `test_a_per_motor_stop_fences_both_of_its_directions` (new, parametrized ×4) | `end-is-not-stop` |
| `test_the_held_set_and_the_release_reach_the_streamer` (new) | `frame-is-the-or` |
| `test_a_links_first_preset_key_is_preceded_by_the_dummy_press` (new) | `dummy-disarms-a-pending-store` |
| `test_a_staged_operation_carries_its_own_cues` (new) | `store-cue-backstop` |
| `test_a_fixed_slot_stages_nothing` (new) | `operation-controls-command-path-only` |
| `test_stop_all_releases_the_wire_and_then_presses_dummy` (new) | `latched-travel-stop` |
| `test_a_status_notification_is_a_receipt_and_a_cue_transition` (new) | `receipts-positive-only` |
| `test_a_missing_slot_submits_nothing` (new) | none |
| `test_only_memory_1_asserts_the_memory_1_control` (new; replaces `test_only_memory_1_recalls_the_memory_1_keycode`) | none |
| `test_the_control_mode_gestures_are_advertised` (new; replaces `test_control_modes_use_the_exact_special_command_lifecycle`) | none |

`test_write_stream_is_unconfirmed_and_wall_clock_paced` (existing, not renamed): docstring rewritten
to explain why `write_command` keeps its own contract even though the streamer writes through its
own writer; the test body's assertions are unchanged.

### `tests/test_coordinator.py` — class `TestHoldPieces` (2 new tests)

| Test | Slug |
|---|---|
| `test_an_ordered_disconnect_releases_before_it_stops_notifying` | `release-before-disconnect` |
| `test_an_unsolicited_drop_writes_no_release` | `release-before-disconnect` |

### `tests/test_diagnostics.py` — class `TestHoldDiagnostics` (2 new tests)

| Test | Slug |
|---|---|
| `test_the_payload_carries_the_reconstructors_counters` | `rejection-surfacing` |
| `test_the_payload_carries_the_streamers_own_counters` | `receipt-paced-write-commands` |

### `tests/test_entities.py` — class `TestHoldCapableEntities` (5 new tests)

| Test | Slug |
|---|---|
| `test_a_preset_button_refuses_a_press` | `presets-hold-only` |
| `test_an_operation_button_keeps_its_declared_cancel` | none |
| `test_a_tap_never_cancels_the_command_in_flight` | `one-expression-door` |
| `test_a_cover_renders_the_held_set` | `entity-state-from-authority` |
| `test_a_cover_stop_fences_the_control_without_the_command_path` | `end-is-not-stop` |

### `tests/test_controller_contract.py` (1 new test)

| Test | Slug |
|---|---|
| `test_only_the_cu170_is_hold_capable` (parametrized over `SUPPORTED_BED_TYPES`) | none |

### Fixture-only changes (no new/removed test function)

| File | Change |
|---|---|
| `tests/test_hold_reconstructor.py` | `_RecordingController` test double gains `self.releases = 0` and `release_wire(self) -> None`, matching `HoldCapable`'s widened contract |
| `tests/test_hold_service.py` | `_declare(STORE_1, ...)`'s `activate_duration_ms` argument flips `223` → `None` (tracks §2's `ControlDeclaration` reversal); `_HoldCapableController` gains `release_wire` |
| `tests/test_init.py` | `test_setup_builds_the_roster_before_the_first_connect`'s local `_declarations` stub gains the `inputs` parameter (tracks `load_control_declarations`'s new signature); `test_timed_move_service_accepts_leggett_okin_pillow`'s docstring and body are rewritten to assert against `hold_reconstructor.submit` instead of mocked `move_pillow_up`/`move_pillow_stop` calls (same test name, not counted as new) |

### Total

113 new test functions across 8 files (2 new files, 6 changed files); 16 test functions deleted (1
replaced within `test_hold_roster.py`, 15 within `test_leggett.py` — see §3). 17 of the 113 new
tests carry no kebab-slug in their docstring (listed above as "none" — each still has a descriptive
test name).
