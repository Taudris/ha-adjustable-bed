# Change inventory: hold-contract-and-reconstructor

Extracted before the rebase onto upstream 4.0, against the 3.7.1-era tree, so its line numbers
and several of its attachment points describe that tree. The commit message records where 4.0
moved a seam and where the hold pieces now hang.

Tier 0 extraction pass. Range `03f2413..d9608ee`, 7 commits:

| Commit | Subject |
|---|---|
| `b50d4c6` | feat: hold intent value types and the control roster |
| `2f3ac4a` | feat: HoldCapable, the controller hold contract |
| `f34acbc` | feat: the hold reconstructor |
| `f48b7cc` | feat: the coordinator builds and holds the roster and the reconstructor |
| `c479857` | feat: the send_intents service and its handler |
| `7a133dc` | feat: timed_move and goto_preset submit hold intents on a hold-capable bed |
| `0fae9d2` | docs: the send_intents service and the hold modules in AGENTS.md |

No judgment, no recommendations. Every claim below is either read directly off the diff or
traced to the surrounding file.

## 1. Public surface, added

### Modules

| Module | File |
|---|---|
| `hold_roster` | `custom_components/adjustable_bed/hold_roster.py` |
| `hold_capability` | `custom_components/adjustable_bed/hold_capability.py` |
| `hold_intent` | `custom_components/adjustable_bed/hold_intent.py` |
| `hold_reconstructor` | `custom_components/adjustable_bed/hold_reconstructor.py` |

### Classes, enums, and type aliases

| Name | Kind | File:line |
|---|---|---|
| `Control` | frozen dataclass, slots; field `name: str` | `hold_roster.py:24-32` |
| `ActionKind` | `StrEnum`; `HOLD`, `ACTIVATE` | `hold_roster.py:35-39` |
| `ControlMark` | `StrEnum`; `OPERATION`, `DELIBERATE_ONLY` | `hold_roster.py:42-46` |
| `ControlDeclaration` | frozen dataclass, slots; fields `control`, `actions: frozenset[ActionKind]`, `ttl_max_ms: int`, `activate_duration_ms: int \| None`, `press_min_frames: int`, `press_min_ms: int`, `marks: frozenset[ControlMark] = frozenset()` | `hold_roster.py:49-59` |
| `ControlRoster` | class | `hold_roster.py:70` |
| `HoldCapable` | `ABC`; one abstract method `hold` | `hold_capability.py:22-32` |
| `SenderId` | `NewType("SenderId", str)` | `hold_intent.py:25` |
| `IntentId` | `NewType("IntentId", str)` | `hold_intent.py:26` |
| `Hold` | frozen dataclass, slots; field `ttl_ms: int` | `hold_intent.py:29-33` |
| `Activate` | frozen dataclass, slots; no fields | `hold_intent.py:36-37` |
| `IntentAction` | type alias, `Hold \| Activate` | `hold_intent.py:41` |
| `IntentSample` | frozen dataclass, slots; fields `intent_id: IntentId`, `control: Control`, `action: IntentAction` | `hold_intent.py:44-50` |
| `IntentSampleSet` | frozen dataclass, slots; fields `sender: SenderId`, `seq: int`, `samples: tuple[IntentSample, ...]` | `hold_intent.py:53-59` |
| `HoldIntent` | frozen dataclass, slots; fields `control: Control`, `began: float`, `last_refresh: float`, `deadline: float` | `hold_intent.py:62-74` |
| `HoldOutcome` | `StrEnum`; `COMPLETED`, `INTERRUPTED`, `FAILED` | `hold_intent.py:77-82` |
| `HoldTarget` | `Protocol`; one method `connect_on_demand() -> None` | `hold_reconstructor.py:55-60` |
| `IntentOrigin` | `StrEnum`; `SAMPLED_HOLD`, `ONE_SHOT` | `hold_reconstructor.py:62-68` |
| `_IntentRecord` | dataclass, slots (module-private); fields `intent: HoldIntent`, `origin: IntentOrigin`, `evict_at: float`, `ended: bool = False`, `expressed: bool = False`, `expression_lost: bool = False` | `hold_reconstructor.py:70-83` |
| `_SenderState` | dataclass, slots (module-private); fields `high_water: int`, `last_activity: float` | `hold_reconstructor.py:87-92` |
| `HoldReconstructor` | class | `hold_reconstructor.py:94` |

### Functions and methods

| Signature | File:line |
|---|---|
| `ControlDeclaration.__post_init__(self) -> None` | `hold_roster.py:61-68` |
| `ControlRoster.__init__(self, declarations: Iterable[ControlDeclaration]) -> None` | `hold_roster.py:78` |
| `ControlRoster.empty(cls) -> ControlRoster` (classmethod) | `hold_roster.py:85` |
| `ControlRoster.controls(self) -> tuple[Control, ...]` (property) | `hold_roster.py:90` |
| `ControlRoster.find(self, name: str) -> Control \| None` | `hold_roster.py:94` |
| `ControlRoster.declaration(self, control: Control) -> ControlDeclaration` | `hold_roster.py:103` |
| `ControlRoster.supports(self, control: Control, action: ActionKind) -> bool` | `hold_roster.py:111` |
| `ControlRoster.is_operation(self, control: Control) -> bool` | `hold_roster.py:115` |
| `ControlRoster.clamp_ttl_ms(self, control: Control, ttl_ms: int) -> int` | `hold_roster.py:119` |
| `ControlRoster.activate_duration_ms(self, control: Control) -> int` | `hold_roster.py:123` |
| `ControlRoster.declares_hold(self) -> bool` (property) | `hold_roster.py:135` |
| `motor_control_name(motor: str, direction: str) -> str` | `hold_roster.py:142` |
| `preset_control_name(slot: int) -> str` | `hold_roster.py:147` |
| `load_control_declarations(coordinator: AdjustableBedCoordinator) -> tuple[ControlDeclaration, ...]` (async) | `hold_roster.py:152-162`; returns `()` unconditionally |
| `HoldCapable.hold(self, held: Mapping[Control, float]) -> None` (abstract) | `hold_capability.py:25-32` |
| `HoldReconstructor.__init__(self, hass, roster, target, clock=None) -> None` | `hold_reconstructor.py:102` |
| `HoldReconstructor.use_roster(self, roster: ControlRoster) -> None` | `hold_reconstructor.py:133` |
| `HoldReconstructor.handle_samples(self, message: IntentSampleSet) -> None` | `hold_reconstructor.py:142` |
| `HoldReconstructor.submit(self, control: Control, ttl_ms: int) -> Future[HoldOutcome]` | `hold_reconstructor.py:168` |
| `HoldReconstructor.stop(self, controls: Iterable[Control]) -> None` | `hold_reconstructor.py:190` |
| `HoldReconstructor.stop_all(self) -> None` | `hold_reconstructor.py:200` |
| `HoldReconstructor.quiesce(self) -> None` | `hold_reconstructor.py:205` |
| `HoldReconstructor.attach(self, controller: HoldCapable) -> None` | `hold_reconstructor.py:224` |
| `HoldReconstructor.detach(self) -> None` | `hold_reconstructor.py:248` |
| `HoldReconstructor.held(self) -> Mapping[Control, HoldIntent]` (property) | `hold_reconstructor.py:273` |
| `HoldReconstructor.holds_anything(self) -> bool` (property) | `hold_reconstructor.py:278` |
| `HoldReconstructor.async_add_listener(self, listener) -> Callable[[], None]` | `hold_reconstructor.py:283` |
| `HoldReconstructor.diagnostics(self) -> dict[str, Any]` (property) | `hold_reconstructor.py:298` |
| `HoldReconstructor._apply(self, sender, sample, now) -> None` | `hold_reconstructor.py:310` |
| `HoldReconstructor._release(self, key, record) -> None` | `hold_reconstructor.py:329` |
| `HoldReconstructor._refresh(self, record, sample, now) -> None` | `hold_reconstructor.py:342` |
| `HoldReconstructor._sample_ttl_s(self, control, action) -> float` | `hold_reconstructor.py` |
| `HoldReconstructor._bounded_deadline(self, intent, deadline) -> float` | `hold_reconstructor.py:367` |
| `HoldReconstructor._stop(self, controls) -> None` | `hold_reconstructor.py:372` |
| `HoldReconstructor._settle(self, now, *, push=True) -> None` | `hold_reconstructor.py:382` |
| `HoldReconstructor._sweep(self, now) -> None` | `hold_reconstructor.py:394` |
| `HoldReconstructor._lapse(self, key, record) -> None` | `hold_reconstructor.py:409` |
| `HoldReconstructor._end(self, key, record) -> None` | `hold_reconstructor.py:419` |
| `HoldReconstructor._derive(self, now) -> dict[Control, HoldIntent]` | `hold_reconstructor.py:429` |
| `HoldReconstructor._push(self) -> None` | `hold_reconstructor.py:437` |
| `HoldReconstructor._mark_expressed(self, held) -> None` | `hold_reconstructor.py:448` |
| `HoldReconstructor._resolve(self, key, outcome) -> None` | `hold_reconstructor.py:454` |
| `HoldReconstructor._arm_pending_event(self, now) -> None` | `hold_reconstructor.py:461` |
| `HoldReconstructor._cancel_pending_event(self) -> None` | `hold_reconstructor.py:484` |
| `HoldReconstructor._async_pending_event(self, _fired_at) -> None` | `hold_reconstructor.py:491` |
| `HoldReconstructor._notify_listeners(self) -> None` | `hold_reconstructor.py:496` |
| `_record(control, ttl_s, origin, now) -> _IntentRecord` (module function) | `hold_reconstructor.py:505` |
| `_newest_per_control(records) -> dict[Control, HoldIntent]` (module function) | `hold_reconstructor.py:516` |
| `_resolve_sampled_control(coordinator, name) -> Control` | `services.py:181` |
| `_resolve_sampled_action(coordinator, control, sample) -> IntentAction` | `services.py:209` |
| `_resolve_sample(coordinator, sample) -> IntentSample` | `services.py:249` |
| `handle_send_intents(call: ServiceCall) -> None` (async) | `services.py:259` |
| `_submit_hold(coordinator, control_name, ttl_ms) -> None` (async) | `services.py:295` |

### Constants

| Name | Value | File:line |
|---|---|---|
| `SENDER_QUIET_HORIZON_S` | `60.0` | `hold_reconstructor.py:47` |
| `_SUBMISSION_SENDER` | `SenderId("direct-submission")` (module-private) | `hold_reconstructor.py:52` |
| `SERVICE_SEND_INTENTS` | `"send_intents"` | `services.py:62` |
| `ATTR_SENDER_ID` | `"sender_id"` | `services.py:78` |
| `ATTR_SEQ` | `"seq"` | `services.py:79` |
| `ATTR_SAMPLES` | `"samples"` | `services.py:80` |
| `ATTR_INTENT_ID` | `"intent_id"` | `services.py:81` |
| `ATTR_CONTROL` | `"control"` | `services.py:82` |
| `ATTR_ACTION` | `"action"` | `services.py:83` |
| `ATTR_TTL_MS` | `"ttl_ms"` | `services.py:84` |

### Coordinator members (new, on the existing `AdjustableBedCoordinator`)

| Member | Kind | File:line |
|---|---|---|
| `control_roster` | property, returns `ControlRoster` | `coordinator.py:1342` |
| `hold_reconstructor` | property, returns `HoldReconstructor` | `coordinator.py:1347` |
| `hold_diagnostics` | property, returns `dict[str, Any]` (delegates to `self._hold_reconstructor.diagnostics`) | `coordinator.py:1352` |
| `async_build_hold_pieces(self) -> None` | async method | `coordinator.py:1356-1364` |
| `_async_await_hold_roster_rebuild(self) -> None` | async method | `coordinator.py:1366-1371` |
| `connect_on_demand(self) -> None` | `@callback` method (this is the `HoldTarget` implementation) | `coordinator.py:1375-1388` |
| `_hold_connection_state_changed(self, connected: bool) -> None` | method, registered via `register_connection_state_callback` | `coordinator.py:1390-1396` |
| `_held_set_changed(self) -> None` | method, registered via `HoldReconstructor.async_add_listener` | `coordinator.py:1398-1420` |
| `_control_roster` | instance attribute, `ControlRoster` | `coordinator.py:421` |
| `_hold_reconstructor` | instance attribute, `HoldReconstructor` | `coordinator.py:422` |
| `_hold_roster_rebuild` | instance attribute, `asyncio.Task[None] \| None` | `coordinator.py:423` |
| `_hold_connect_task` | instance attribute, `asyncio.Task[bool] \| None` | `coordinator.py:424` |
| `_disconnect_after_hold_release` | instance attribute, `bool` | `coordinator.py:425` |

### Service registration

| Item | File:line |
|---|---|
| `adjustable_bed.send_intents` service, registered with `handle_send_intents` and a `vol.Schema` (`device_id`, `sender_id`, `seq: int >= 0`, `samples`: non-empty list of `{intent_id, control, action: hold\|activate, ttl_ms?}`) | `services.py:1131-1163` area |
| `services.yaml` → `send_intents:` entry (`name`, `description`, `fields.device_id/sender_id/seq/samples`) | `services.yaml` |
| `services.yaml` → `goto_preset.duration_ms` field | `services.yaml` |

### Translation / string keys (added to both `strings.json` and `translations/en.json`, identical content)

| Key path | File |
|---|---|
| `services.goto_preset.fields.duration_ms` (name + description) | `strings.json`, `translations/en.json` |
| `services.send_intents` (name, description, `fields.device_id`, `fields.sender_id`, `fields.seq`, `fields.samples`) | `strings.json`, `translations/en.json` |
| `exceptions.hold_not_supported` | `strings.json`, `translations/en.json` |
| `exceptions.hold_control_unknown` | `strings.json`, `translations/en.json` |
| `exceptions.hold_action_unsupported` | `strings.json`, `translations/en.json` |
| `exceptions.hold_operation_control_sampled` | `strings.json`, `translations/en.json` |
| `exceptions.hold_ttl_mismatch` | `strings.json`, `translations/en.json` |

### Test files (new)

| File | New test functions |
|---|---|
| `tests/test_hold_reconstructor.py` | 47 |
| `tests/test_hold_roster.py` | 15 |
| `tests/test_hold_service.py` | 15 |

(Function-level detail in §6.)

### AGENTS.md

Doc-only addition (commit `0fae9d2`): a `adjustable_bed.send_intents` row in the services table,
and four new module-list lines (`hold_roster.py`, `hold_intent.py`, `hold_capability.py`,
`hold_reconstructor.py`) in the architecture section. No code.

## 2. Public surface, changed or widened

All entries are `AdjustableBedCoordinator` methods (`custom_components/adjustable_bed/coordinator.py`).
Signatures are unchanged in every case; each gained a branch or a call that changes what a caller
observes.

| Member | Before | After | File:line | Commit |
|---|---|---|---|---|
| `__init__(self, hass, entry)` | Did not construct a roster or reconstructor, did not register any hold-related callback or listener. | Constructs `self._control_roster = ControlRoster.empty()` and `self._hold_reconstructor = HoldReconstructor(hass, self._control_roster, self)`; initializes `_hold_roster_rebuild`, `_hold_connect_task`, `_disconnect_after_hold_release`; calls `self.register_connection_state_callback(self._hold_connection_state_changed)` and `self._hold_reconstructor.async_add_listener(self._held_set_changed)`. | `coordinator.py:421-427` | `f48b7cc` |
| `_apply_runtime_bed_type_correction(self, corrected_bed_type: str) -> bool` | Updated `self._bed_type` and pulse defaults; returned whether the type changed. No side effect beyond `self._bed_type`/pulse fields. | Same return contract; when `bed_type_changed` is true, additionally schedules `self._hold_roster_rebuild = self.hass.async_create_task(self.async_build_hold_pieces(), name=...)`. | `coordinator.py:521-529` | `f48b7cc` |
| `async_shutdown(self) -> None` | Called `self._cancel_passive_position_reconciliation_task()` then `await self.async_disconnect()`. | Same two calls, with `self._hold_reconstructor.quiesce()` inserted between them (before the disconnect). | `coordinator.py:3231-3238` | `f48b7cc` |
| `_async_idle_disconnect(self) -> None` | Inside `self._command_lock`: returned early if the disconnect timer had been re-armed; otherwise disconnected under `self._lock`. | Same, with a second early-return guard added after the re-arm check: `if self._hold_reconstructor.holds_anything: return` (logged at debug). | `coordinator.py:3395-3413` | `f48b7cc` |
| `async_stop_command(self) -> None` | First action was `_LOGGER.info("Stop requested...")`, then incremented `_cancel_counter` and set `_cancel_command`. | First action is now `self._hold_reconstructor.stop_all()`, ahead of the log line, the cancel counter, and acquiring `self._command_lock`; runs even if the link is down. | `coordinator.py:3524-3530` | `f48b7cc` |
| `_async_finish_controller_operation(self, *, entry_cancel_count, skip_disconnect, operation_name) -> None` | `if (disconnect_after_operation_enabled and not skip_disconnect and not command_preempted): disconnect and return`; `elif command_preempted: log skip`. | Same disconnect condition (renamed `disconnect_wanted`) is now combined with `holding = self._hold_reconstructor.holds_anything`: disconnects only `if disconnect_wanted and not holding`; when `disconnect_wanted` is true but `holding` is true, sets `self._disconnect_after_hold_release = True` (logged) instead of disconnecting; `elif command_preempted` unchanged; `self._reset_disconnect_timer()` now runs unconditionally at the end of the non-disconnecting paths. | `coordinator.py:3613-3643` | `f48b7cc` |
| `async_seek_position(...)` (the per-iteration `finally` block, not the whole method) | `if self._disconnect_after_operation_enabled(): disconnect` else reset timer. | `if self._disconnect_after_operation_enabled() and not self._hold_reconstructor.holds_anything: disconnect` else reset timer. | `coordinator.py:4624-4636` | `f48b7cc` |

Also widened, in `services.py` (not `AdjustableBedCoordinator`, but existing public service handlers):

| Member | Before | After | File:line | Commit |
|---|---|---|---|---|
| `handle_goto_preset(call: ServiceCall) -> None` | Unconditionally ran `await coordinator.async_execute_controller_command(lambda ctrl, p=preset: ctrl.preset_memory(p))`. | Reads new optional `duration_ms` from the call; when `isinstance(controller, HoldCapable)`, calls `await _submit_hold(coordinator, preset_control_name(preset), duration_ms)` instead; otherwise unchanged. | `services.py:328-366` area (branch at `:363`) | `7a133dc` |
| `handle_timed_move(call: ServiceCall) -> None` | Always resolved `motor_configs[motor]` and issued the pulse-based move/stop calls. | When `isinstance(controller, HoldCapable)`, calls `await _submit_hold(coordinator, motor_control_name(motor, direction), duration_ms); continue` before reaching the pulse-config lookup; every other bed's path is unchanged. | `services.py:662-720` area (branch at `:714`) | `7a133dc` |
| `async_register_services(hass) -> None` (schema registration, not a call-site contract) | `goto_preset` schema had `CONF_DEVICE_ID`, `ATTR_PRESET` only. | `goto_preset` schema gains `vol.Optional(ATTR_DURATION_MS)` bounded by `MIN_TIMED_MOVE_DURATION_MS`/`MAX_TIMED_MOVE_DURATION_MS` (`services.py:1058`); a new `send_intents` schema is registered (`services.py:1131-1163` area). | `services.py` | `7a133dc`, `c479857` |

Also widened, in `__init__.py` (the entry's setup path):

| Member | Before | After | File:line | Commit |
|---|---|---|---|---|
| `async_setup_entry(hass, entry) -> bool` | Constructed the coordinator, ensured its device-registry entry, then started availability telemetry and ran the first connect. | Same, with `await coordinator.async_build_hold_pieces()` inserted between the device-registry entry and the availability telemetry (+4 lines, the call and its comment). The roster and the reconstructor are therefore built before the first connect, and before the `hass.data[DOMAIN][entry.entry_id] = coordinator` assignment, which every setup path makes later inside `_async_finish_entry_setup` (`:257`, called at `:290` and `:469`). | `__init__.py:316-321` | `f48b7cc` |

## 3. Removed

None. Every `-` line in the diff is part of an in-place restructuring of a conditional already
captured in §2 (the `_async_finish_controller_operation` guard extraction, the `command_preempted`
branch reshape, the seek-loop `finally` guard, and `handle_goto_preset`'s call moving from
unconditional to the `else` branch of the new `isinstance` check). No module, class, function,
method, constant, service, service field, translation key, or test was deleted. Confirmed with
`git diff 03f2413..HEAD | grep -E '^-[^-]'` restricted to each of `custom_components/`, `tests/`,
and the `.yaml`/`.json`/`AGENTS.md` files.

## 4. Decisions the code introduces

### hold_roster.py — commit `b50d4c6`

- **Guard, `ControlDeclaration.__post_init__`** (`hold_roster.py:61-68`): raises `ValueError` when
  `(ActionKind.ACTIVATE in self.actions) != (self.activate_duration_ms is not None)` — a control
  declares an Activate duration if and only if it supports Activate.
- **Default/stub, `load_control_declarations`** (`hold_roster.py:152-162`): returns `()`
  unconditionally; the `coordinator` parameter is discarded (`del coordinator`). No bed module is
  consulted in this diff, so every roster is empty and no bed is hold-capable yet.
- **Construction site, `ControlRoster.__init__`**: builds `self._declarations` as a `dict` keyed by
  `declaration.control.name` from the `declarations` iterable it is constructed with; the
  coordinator is the only caller (`coordinator.py:1359`, via `async_build_hold_pieces`).
- **Guard, `ControlRoster.declaration`**: raises `KeyError` (undecorated, i.e., the dict lookup's own
  exception) when the control's name is not in `_declarations`.
- **Guard, `ControlRoster.activate_duration_ms`** (`hold_roster.py:123-131`): raises `ValueError` when
  the declaration's `activate_duration_ms` is `None`.
- **Derived value, `ControlRoster.declares_hold`** (`hold_roster.py:135-139`): `True` when any
  declaration's `actions` contains `ActionKind.HOLD`.
- **Name-construction functions**: `motor_control_name(motor, direction) -> f"motor-{motor}-{direction}"`;
  `preset_control_name(slot) -> f"preset-{slot}"`. Both are pure string formatting with no validation.

### hold_capability.py — commit `2f3ac4a`

- **Type decision**: `HoldCapable` is a nominal `ABC`, not a `runtime_checkable` `Protocol`; a
  controller must inherit it explicitly for `isinstance(controller, HoldCapable)` to be true anywhere
  it is checked (`coordinator.py:1393`, `services.py:363`, `services.py:714`).
- No implementer exists in this diff; `hold()` is abstract only.

### hold_intent.py — commit `b50d4c6`

- **Unit decision** (stated in the module docstring, not a branch): `ttl_ms` is `int` milliseconds;
  `began`, `last_refresh`, `deadline` are `float` seconds on the event loop's monotonic clock.
- `SenderId`/`IntentId` are distinct `NewType(str)` wrappers — not interchangeable at the type level.

### hold_reconstructor.py — commit `f34acbc`

- **Constant**: `SENDER_QUIET_HORIZON_S = 60.0` (`hold_reconstructor.py:47`) — how long a sender's
  high-water survives after its last record activity.
- **Constant**: `_SUBMISSION_SENDER = SenderId("direct-submission")` (`hold_reconstructor.py:52`) —
  the reserved sender id every `submit()` call is filed under.
- **Guard, `handle_samples`** (`hold_reconstructor.py:143-155`): drops the whole message
  (`self._rejected_messages += 1`, return) when `self._closed` or when a known sender's
  `message.seq <= sender.high_water`. Ordering inside the method: sweep-then-evict runs before
  applying samples, which runs before `_settle`.
- **Guard, `submit`** (`hold_reconstructor.py:168-186` area): if `self._closed`, resolves the
  returned future with `HoldOutcome.INTERRUPTED` immediately and does not create a record.
- **Branch, `_apply`** (`hold_reconstructor.py:310-327`): three-way — a `Hold` sample with
  `ttl_ms == 0` calls `_release`; an unknown `(sender, intent_id)` key creates a new record with
  origin `SAMPLED_HOLD` (for `Hold`) or `ONE_SHOT` (for `Activate`); an existing key calls `_refresh`.
- **Guard, `_release`**: no-op if `record is None` (already evicted or never seen).
- **Decision, `_refresh`** (`hold_reconstructor.py:342-365`): always moves `record.evict_at`; if the
  record is already `ended`, increments `self._dropped_renewals` and returns without touching the
  intent's deadline; otherwise replaces the deadline outright (via `_bounded_deadline` for a `Hold`,
  unchanged for an `Activate`) — a refresh never extends past the newest ask.
- **Bound, `_bounded_deadline`** (`hold_reconstructor.py:367-370`): caps a deadline at
  `intent.began + (roster.declaration(control).ttl_max_ms / 1000)` — the control's declared lifetime,
  measured from the press's own start.
- **Branch, `_stop`** (`hold_reconstructor.py:372-380`): ends and resolves `INTERRUPTED` every
  unended record whose control is in `controls`, or every unended record when `controls is None`
  (`stop_all`'s path).
- **Guard, `quiesce`** (`hold_reconstructor.py:205-223` area): no-op if already `self._closed`.
  Otherwise ends every unended record with `INTERRUPTED`, clears `_records` and `_senders`, cancels
  the pending timer, sets `self._closed = True`, and — only if `self._held` was non-empty — clears it
  and notifies listeners, then always calls `self._push()` once more (pushing the empty set).
- **Filter, `attach`** (`hold_reconstructor.py:224-247` area): pushes only records where
  `origin is ONE_SHOT and not ended and not expressed and intent.deadline > now` — a sample-borne
  `Hold` is never pushed at attach time; it waits for its sender's next sample.
- **Guard, `detach`** (`hold_reconstructor.py:248-249`): returns immediately if
  `self._controller is None`, so the coordinator's own "lost connection" report at the start of every
  connect attempt does not end intents a second time. Otherwise sets `expression_lost = True` on every
  unended record, and additionally ends + resolves `FAILED` any record whose `origin is ONE_SHOT`.
- **Counter, `_lapse`** (`hold_reconstructor.py:409-417`): increments `self._rejected_submissions` when
  a lapsing record was never `expressed`.
- **Branch, `_end`** (`hold_reconstructor.py:419-427`): resolves `COMPLETED` if
  `expressed and not expression_lost`, else `FAILED`.
- **Filter, `_derive`** (`hold_reconstructor.py:429-435`): the held set is the newest-per-control over
  records where `not ended and intent.deadline > now`.
- **Branch, `_push`** (`hold_reconstructor.py:437-446`): if no controller is attached, calls
  `self._target.connect_on_demand()` only when `self._held` is non-empty (an empty set never summons
  a connect); if a controller is attached, pushes `{control: intent.deadline}` and marks every
  contributing record `expressed = True`.
- **Guard, `_resolve`** (`hold_reconstructor.py:454-459`): only sets the future's result if
  `future is not None and not future.done()` — a caller-cancelled future is left alone.
- **Scheduling, `_arm_pending_event`** (`hold_reconstructor.py:461-483`): cancels any existing timer,
  then computes the minimum of every record's `evict_at`, every unended record's `intent.deadline`,
  and every sender-with-no-records' `last_activity + SENDER_QUIET_HORIZON_S`; if that set is empty, no
  timer is armed; otherwise `async_call_later(hass, max(0.0, min(instants) - now), self._async_pending_event)`.
- **Fault isolation, `_notify_listeners`** (`hold_reconstructor.py:496-503`): catches and logs
  (`_LOGGER.warning`) any `Exception` per listener, continuing to the rest.
- **Tie-break, `_newest_per_control`** (`hold_reconstructor.py:516-527`): a record replaces the
  current entry only when `record.intent.began > current.began` (strict `>`), so on an exact tie the
  first-seen record in iteration order is kept.

### coordinator.py — commit `f48b7cc`

- **Construction site**: `self._control_roster` and `self._hold_reconstructor` are both built inside
  `AdjustableBedCoordinator.__init__` (not lazily, not only after setup) — `coordinator.py:421-422`.
  The commit message states this as a deviation from the plan (which built both only inside
  `async_build_hold_pieces`), justified by tests constructing the coordinator directly and calling
  `async_stop_command`/`async_shutdown`/the disconnect predicates without a setup step.
- **Call-site registration** (`coordinator.py:426-427`): `__init__` registers
  `self._hold_connection_state_changed` via the existing `register_connection_state_callback` fan-out,
  and registers `self._held_set_changed` via `HoldReconstructor.async_add_listener` (whose contract
  replays once at registration time, per `hold_reconstructor.py`'s own doc).
- **Guard, `_apply_runtime_bed_type_correction`** (`coordinator.py:522-529`): the roster rebuild is
  scheduled only `if bed_type_changed`, as an `asyncio.Task` via `self.hass.async_create_task`, named
  `f"adjustable_bed_hold_roster_{self._address}"`.
- **Await site, `_async_await_hold_roster_rebuild`** (`coordinator.py:1366-1371`): guards
  `if rebuild is None: return`; otherwise clears `self._hold_roster_rebuild = None` *before* awaiting
  the captured task. Called once, at `coordinator.py:2545`, inside the connect path after
  `_apply_runtime_bed_type_correction`.
- **De-dupe guard, `connect_on_demand`** (`coordinator.py:1382-1383`): returns early if
  `self._hold_connect_task is not None and not self._hold_connect_task.done()` — at most one
  background connect task in flight regardless of how many pushes ask for one.
- **Branch, `_hold_connection_state_changed`** (`coordinator.py:1390-1396`): reads `self._controller`
  (not the callback's own `connected` argument) to decide whether to attach; attaches only
  `if connected and isinstance(controller, HoldCapable)`; every other case (including
  `connected=True` with a non-`HoldCapable` controller) calls `detach()` unconditionally.
- **Branch, `_held_set_changed`** (`coordinator.py:1398-1420`): computes
  `link_idle = not holds_anything and self._client is not None and self._client.is_connected`;
  returns early `if not link_idle`. When idle, `if self._disconnect_after_hold_release:` clears the
  flag and starts a background disconnect task named
  `f"adjustable_bed_hold_release_disconnect_{self._address}"`; otherwise (implied, past the shown
  hunk) resets the idle disconnect timer.
- **Ordering, `async_shutdown`** (`coordinator.py:3234-3238`): `self._hold_reconstructor.quiesce()`
  runs before `await self.async_disconnect()`, with a comment stating the reason: the empty set must
  reach the controller while the link still exists, and an awaiting `submit()` caller must not hang
  through teardown.
- **Guard, `_async_idle_disconnect`** (`coordinator.py:3404-3411`): added after the existing
  timer-re-armed guard, before the `async with self._lock` disconnect — returns (logged) if
  `self._hold_reconstructor.holds_anything`.
- **Ordering, `async_stop_command`** (`coordinator.py:3526-3530`): `self._hold_reconstructor.stop_all()`
  is the method's first statement, ahead of the info log, the cancel counter increment, and acquiring
  `self._command_lock` — runs even when the link is down (the comment states this is why it precedes
  the early return further down).
- **Branch, `_async_finish_controller_operation`** (`coordinator.py:3613-3643`): `holding` is read once
  via `self._hold_reconstructor.holds_anything`; disconnect happens only `if disconnect_wanted and not
  holding`; `if disconnect_wanted` (implicitly holding, since the prior branch returned) sets
  `self._disconnect_after_hold_release = True`; `self._reset_disconnect_timer()` is unconditional at
  the end of every non-disconnecting path.
- **Guard, `async_seek_position` finally block** (`coordinator.py:4626-4629`): the existing
  disconnect-after-seek condition gains `and not self._hold_reconstructor.holds_anything`.

### services.py — commits `c479857`, `7a133dc`

- **Guard, `_resolve_sampled_control`** (`services.py:189-206`): raises `ServiceValidationError`
  (`translation_key="hold_control_unknown"`) when `roster.find(name) is None`; raises
  `ServiceValidationError` (`translation_key="hold_operation_control_sampled"`) when
  `roster.is_operation(control)` is true, before returning.
- **Guard, `_resolve_sampled_action`** (`services.py:220-246`): raises `ServiceValidationError`
  (`translation_key="hold_action_unsupported"`) when `not coordinator.control_roster.supports(control,
  kind)`; raises `ServiceValidationError` (`translation_key="hold_ttl_mismatch"`) when
  `(kind is ActionKind.HOLD) != (ttl_ms is not None)`; otherwise returns `Hold(ttl_ms)` when `ttl_ms is
  not None`, else `Activate()`.
- **Ordering, `handle_send_intents`** (`services.py:259-292`): per `device_id`, raises
  `device_not_found` if no coordinator resolves; raises `hold_not_supported` if
  `not coordinator.control_roster.declares_hold` (read from the roster, never from `coordinator._controller`);
  only then builds the full `IntentSampleSet` via a generator expression over `_resolve_sample` (so
  every sample resolves, and the first failure raises, before any sample reaches
  `coordinator.hold_reconstructor.handle_samples`).
- **Default, `_submit_hold`** (`services.py:295-...`): raises `hold_control_unknown` if
  `roster.find(control_name) is None`; otherwise reads `press_min_ms =
  roster.declaration(control).press_min_ms` and calls
  `coordinator.hold_reconstructor.submit(control, press_min_ms if ttl_ms is None else ttl_ms)`,
  awaited before returning — `ttl_ms=None` (the caller's default when `duration_ms` is omitted) means
  "hold for the control's press minimum."
- **Branch, `handle_goto_preset`** (`services.py:363-366` area): `if isinstance(controller,
  HoldCapable): await _submit_hold(coordinator, preset_control_name(preset), duration_ms)`; `else:`
  the pre-existing `async_execute_controller_command(...)` call, unchanged.
- **Branch, `handle_timed_move`** (`services.py:714-718` area): `if isinstance(controller,
  HoldCapable): await _submit_hold(coordinator, motor_control_name(motor, direction), duration_ms);
  continue` — placed ahead of the existing `motor_configs[motor]` lookup, so a hold-capable bed never
  reaches the pulse-config path.
- **Schema bound reuse** (`services.py:1058-1061` area): `goto_preset`'s new `duration_ms` field reuses
  `MIN_TIMED_MOVE_DURATION_MS`/`MAX_TIMED_MOVE_DURATION_MS` rather than declaring its own bounds.
- **Schema bound omission** (`services.py:1153-1155` area): `send_intents`' `ATTR_TTL_MS` field has
  `vol.Range(min=0)` but no upper bound in the schema; the comment states the control's roster maximum
  (`ControlRoster.clamp_ttl_ms`) is the sole clamp authority.
- **Schema requirement** (`services.py:1143`): `ATTR_SAMPLES` requires `vol.Length(min=1)` — the schema
  itself refuses an empty sample list, ahead of any handler code.

## 5. Cross-references

| New/changed type | Referenced by |
|---|---|
| `ControlRoster` | `AdjustableBedCoordinator._control_roster` (held) / `.control_roster` (property); `AdjustableBedCoordinator.async_build_hold_pieces` (constructs it); `HoldReconstructor._roster` (held, via `__init__` and `use_roster`); `services.py`'s `_resolve_sampled_control`, `_resolve_sampled_action`, `_submit_hold`, `handle_send_intents` (all read `coordinator.control_roster`) |
| `load_control_declarations` | `AdjustableBedCoordinator.async_build_hold_pieces` (only caller) |
| `HoldCapable` | `AdjustableBedCoordinator._hold_connection_state_changed` (`isinstance` check); `services.handle_goto_preset`, `services.handle_timed_move` (`isinstance` checks); test doubles `_HoldingController`/`_RecordingController`/`_HoldCapableController` (inherit it) |
| `HoldReconstructor` | `AdjustableBedCoordinator._hold_reconstructor` (held) / `.hold_reconstructor` (property) / `.hold_diagnostics` (delegates to `.diagnostics`); constructed once, in `__init__`, with `target=self` (the coordinator satisfies the `HoldTarget` protocol via its own `connect_on_demand`); `services.handle_send_intents` (calls `.handle_samples`); `services._submit_hold` (calls `.submit`) |
| `HoldTarget` (protocol) | Implemented structurally by `AdjustableBedCoordinator` (which defines `connect_on_demand`); no other implementer in this diff |
| `IntentSampleSet` / `IntentSample` | Constructed in `services.handle_send_intents` / `services._resolve_sample`; consumed by `HoldReconstructor.handle_samples` |
| `Hold` / `Activate` | Constructed in `services._resolve_sampled_action`; consumed by `HoldReconstructor._apply`/`_refresh`/`_sample_ttl_s` |
| `HoldOutcome` | Produced by `HoldReconstructor._end`/`_resolve`/`submit`/`quiesce`; consumed by `services._submit_hold` (logs the outcome) and by every `TestDirectSubmissionDoors`/`TestHoldPieces` test that patches `submit` |
| `Control` | Constructed via `hold_roster.motor_control_name`/`preset_control_name` (as `str`, then wrapped by `ControlRoster.find`); held as dict keys throughout `HoldReconstructor` and in `HoldCapable.hold`'s `Mapping[Control, float]` argument |
| `hold_diagnostics` (coordinator property) | Not called anywhere in this diff. `custom_components/adjustable_bed/diagnostics.py:221` includes the sibling `coordinator.availability_diagnostics` in its output dict but was not touched by this diff, so `hold_diagnostics` has no wired caller yet. |

## 6. Test inventory

### tests/test_controller_contract.py (1 new test)

| Test | Slug |
|---|---|
| `test_hold_capability_is_declared_by_inheritance_alone` | none — docstring states the fact directly, no kebab-slug prefix |

### tests/test_coordinator.py — class `TestHoldPieces` (12 new tests)

| Test | Slug |
|---|---|
| `test_building_the_pieces_hands_the_roster_to_the_reconstructor` | none |
| `test_a_bed_type_correction_rebuilds_the_roster` | `roster-build-site` |
| `test_an_unchanged_bed_type_schedules_no_rebuild` | none |
| `test_the_stop_path_ends_holds_ahead_of_the_counter_and_the_lock` | `stop-fences-the-control` |
| `test_the_stop_path_ends_holds_with_the_link_down` | `stop-fences-the-control` |
| `test_a_hold_capable_link_attaches_and_its_end_fails_a_one_shot` | `one-shot-fails-at-detach` |
| `test_a_push_with_no_controller_starts_a_connect` | `connect-on-demand` |
| `test_the_idle_disconnect_waits_for_the_held_set` | `link-lifecycle-counts-holds` |
| `test_the_idle_timer_re_arms_when_the_held_set_empties` | `link-lifecycle-counts-holds` |
| `test_the_post_command_disconnect_waits_for_the_release` | `link-lifecycle-counts-holds` |
| `test_the_seek_loop_reads_the_held_set` | `link-lifecycle-counts-holds` |
| `test_shutdown_quiesces_before_the_disconnect` | none |

### tests/test_hold_reconstructor.py (47 new tests, new file)

| Test | Slug |
|---|---|
| `test_every_door_bounds_its_assertion` | `intents-are-time-bounded` |
| `test_an_activate_carries_no_ttl_and_reads_the_rosters` | `intents-are-parameterized` |
| `test_a_ttl_past_the_cap_clamps_at_intake` | `deadline-clamps-at-intake` |
| `test_a_vanished_client_holds_for_at_most_one_clamped_ttl` | `deadline-clamps-at-intake` |
| `test_an_activate_born_deadline_does_not_move` | `deadline-clamps-at-intake` |
| `test_a_shorter_ttl_moves_the_deadline_earlier` | `succession-replaces-deadline` |
| `test_a_refreshed_hold_ends_at_its_press_start_plus_the_cap` | `hold-lifetime-from-press-start` |
| `test_an_overlapping_intent_id_holds_across_the_changeover` | `hold-lifetime-from-press-start` |
| `test_a_stale_message_drops_whole` | `intent-intake-is-memoryless` |
| `test_a_newer_message_creates_refreshes_and_ends` | `intent-intake-is-memoryless` |
| `test_an_omitted_intent_lapses_rather_than_ending` | `intent-intake-is-memoryless` |
| `test_a_refresh_of_a_stopped_record_holds_nothing` | `intent-intake-is-memoryless` |
| `test_an_evicted_id_is_unknown_and_creates` | `intent-intake-is-memoryless` |
| `test_a_quiet_sender_is_forgotten_after_the_horizon` | `intent-intake-is-memoryless` |
| `test_the_later_press_governs_and_the_survivor_reverts` | `newest-contributor-governs` |
| `test_the_published_pair_is_one_contributors_own` | `newest-contributor-governs` |
| `test_a_release_ends_only_its_own_intent` | `end-is-not-stop` |
| `test_stop_takes_a_motors_two_controls_and_stop_all_takes_the_bed` | `end-is-not-stop` |
| `test_the_shrunken_set_reaches_the_controller_with_nothing_queued` | `stop-fences-the-control` |
| `test_an_in_flight_refresh_of_a_stopped_press_holds_nothing` | `stop-fences-the-control` |
| `test_a_new_intent_id_after_the_stop_asserts_normally` | `stop-fences-the-control` |
| `test_stop_all_fences_every_control` | `stop-fences-the-control` |
| `test_a_subscriber_receives_the_latest_set_at_once` | `intent-set-publication` |
| `test_every_change_publishes_one_complete_set` | `intent-set-publication` |
| `test_unregistering_stops_the_publications` | `intent-set-publication` |
| `test_a_control_no_controller_expressed_is_still_published` | `intent-set-publication` |
| `test_a_connect_pushes_the_one_shots_and_no_sample_borne_hold` | `one-shots-persist-holds-do-not` |
| `test_a_sample_borne_hold_reaches_the_controller_at_its_next_sample` | `one-shots-persist-holds-do-not` |
| `test_a_detach_fails_every_one_shot_at_once` | `one-shot-fails-at-detach` |
| `test_a_detach_retains_the_failed_records_and_leaves_holds_live` | `one-shot-fails-at-detach` |
| `test_the_next_connect_pushes_nothing_the_dead_link_expressed` | `one-shot-fails-at-detach` |
| `test_a_link_start_report_ends_nothing` | `one-shot-fails-at-detach` |
| `test_a_non_empty_set_with_no_controller_starts_the_connect` | `connect-on-demand` |
| `test_an_empty_set_summons_nothing` | `connect-on-demand` |
| `test_a_deadline_passing_before_the_connect_counts_and_does_not_raise` | `connect-on-demand` |
| `test_the_default_clock_is_the_event_loops_monotonic_time` | `deadlines-cross-every-tier` |
| `test_the_lapse_push_carries_the_shrunken_set` | `deadlines-cross-every-tier` |
| `test_the_pushed_deadline_is_the_effective_one` | `deadlines-cross-every-tier` |
| `test_a_submission_expressed_to_its_end_completes` | none |
| `test_a_stop_interrupts_a_live_submission` | none |
| `test_a_submission_never_expressed_fails` | none |
| `test_a_quiesce_interrupts_every_live_submission` | none |
| `test_quiesce_closes_intake_and_cancels_the_wake` | none |
| `test_a_stop_obsoletes_a_press_before_any_push` | `key-registration` |
| `test_each_press_is_expressed_once_in_the_order_it_was_held` | `press-state-fidelity` |
| `test_the_four_intent_side_loss_causes` | `press-state-fidelity` |
| `test_live_intents_keep_the_deadlines_the_old_roster_clamped` | none |

### tests/test_hold_roster.py (15 new tests, new file)

| Test | Slug |
|---|---|
| `test_controls_of_one_name_compare_equal` | none |
| `test_control_names_carry_motor_direction_and_preset_slot` | none |
| `test_a_control_declares_hold_activate_or_both` | `roster-declares-actions` |
| `test_a_preset_declares_no_activate_duration` | `roster-declares-actions` |
| `test_a_declaration_pairs_its_activate_duration_with_its_actions` | `roster-declares-actions` |
| `test_a_ttl_past_the_cap_clamps_and_one_under_it_does_not` | `roster-declares-actions` |
| `test_a_motors_activate_duration_is_the_declared_pulse` | `roster-declares-actions` |
| `test_no_bed_module_declares_a_control_yet` | `roster-declares-actions` |
| `test_a_roster_declaring_any_hold_is_hold_capable` | `roster-declares-actions` |
| `test_an_undeclared_name_resolves_to_nothing` | none |
| `test_reading_an_undeclared_control_raises` | none |
| `test_the_roster_lists_every_declared_control` | none |
| `test_a_staged_operation_is_marked` | `operation-controls-command-path-only` |
| `test_deliberate_only_is_a_second_mark_on_the_same_control` | `operation-controls-command-path-only` |
| `test_ping_holds_and_never_activates` | `ping` |

### tests/test_hold_service.py (15 new tests, new file)

| Test | Slug |
|---|---|
| `test_the_service_registers` | `send-intents-service` |
| `test_a_refused_sample_reaches_nothing` | `action-support-at-the-boundary` |
| `test_a_bed_declaring_no_hold_is_refused` | `action-support-at-the-boundary` |
| `test_an_idle_bed_answers_as_a_connected_one_does` | `action-support-at-the-boundary` |
| `test_one_earlier_refusal_stops_the_whole_set` | `action-support-at-the-boundary` |
| `test_a_valid_set_reaches_the_reconstructor_in_one_call` | `action-support-at-the-boundary` |
| `test_an_operation_control_is_refused_before_any_bit` | `operation-controls-command-path-only` |
| `test_an_empty_set_is_refused_at_the_schema` | `client-sample-sets` |
| `test_an_omitted_intent_lapses_rather_than_ending` | `client-sample-sets` |
| `test_a_release_rides_the_next_message_at_ttl_zero` | `client-sample-sets` |
| `test_every_accepted_sample_becomes_a_bounded_intent` | `intents-are-time-bounded` |
| `test_timed_move_submits_a_hold_on_the_motors_control` | `reuse-not-duplicate` |
| `test_goto_preset_submits_a_hold_on_the_slots_control` | `presets-hold-only` |
| `test_goto_preset_without_a_duration_holds_for_the_press_minimum` | `presets-hold-only` |
| `test_a_bed_that_is_not_hold_capable_keeps_its_baseline_paths` | none |

### tests/test_init.py — class `TestHoldPiecesLifecycle` (2 new tests)

| Test | Slug |
|---|---|
| `test_setup_builds_the_roster_before_the_first_connect` | `roster-build-site` |
| `test_unload_quiesces_the_reconstructor_and_then_closes_intake` | none |

### Total

92 new test functions across 6 files (3 new files, 3 changed files); 17 carry no kebab-slug in
their docstring (listed above as "none" — each still has a descriptive test name).
