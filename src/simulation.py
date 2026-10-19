"""Scenario simulation: Part D issues a forecast, the Part C demo consumes it.

Time is simulated. By default one simulated hour takes ~10 real seconds; with
`instant=True` there is no sleeping at all (used by tests).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

from .config import (
    CONTROLLER_STATUS_FILENAME,
    DATA_PATH,
    FORECAST_FILENAME,
    OUTPUT_DIR,
    SIMULATED_LUNCH_DEPARTURE,
    SIMULATION_DEFAULT_DATE,
    SIMULATION_SECONDS_PER_HOUR,
    SIMULATION_STEP_MINUTES,
    TIMEZONE,
)
from .controller import ZoneController
from .models import ControlMode, Forecast, format_clock, write_json_atomic
from .predictor import build_forecast, load_occupancy_records

logger = logging.getLogger(__name__)

FORECAST_VALID = "valid"
FORECAST_MALFORMED = "malformed"
MALFORMED_FORECAST_TEXT = '{"schema_version": "1.0", "zone_id": "ZONE_01", "status": "ACT'


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    start: clock_time
    end: clock_time
    arrival: clock_time | None          # None = nobody returns
    forecast_state: str
    expected_modes: tuple[ControlMode, ...]


@dataclass(frozen=True)
class ScenarioResult:
    key: str
    observed_modes: tuple[ControlMode, ...]
    expected_modes: tuple[ControlMode, ...]

    @property
    def passed(self) -> bool:
        return self.observed_modes == self.expected_modes


SCENARIOS: dict[str, Scenario] = {
    "normal": Scenario(
        "normal", "Normal return", clock_time(12, 40), clock_time(14, 10), clock_time(14, 3),
        FORECAST_VALID,
        (ControlMode.LONG_VACANCY, ControlMode.PRECOOL, ControlMode.OCCUPIED),
    ),
    "early-return": Scenario(
        "early-return", "Unexpected early return", clock_time(12, 40), clock_time(13, 40),
        clock_time(13, 20), FORECAST_VALID,
        (ControlMode.LONG_VACANCY, ControlMode.OCCUPIED),
    ),
    "cancelled-return": Scenario(
        "cancelled-return", "Cancelled or late return", clock_time(12, 40), clock_time(14, 25),
        None, FORECAST_VALID,
        (ControlMode.LONG_VACANCY, ControlMode.PRECOOL, ControlMode.LONG_VACANCY),
    ),
    "invalid-forecast": Scenario(
        "invalid-forecast", "Invalid or missing forecast", clock_time(12, 40), clock_time(14, 10),
        clock_time(14, 3), FORECAST_MALFORMED,
        (ControlMode.LONG_VACANCY, ControlMode.OCCUPIED),
    ),
}


def _combine(day: date, at: clock_time) -> datetime:
    return datetime.combine(day, at, tzinfo=TIMEZONE)


def _issue_forecast(
    scenario: Scenario, forecast_path: Path, history_path: Path,
    forecast_date: date, issued_at: datetime,
) -> Forecast | None:
    """Play the role of Part D: write forecast.json (or a corrupt one on purpose)."""
    if scenario.forecast_state == FORECAST_MALFORMED:
        forecast_path.parent.mkdir(parents=True, exist_ok=True)
        forecast_path.write_text(MALFORMED_FORECAST_TEXT, encoding="utf-8")
        logger.info("Part D wrote a MALFORMED forecast.json on purpose (simulated failure)")
        return None
    records = load_occupancy_records(history_path)
    forecast = build_forecast(records, forecast_date, issued_at)
    write_json_atomic(forecast_path, forecast.to_dict())
    logger.info("Part D wrote %s (status %s, issued %s)",
                FORECAST_FILENAME, forecast.status.value, format_clock(issued_at))
    return forecast


def _publish_expiry_if_needed(
    forecast: Forecast | None, forecast_path: Path, now: datetime
) -> Forecast | None:
    """Part D side: rewrite forecast.json as EXPIRED once expires_at is reached."""
    if forecast is None:
        return None
    refreshed = forecast.refreshed(now)
    if refreshed is not forecast:
        write_json_atomic(forecast_path, refreshed.to_dict())
        logger.info(
            "[%s] Part D marked %s as %s (expiry reached; PRECOOL must stop)",
            format_clock(now), FORECAST_FILENAME, refreshed.status.value,
        )
    return refreshed


def run_scenario(
    scenario: Scenario,
    *,
    forecast_date: date = SIMULATION_DEFAULT_DATE,
    instant: bool = False,
    seconds_per_sim_hour: float = SIMULATION_SECONDS_PER_HOUR,
    output_dir: Path = OUTPUT_DIR,
    history_path: Path = DATA_PATH,
) -> ScenarioResult:
    forecast_path = output_dir / FORECAST_FILENAME
    start = _combine(forecast_date, scenario.start)
    end = _combine(forecast_date, scenario.end)
    arrival = _combine(forecast_date, scenario.arrival) if scenario.arrival else None
    left_at = _combine(forecast_date, SIMULATED_LUNCH_DEPARTURE)

    pace = ("instant mode, no sleeping" if instant
            else f"1 simulated hour = {seconds_per_sim_hour:g} real seconds")
    logger.info("===== Scenario '%s': %s =====", scenario.key, scenario.title)
    logger.info("Simulated time %s -> %s on %s (%s)",
                format_clock(start), format_clock(end), forecast_date.isoformat(), pace)

    forecast = _issue_forecast(scenario, forecast_path, history_path, forecast_date, start)
    if arrival is None:
        logger.info("Occupant left at %s and does not return", format_clock(left_at))
    else:
        logger.info("Occupant left at %s and returns at %s",
                    format_clock(left_at), format_clock(arrival))

    controller = ZoneController(
        forecast_path=forecast_path, status_path=output_dir / CONTROLLER_STATUS_FILENAME
    )
    step = timedelta(minutes=SIMULATION_STEP_MINUTES)
    sleep_seconds = 0.0 if instant else SIMULATION_STEP_MINUTES / 60 * seconds_per_sim_hour
    observed: list[ControlMode] = []

    now = start
    while now <= end:
        forecast = _publish_expiry_if_needed(forecast, forecast_path, now)
        occupied = arrival is not None and now >= arrival
        decision = controller.evaluate(now, occupied, left_at)
        if not observed or observed[-1] is not decision.mode:
            observed.append(decision.mode)
        logger.debug("[%s] %s (occupied=%s)", format_clock(now), decision.mode.value, occupied)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        now += step

    result = ScenarioResult(scenario.key, tuple(observed), scenario.expected_modes)
    logger.info(
        "Scenario '%s': observed %s | expected %s | %s",
        scenario.key,
        " -> ".join(m.value for m in result.observed_modes),
        " -> ".join(m.value for m in result.expected_modes),
        "PASS" if result.passed else "FAIL",
    )
    return result