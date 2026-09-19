# Sleep Number BLE commands

Use `adjustable_bed.sleep_number_command` for additional Sleep Number controls and
queries. The command picker includes both Fuzion and older MCR commands; each bed
rejects commands or features it does not support. Parameters are a structured
object, never raw packets. Unknown or missing fields fail validation.

```yaml
action: adjustable_bed.sleep_number_command
data:
  device_id: YOUR_DEVICE_ID
  command: get_system_status
response_variable: bed_status
```

The optional response has `command` and `results` fields. `results` maps `single`,
`left`, `right`, or native `both` to the controller's parsed response. Setters may return an empty
object. Firmware errors and missing required responses fail the action.

For paired beds, the top-level `side` selects the physical target (`left`, `right`,
or `both`). Targeting a child device defaults to that child. Commands with a
`parameters.side` field require `left` or `right`; it must agree with a bound
physical side. To run such a command on both paired sides, make separate calls
with matching top-level and parameter sides. All selected sides are validated
before the requested commands execute. Calls use the integration's command lock
and paired cancellation rules. Status and configuration calls do not interrupt
motion; motion and stop calls can cancel a running movement.

## Fuzion commands

See the [SleepIQ discovery disposition](sleep-number-fuzion-disposition.md)
for the full Fuzion command, parameter, enum and response tables, including
temperature programs. `get_temperature_programs` joins temperature-program
info and settings by program ID into a complete program list. Hardware support is checked from the connected bed.

## Older MCR commands

These commands require the older MCR protocol. Every listed required parameter
must be supplied. Optional durations default to zero, except active foot warming defaults to 120 minutes. Omitted massage timer preserves its current value. Numeric fields
must be integers and booleans must be actual booleans.

| Command | Required parameters | Optional parameters |
|---|---|---|
| `mcr_status` | None | None |
| `foundation_status` | None | None |
| `position` | `axis`, `position`, `side` | None |
| `stop` | `side` | None |
| `preset_save` | `preset`, `side` | None |
| `preset_reset` | `preset`, `side` | None |
| `preset_timer` | `preset`, `side`, `timer` | None |
| `firmness_favorite` | `firmness`, `side` | None |
| `firmness_favorites` | None | None |
| `responsive_air` | `enabled`, `side` | None |
| `responsive_air_status` | None | None |
| `massage` | `side` | `foot`, `head`, `mode`, `timer` |
| `massage_status` | `side` | None |
| `foot_warming` | `level`, `side` | `duration` |
| `foot_warming_status` | `side` | None |
| `outlet` | `enabled`, `outlet` | `duration` |
| `outlet_status` | `outlet` | None |
| `light_intensity` | `intensity`, `side` | None |
| `underbed_auto` | `enabled` | None |
| `underbed_auto_status` | None | None |
| `pinch_status` | None | None |
| `sense_and_do` | `enabled` | None |
| `sense_and_do_status` | None | None |
| `kid_outlet` | `device` | `light_on`, `outlet_on` |
| `kid_outlet_status` | `device` | None |
| `head_tilt` | `enabled` | None |
| `software_versions` | None | None |

Parameter values:

- `side`: `left` or `right`; `axis`: `head` or `foot`.
- `position`: 0–100; `firmness`: 5–100 in steps of 5.
- `head`, `foot`, `mode`, `level`: 0–3.
- `duration`, `timer`: 0–32767. Massage and outlet timers use seconds.
  Foot-warming durations use minutes. Preset timers accept the raw protocol value,
  with zero cancelling the timer; the APK evidence does not establish a time unit.
  Massage timer 255 preserves the existing timer.
- `outlet`: 1–4 (right/left nightstand 1/2, right/left nightlight 3/4; outlet 3 is the under-bed light on models with that feature); `device`: 0–15.
- `intensity`: 1, 30, 45, 75 or 100.
- `enabled`, `outlet_on`, `light_on`: true or false.
- `preset`: `Favorite`, `Read`, `Watch TV`, `Flat`, `Zero G` or `Snore`,
  restricted to presets supported by the connected foundation.

`massage` needs at least one of its optional settings. `kid_outlet` needs
`outlet_on` or `light_on`. Foundation features, presets, massage, warming, the
head-tilt chamber and sense-and-do are checked before commands execute.

```yaml
action: adjustable_bed.sleep_number_command
data:
  device_id: YOUR_DEVICE_ID
  side: left
  command: position
  parameters:
    side: left
    axis: head
    position: 35
```

Protocol behavior comes from the SleepIQ 5.4.11 APK. Physical validation on each
bed model remains pending; successful static tests do not establish hardware
compatibility.
