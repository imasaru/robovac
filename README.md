# Eufy RobovVac control for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)
[![Sponsor me on Github Sponsors](https://img.shields.io/badge/Sponsor-ea4aaa?style=for-the-badge&logo=github-sponsors&logoColor=%23EA4AAA&labelColor=white)](https://github.com/sponsors/damacus)

A Eufy RoboVac integration for Home Assistant.

📖 **[Full Documentation](https://damacus.github.io/robovac)**

## ⚠️ Breaking Change in v2.0.0

**Version 2.0.0 requires you to remove and re-add the integration.** See the [upgrade guide](https://damacus.github.io/robovac) for details.

## Installation

See the [installation guide](https://damacus.github.io/robovac) for full instructions.

## Debugging

See the [troubleshooting guide](https://damacus.github.io/robovac) for debugging instructions.

## Model Validation

Validate if your RoboVac model is supported:

```bash
python -m custom_components.robovac.model_validator_cli --list
```

## Selected-room cleaning

Room cleaning is available through Home Assistant's `vacuum.clean_area` (segments) and the `roomClean` command of `vacuum.send_command`:

```yaml
action: vacuum.send_command
target:
  entity_id: vacuum.l35_hybrid
data:
  command: roomClean
  params:
    roomIds: [1, 3]   # required; ids may be ints or numeric strings
    count: 1          # cleanTimes, integer >= 1 (invalid values fall back to 1)
    mapId: 1          # optional
    send_start: true  # optional, overrides the model's room_clean_send_start
```

- **Room segments**: configure `id:name` pairs (e.g. `1:Kitchen,2:Living Room`) and the room segment map ID in the vacuum's options. Only configured ids are accepted, and the map ID is sent with each request.
- **Generic fallback rooms**: models that set `room_clean_fallback_room_count` (currently only the L35 Hybrid T2194, with 31 rooms) offer `Room 1` … `Room N` segments (ids 1–N) and support `clean_area` before any segments are configured. These requests use the same validation, and the map ID is sent when one is configured. Other models expose `clean_area` only after room segments are configured.
- **Safety**: when no valid room id is left after validation, nothing is sent to the vacuum (no room selection and no start command). For `send_command`, a validation error is raised. A missing `roomIds` no longer defaults to room 1.
- **Follow-up start command**: legacy JSON models write `selectRoomsClean` to DPS 124 and then, about 1 s later, send start on DPS 2. Models that start from DPS 124 alone and fall back to whole-house cleaning on the extra start ([#624](https://github.com/damacus/robovac/issues/624), reported on T2193) can set `room_clean_send_start = False` in their model file, or pass `send_start: false` for a single call. The default is still to send start on every model.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for contribution guidelines and development setup.
