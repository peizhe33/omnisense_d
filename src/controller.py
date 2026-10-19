"""Part C demonstration: a small state machine that consumes forecast.json.

Part C is the final decision-maker. Live occupancy always overrides a forecast.
Part D never activates equipment, and neither does this demo (it only writes a status file).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .config import (
    CONTROLLER_STATUS_PATH,
    FORECAST_PATH,
    LONG_VACANCY_THRESHOLD_MINUTES,
    TIMEZONE,
    ZONE_ID,
)
from .models import (
    FORECAST_UNAVAILABLE,
    ControllerDecision,
    ControlMode,
    Forecast,
    ForecastError,
    ForecastStatus,
    format_clock,
    write_json_atomic,
)

logger = logging.getLogger(__name__)


def load_forecast(path: Path) -> tuple[Forecast | None, str | None]:
    """Read forecast.json. Returns (forecast, None) or (None, warning). Never raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"{path.name} not found"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path.name} unreadable ({exc.__class__.__name__})"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, f"{path.name} is not valid JSON"
    try:
        return Forecast.from_dict(data), None
    except ForecastError as exc:
        return None, f"{path.name} unusable: {exc}"


def is_precool_window(forecast: Forecast | None, now: datetime) -> bool:
    """True only for an ACTIVE forecast with planned_restart <= now < expires_at."""
    if forecast is None or forecast.status is not ForecastStatus.ACTIVE:
        return False
    if forecast.planned_restart_time is None or forecast.expires_at is None:
        return False
    return forecast.planned_restart_time <= now < forecast.expires_at


def _clock_or_unknown(moment: datetime | None) -> str:
    return format_clock(moment) if moment is not None else "unknown time"


def _forecast_context(
    forecast: Forecast | None, effective: ForecastStatus | None, warning: str | None
) -> str:
    """Sentence explaining why a forecast is not driving the decision."""
    if forecast is None or effective is None:
        detail = warning or "forecast unavailable"
        return f"WARNING: forecast ignored ({detail}); using occupancy/vacancy logic only."
    if effective is ForecastStatus.ACTIVE:
        return (
            f"Forecast ACTIVE; planned restart at "
            f"{_clock_or_unknown(forecast.planned_restart_time)} not reached yet."
        )
    if effective is ForecastStatus.EXPIRED:
        return f"Forecast expired at {_clock_or_unknown(forecast.expires_at)} and was ignored."
    return f"Forecast status is {effective.value}; it was ignored."


def decide_mode(
    *,
    zone_id: str,
    now: datetime,
    occupancy_detected: bool | None,
    vacancy_started_at: datetime | None,
    forecast: Forecast | None,
    forecast_warning: str | None = None,
    long_vacancy_threshold_minutes: int = LONG_VACANCY_THRESHOLD_MINUTES,
) -> ControllerDecision:
    """Pure decision function (no I/O) implementing the Part C priority order."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("'now' must be timezone-aware")

    effective = forecast.effective_status(now) if forecast is not None else None
    forecast_status = effective.value if effective is not None else FORECAST_UNAVAILABLE

    def decision(mode: ControlMode, reason: str) -> ControllerDecision:
        return ControllerDecision(
            zone_id=zone_id,
            mode=mode,
            reason=reason,
            evaluated_at=now.astimezone(TIMEZONE),
            occupancy_detected=occupancy_detected,
            forecast_status=forecast_status,
        )

    # Occupancy sensing itself unavailable -> cannot trust any decision.
    if occupancy_detected is None:
        return decision(
            ControlMode.SAFE_FALLBACK,
            "Live occupancy input is unavailable; holding SAFE_FALLBACK and ignoring the forecast.",
        )

    # Priority 1: live occupancy overrides everything.
    if occupancy_detected:
        if effective is ForecastStatus.ACTIVE and forecast is not None:
            context = (
                f" Occupancy overrides the forecast (expected reuse "
                f"{_clock_or_unknown(forecast.next_use_time)}, planned restart "
                f"{_clock_or_unknown(forecast.planned_restart_time)})."
            )
        else:
            context = " " + _forecast_context(forecast, effective, forecast_warning)
        return decision(ControlMode.OCCUPIED, "Live occupancy detected." + context)

    # Priority 2: valid forecast inside its precool window (never past expiry).
    if forecast is not None and is_precool_window(forecast, now):
        return decision(
            ControlMode.PRECOOL,
            f"Valid forecast expects reuse at {_clock_or_unknown(forecast.next_use_time)}; "
            f"assumed cooling duration is {forecast.estimated_cooling_minutes} minutes.",
        )

    # Priorities 3 and 4: vacancy logic.
    if vacancy_started_at is None:
        return decision(
            ControlMode.SAFE_FALLBACK,
            "Vacancy start time is unknown; holding SAFE_FALLBACK. "
            + _forecast_context(forecast, effective, forecast_warning),
        )
    vacancy_minutes = max(0.0, (now - vacancy_started_at).total_seconds() / 60)
    mode = (
        ControlMode.LONG_VACANCY
        if vacancy_minutes >= long_vacancy_threshold_minutes
        else ControlMode.SHORT_VACANCY
    )
    reason = (
        f"Vacant for {int(vacancy_minutes)} min "
        f"(long-vacancy threshold {long_vacancy_threshold_minutes} min). "
        + _forecast_context(forecast, effective, forecast_warning)
    )
    return decision(mode, reason)


class ZoneController:
    """Stateful wrapper: reads forecast.json, decides, writes controller_status.json, logs changes."""

    def __init__(
        self,
        zone_id: str = ZONE_ID,
        forecast_path: Path = FORECAST_PATH,
        status_path: Path = CONTROLLER_STATUS_PATH,
        long_vacancy_threshold_minutes: int = LONG_VACANCY_THRESHOLD_MINUTES,
    ) -> None:
        self.zone_id = zone_id
        self.forecast_path = forecast_path
        self.status_path = status_path
        self.long_vacancy_threshold_minutes = long_vacancy_threshold_minutes
        self.last_mode: ControlMode | None = None
        self._last_warning: str | None = None

    def evaluate(
        self, now: datetime, occupancy_detected: bool | None, vacancy_started_at: datetime | None
    ) -> ControllerDecision:
        forecast, warning = load_forecast(self.forecast_path)
        if forecast is not None and forecast.zone_id != self.zone_id:
            warning = f"forecast is for zone {forecast.zone_id!r}, not {self.zone_id!r}"
            forecast = None
        self._log_warning_once(warning)

        decision = decide_mode(
            zone_id=self.zone_id,
            now=now,
            occupancy_detected=occupancy_detected,
            vacancy_started_at=vacancy_started_at,
            forecast=forecast,
            forecast_warning=warning,
            long_vacancy_threshold_minutes=self.long_vacancy_threshold_minutes,
        )
        try:
            write_json_atomic(self.status_path, decision.to_dict())
        except OSError as exc:
            logger.warning("Could not write %s (%s); will retry on the next evaluation",
                           self.status_path.name, exc.strerror)
        self._log_mode(decision)
        return decision

    def _log_warning_once(self, warning: str | None) -> None:
        if warning is not None and warning != self._last_warning:
            logger.warning("Forecast ignored: %s", warning)
        self._last_warning = warning

    def _log_mode(self, decision: ControllerDecision) -> None:
        clock = format_clock(decision.evaluated_at)
        if self.last_mode is None:
            logger.info("[%s] Initial mode: %s | %s", clock, decision.mode.value, decision.reason)
        elif decision.mode is not self.last_mode:
            logger.info(
                "[%s] Mode change: %s -> %s | %s",
                clock, self.last_mode.value, decision.mode.value, decision.reason,
            )
        self.last_mode = decision.mode