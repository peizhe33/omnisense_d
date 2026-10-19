# OmniSense Part D — Forecasting and Precooling Recommendation

> **All occupancy history in this project is SYNTHETIC demo data.**
> Results are simulated. They are not measured energy savings, not measured
> sensing accuracy, and not measured properties of any real air-conditioning system.

## Purpose

Part D predicts when a room will next be used (the **lunch return**) and recommends
a **planned restart time** so the room can be precooled before people arrive.
It writes the recommendation to `output/forecast.json`. It includes a small
demonstration of Part C (the controller) that consumes that file.

## Architecture and responsibility boundaries

| Part | Responsibility | In this repo |
|------|----------------|--------------|
| C | Final HVAC decision (state machine) | `src/controller.py` (demo) |
| D | Predict next use and recommend a restart time | `src/predictor.py`, `src/cooling.py` |
| E | Display only | external dashboard |

Rules enforced by the design:

- Part D **never** activates HVAC equipment. It only writes a JSON recommendation.
- Part C is the **only** component that chooses a control mode.
- Part E **only displays** the two JSON files and never recalculates a decision.
- **Live occupancy always overrides a forecast.**

## Setup

```bash
cd omnisense_part_d
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt      # pytest (+ tzdata on Windows only)
```

Requires Python 3.11+. Runtime code uses only the standard library.

## Run commands

Run everything **from the `omnisense_part_d/` folder** using `-m`.

```bash
# 1. Generate a forecast (writes output/forecast.json)
python -m src.main forecast --date 2026-10-20 --current-temperature 27 --target-temperature 24

# Same, with a fixed issue time (deterministic output)
python -m src.main forecast --date 2026-10-20 --issued-at 2026-10-20T12:10

# 2. Simulations (instant = no sleeping)
python -m src.main simulate --scenario normal --instant
python -m src.main simulate --scenario early-return --instant
python -m src.main simulate --scenario cancelled-return --instant
python -m src.main simulate --scenario invalid-forecast --instant
python -m src.main simulate --scenario all --instant

# 3. Accelerated demo: 1 simulated hour = 10 real seconds (default pace)
python -m src.main simulate --scenario normal

# 4. Tests
pytest
```

Add `--verbose` before the subcommand for per-minute debug logs:
`python -m src.main --verbose simulate --scenario normal --instant`.

## Example `forecast.json`

```json
{
  "schema_version": "1.0",
  "zone_id": "ZONE_01",
  "source": "SYNTHETIC_HISTORY",
  "prediction_method": "HISTORICAL_MEDIAN",
  "next_use_time": "2026-10-20T14:00:00+08:00",
  "next_use_window_start": "2026-10-20T13:50:00+08:00",
  "next_use_window_end": "2026-10-20T14:10:00+08:00",
  "planned_restart_time": "2026-10-20T13:30:00+08:00",
  "estimated_cooling_minutes": 30,
  "cooling_model": "ASSUMED_COOLING_RESPONSE",
  "forecast_quality": "STRONG",
  "record_count": 5,
  "spread_minutes": 9,
  "issued_at": "2026-10-20T12:10:00+08:00",
  "expires_at": "2026-10-20T14:15:00+08:00",
  "status": "ACTIVE",
  "message": "Predicted reuse 14:00 from SYNTHETIC history; restart planned 30 min earlier using the assumed cooling model."
}
```

Statuses: `ACTIVE`, `EXPIRED`, `CANCELLED`, `INSUFFICIENT_DATA`, `INVALID`.
`INSUFFICIENT_DATA` and `INVALID` forecasts carry `null` times.

## Example `controller_status.json`

```json
{
  "zone_id": "ZONE_01",
  "mode": "PRECOOL",
  "reason": "Valid forecast expects reuse at 14:00; assumed cooling duration is 30 minutes.",
  "evaluated_at": "2026-10-20T13:30:00+08:00",
  "occupancy_detected": false,
  "forecast_status": "ACTIVE"
}
```

`forecast_status` is the *effective* status at evaluation time. It is `UNAVAILABLE`
when the forecast file is missing or unusable.

## Historical-median method

1. Keep CSV rows with `day_type = weekday`, a valid date before the forecast date,
   and a valid `lunch_return` (`HH:MM`). Rows with missing or malformed values are skipped with a warning.
2. Take the **latest five** such rows.
3. Convert each `HH:MM` to minutes after midnight and take the **median**
   (robust against one odd day). An even count rounds half up.
4. Convert back to a timezone-aware datetime in `Asia/Kuala_Lumpur` (via `zoneinfo`).

The usual-use window is the predicted time ±10 minutes.

## Forecast-quality categories

Spread = latest lunch-return minute − earliest lunch-return minute among the records used.

| Condition | Quality |
|-----------|---------|
| fewer than 3 valid records | `WEAK` (and status `INSUFFICIENT_DATA`, no prediction) |
| spread ≤ 15 min | `STRONG` |
| spread ≤ 30 min | `MODERATE` |
| spread > 30 min | `WEAK` |

This is a plain-language consistency label. It is **not** a statistical confidence probability.

## Assumed cooling model (`ASSUMED_COOLING_RESPONSE`)

```
temperature_difference = max(0, current_temperature_c - target_temperature_c)
raw_cooling_minutes    = temperature_difference * minutes_per_degree_c
cooling_minutes        = min(raw_cooling_minutes, maximum_precooling_minutes)
planned_restart_time   = next_use_time - cooling_minutes
```

Defaults: 27 °C now, 24 °C target, 10 min/°C, 45 min cap. This is an
**assumption chosen for transparency**, not a measured property of a real AC system.
Invalid inputs (negative minutes per degree, temperatures outside 10–45 °C, NaN) produce a forecast with status `INVALID`.

## Forecast expiry

`expires_at = next_use_time + 15 minutes`. After that moment the forecast is no
longer valid. PRECOOL must stop and the controller returns to vacancy logic. The
controller computes expiry from its own clock, so it is safe even if nobody
rewrites the file. In the simulation, Part D also rewrites `forecast.json` with
status `EXPIRED` so the dashboard sees it.

## Controller decision priority (Part C demo)

1. Live occupancy `true` → `OCCUPIED` (always wins, never waits for planned restart).
2. Forecast `ACTIVE` and `planned_restart_time <= now < expires_at` → `PRECOOL`.
3. Vacant for ≥ 30 min (configurable) → `LONG_VACANCY`.
4. Otherwise → `SHORT_VACANCY`.

Missing, corrupt, naive-timestamp, cancelled, expired, or wrong-zone forecasts are
**ignored** with a warning in `reason`. The controller never crashes on them.
`SAFE_FALLBACK` is used only when live occupancy input is unavailable or the vacancy start is unknown.

## Dashboard integration (Part E)

The dashboard should **only read** two files and never import controller code:

- `output/forecast.json`
- `output/controller_status.json`

It may display next expected use (`next_use_time`), planned precooling time
(`planned_restart_time`), forecast quality, source, current control mode, the
controller's `reason`, and forecast status and expiry.

It must **not** recompute modes. The controller's `mode` is the decision.

Both files are written atomically (temp file, then `os.replace`), so a poll never
sees a half-written file. Parse timestamps as ISO-8601 with offset. If a file is
missing or unparsable, show "waiting for data" and try again on the next poll.

## Limitations and honest claims

- History is synthetic. No real sensors, no real energy, no real comfort data.
- The cooling model is a linear assumption, not a thermal model. Real rooms vary with weather, insulation and equipment.
- The method only predicts lunch return, using only ~5 weekday samples, with no holiday or calendar awareness.
- Quality labels describe historical consistency only.
- The controller is a demonstration state machine, not a real BMS integration. No equipment is actuated.
- Possible extension: refuse to PRECOOL when quality is `WEAK`.
- Nothing here should be quoted as measured savings or measured accuracy.

## 90-second hackathon demo sequence

1. **(0:00–0:15)** Show `data/occupancy_history.csv` and say: "synthetic history, not real sensors."
2. **(0:15–0:30)** Run the `forecast` command. Point at `next_use_time`, `planned_restart_time`, `forecast_quality`, `ASSUMED_COOLING_RESPONSE`.
3. **(0:30–0:45)** `simulate --scenario early-return`. Occupancy flips the mode to `OCCUPIED` before the planned restart, because live evidence wins.
4. **(0:45–1:05)** `simulate --scenario normal`. Watch `LONG_VACANCY → PRECOOL → OCCUPIED`.
5. **(1:05–1:20)** `simulate --scenario cancelled-return --instant`. PRECOOL stops at expiry and the mode returns to `LONG_VACANCY`.
6. **(1:20–1:30)** `simulate --scenario invalid-forecast --instant`. A corrupt file produces a warning and a safe fallback, with no crash.
   Then run the `forecast` command again to restore a good file.