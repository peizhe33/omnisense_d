"""Historical-median lunch-return predictor and forecast builder (Part D).

Part D only RECOMMENDS. It never activates equipment; Part C decides.
"""
from __future__ import annotations

import csv
import logging
import math
import re
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import (
    DEFAULT_CURRENT_TEMPERATURE_C,
    DEFAULT_MAXIMUM_PRECOOLING_MINUTES,
    DEFAULT_MINUTES_PER_DEGREE_C,
    DEFAULT_TARGET_TEMPERATURE_C,
    EXPIRY_AFTER_NEXT_USE_MINUTES,
    MIN_RECORDS_FOR_FORECAST,
    MODERATE_SPREAD_MAX_MINUTES,
    NEXT_USE_WINDOW_MINUTES,
    PREDICTION_METHOD,
    RECORDS_TO_USE,
    STRONG_SPREAD_MAX_MINUTES,
    TIMEZONE,
    ZONE_ID,
)
from .cooling import calculate_planned_restart_time, estimate_cooling
from .models import (
    Forecast,
    ForecastQuality,
    ForecastStatus,
    OccupancyRecord,
    PredictionResult,
    format_clock,
    format_minutes_of_day,
    format_stamp,
)

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = (
    "date", "day_type", "first_arrival", "lunch_departure", "lunch_return", "final_departure",
)
WEEKDAY_LABEL = "weekday"
_HHMM_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")


# --- CSV loading ------------------------------------------------------------------------
def parse_hhmm(value: str | None) -> int | None:
    """'HH:MM' -> minutes after midnight, or None if missing/malformed."""
    if not isinstance(value, str):
        return None
    match = _HHMM_PATTERN.match(value.strip())
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def _parse_row(row: dict[str, str | None], line_number: int) -> OccupancyRecord | None:
    day_type = (row.get("day_type") or "").strip().lower()
    if day_type != WEEKDAY_LABEL:
        logger.debug("CSV line %d: day_type %r is not a weekday; skipped", line_number, day_type)
        return None
    try:
        record_date = date.fromisoformat((row.get("date") or "").strip())
    except ValueError:
        logger.warning("Skipping CSV line %d: invalid date %r", line_number, row.get("date"))
        return None
    lunch_return = parse_hhmm(row.get("lunch_return"))
    if lunch_return is None:
        logger.warning(
            "Skipping %s (CSV line %d): lunch_return is missing or malformed (%r); record not used",
            record_date.isoformat(), line_number, row.get("lunch_return"),
        )
        return None
    return OccupancyRecord(record_date=record_date, lunch_return_minute=lunch_return)


def load_occupancy_records(csv_path: Path) -> list[OccupancyRecord]:
    """Load valid weekday records. Never raises for bad files; returns [] instead."""
    try:
        handle = csv_path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        logger.warning("Cannot read occupancy history %s (%s)", csv_path, exc.strerror)
        return []
    records: list[OccupancyRecord] = []
    with handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            logger.warning("Occupancy history is missing columns %s; no records loaded", missing)
            return []
        for line_number, row in enumerate(reader, start=2):  # line 1 is the header
            record = _parse_row(row, line_number)
            if record is not None:
                records.append(record)
    return records


# --- Prediction ---------------------------------------------------------------------------
def select_recent_records(
    records: list[OccupancyRecord], forecast_date: date, limit: int = RECORDS_TO_USE
) -> list[OccupancyRecord]:
    """Latest `limit` records dated strictly before the forecast date (oldest first)."""
    eligible = sorted(
        (r for r in records if r.record_date < forecast_date), key=lambda r: r.record_date
    )
    return eligible[-limit:]


def median_minutes(values: list[int]) -> int:
    """Median of minute-of-day values, rounded half up to a whole minute."""
    if not values:
        raise ValueError("cannot take the median of an empty list")
    return math.floor(statistics.median(values) + 0.5)


def calculate_spread(values: list[int]) -> int:
    return max(values) - min(values)


def classify_quality(record_count: int, spread_minutes: int | None) -> ForecastQuality:
    """Interpretable quality label (not a statistical probability)."""
    if record_count < MIN_RECORDS_FOR_FORECAST or spread_minutes is None:
        return ForecastQuality.WEAK
    if spread_minutes <= STRONG_SPREAD_MAX_MINUTES:
        return ForecastQuality.STRONG
    if spread_minutes <= MODERATE_SPREAD_MAX_MINUTES:
        return ForecastQuality.MODERATE
    return ForecastQuality.WEAK


def minutes_to_datetime(day: date, minutes: int) -> datetime:
    """Minutes after midnight on `day` -> aware datetime. Values >= 1440 roll into the next day."""
    midnight = datetime.combine(day, time.min, tzinfo=TIMEZONE)
    return midnight + timedelta(minutes=minutes)


def predict_next_use(records: list[OccupancyRecord], forecast_date: date) -> PredictionResult:
    """Predict the next lunch return as the median of the latest valid weekday records."""
    recent = select_recent_records(records, forecast_date)
    minutes = [r.lunch_return_minute for r in recent]
    dates_used = tuple(r.record_date for r in recent)
    record_count = len(recent)

    if record_count < MIN_RECORDS_FOR_FORECAST:
        message = (
            f"Only {record_count} valid record(s) before {forecast_date.isoformat()}; "
            f"at least {MIN_RECORDS_FOR_FORECAST} are needed, so no forecast was made."
        )
        logger.warning(message)
        return PredictionResult(
            status=ForecastStatus.INSUFFICIENT_DATA,
            forecast_quality=ForecastQuality.WEAK,
            record_count=record_count,
            spread_minutes=calculate_spread(minutes) if minutes else None,
            next_use_time=None,
            records_used=dates_used,
            message=message,
        )

    if record_count < RECORDS_TO_USE:
        logger.warning(
            "Only %d valid records available (preferred %d); using all of them",
            record_count, RECORDS_TO_USE,
        )
    spread = calculate_spread(minutes)
    quality = classify_quality(record_count, spread)
    next_use_time = minutes_to_datetime(forecast_date, median_minutes(minutes))

    logger.info(
        "Records used (%d most recent valid weekday records before %s): %s",
        record_count, forecast_date.isoformat(),
        ", ".join(
            f"{r.record_date.isoformat()}={format_minutes_of_day(r.lunch_return_minute)}"
            for r in recent
        ),
    )
    logger.info("Predicted next use: %s (%s of lunch-return times)",
                format_stamp(next_use_time), PREDICTION_METHOD)
    logger.info("Forecast quality: %s (records=%d, spread=%d min)",
                quality.value, record_count, spread)
    return PredictionResult(
        status=ForecastStatus.ACTIVE,
        forecast_quality=quality,
        record_count=record_count,
        spread_minutes=spread,
        next_use_time=next_use_time,
        records_used=dates_used,
        message="Prediction made from SYNTHETIC history.",
    )


# --- Forecast builder -----------------------------------------------------------------------
def build_forecast(
    records: list[OccupancyRecord],
    forecast_date: date,
    issued_at: datetime,
    current_temperature_c: float = DEFAULT_CURRENT_TEMPERATURE_C,
    target_temperature_c: float = DEFAULT_TARGET_TEMPERATURE_C,
    minutes_per_degree_c: float = DEFAULT_MINUTES_PER_DEGREE_C,
    maximum_precooling_minutes: float = DEFAULT_MAXIMUM_PRECOOLING_MINUTES,
    zone_id: str = ZONE_ID,
) -> Forecast:
    """Combine prediction + assumed cooling into a forecast. Never raises for bad data."""
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise ValueError("issued_at must be timezone-aware")

    prediction = predict_next_use(records, forecast_date)
    if prediction.next_use_time is None:
        return Forecast(
            zone_id=zone_id,
            status=ForecastStatus.INSUFFICIENT_DATA,
            issued_at=issued_at,
            forecast_quality=prediction.forecast_quality,
            record_count=prediction.record_count,
            spread_minutes=prediction.spread_minutes,
            message=prediction.message,
        )

    try:
        cooling = estimate_cooling(
            current_temperature_c, target_temperature_c,
            minutes_per_degree_c, maximum_precooling_minutes,
        )
    except ValueError as exc:
        logger.error("Cooling inputs rejected: %s", exc)
        return Forecast(
            zone_id=zone_id,
            status=ForecastStatus.INVALID,
            issued_at=issued_at,
            forecast_quality=prediction.forecast_quality,
            record_count=prediction.record_count,
            spread_minutes=prediction.spread_minutes,
            message=f"Invalid cooling inputs: {exc}",
        )

    next_use = prediction.next_use_time
    planned_restart = calculate_planned_restart_time(next_use, cooling.cooling_minutes)
    window = timedelta(minutes=NEXT_USE_WINDOW_MINUTES)
    expires_at = next_use + timedelta(minutes=EXPIRY_AFTER_NEXT_USE_MINUTES)
    status = ForecastStatus.ACTIVE if issued_at < expires_at else ForecastStatus.EXPIRED

    logger.info(
        "Assumed cooling (ASSUMED_COOLING_RESPONSE, not measured): "
        "%.1f C x %.1f min/C = %.1f min raw -> %d min used (cap %g min)",
        cooling.temperature_difference_c, float(minutes_per_degree_c),
        cooling.raw_cooling_minutes, cooling.cooling_minutes, maximum_precooling_minutes,
    )
    if cooling.capped:
        logger.warning("Required cooling exceeds the cap; restart planned with the capped duration")
    logger.info("Planned restart: %s (Part C decides whether to act on it)",
                format_stamp(planned_restart))
    logger.info("Forecast expires: %s", format_stamp(expires_at))
    if status is ForecastStatus.EXPIRED:
        logger.warning("Forecast was issued after its expiry time; status is EXPIRED")

    return Forecast(
        zone_id=zone_id,
        status=status,
        issued_at=issued_at,
        forecast_quality=prediction.forecast_quality,
        record_count=prediction.record_count,
        spread_minutes=prediction.spread_minutes,
        next_use_time=next_use,
        next_use_window_start=next_use - window,
        next_use_window_end=next_use + window,
        planned_restart_time=planned_restart,
        estimated_cooling_minutes=cooling.cooling_minutes,
        expires_at=expires_at,
        message=(
            f"Predicted reuse {format_clock(next_use)} from SYNTHETIC history; restart planned "
            f"{cooling.cooling_minutes} min earlier using the assumed cooling model."
        ),
    )