"""Shared data models and small JSON/format helpers for OmniSense Part D."""
from __future__ import annotations

import time
import json
import os
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .config import COOLING_MODEL, PREDICTION_METHOD, SCHEMA_VERSION, SOURCE, TIMEZONE

FORECAST_UNAVAILABLE = "UNAVAILABLE"  # controller label when no usable forecast exists


class ForecastError(ValueError):
    """Forecast data is missing fields, malformed, or otherwise unusable."""


class ForecastStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INVALID = "INVALID"


class ForecastQuality(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"


class ControlMode(str, Enum):
    OCCUPIED = "OCCUPIED"
    PRECOOL = "PRECOOL"
    SHORT_VACANCY = "SHORT_VACANCY"
    LONG_VACANCY = "LONG_VACANCY"
    SAFE_FALLBACK = "SAFE_FALLBACK"


# --- Formatting helpers (used in logs and reasons) ---------------------------------
def format_clock(moment: datetime) -> str:
    """'HH:MM' in Malaysia time."""
    return moment.astimezone(TIMEZONE).strftime("%H:%M")


def format_stamp(moment: datetime) -> str:
    """'YYYY-MM-DD HH:MM' in Malaysia time."""
    return moment.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")


def format_minutes_of_day(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --- Data classes -----------------------------------------------------------------------
@dataclass(frozen=True)
class OccupancyRecord:
    """One valid historical weekday with a usable lunch-return time."""

    record_date: date
    lunch_return_minute: int  # minutes after midnight


@dataclass(frozen=True)
class PredictionResult:
    """Output of the historical-median predictor (before cooling is applied)."""

    status: ForecastStatus  # ACTIVE = prediction made, INSUFFICIENT_DATA = none made
    forecast_quality: ForecastQuality
    record_count: int
    spread_minutes: int | None
    next_use_time: datetime | None
    records_used: tuple[date, ...]
    message: str


@dataclass(frozen=True)
class CoolingEstimate:
    """Result of the ASSUMED_COOLING_RESPONSE calculation."""

    temperature_difference_c: float
    raw_cooling_minutes: float
    cooling_minutes: int
    capped: bool


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _parse_aware_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ForecastError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ForecastError(f"{field_name} is not a valid ISO-8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForecastError(f"{field_name} has no timezone offset: {value!r}")
    return parsed


def _optional_datetime(data: dict[str, Any], key: str) -> datetime | None:
    value = data.get(key)
    return None if value is None else _parse_aware_datetime(value, key)


@dataclass(frozen=True)
class Forecast:
    """The forecast.json contract (schema 1.0)."""

    zone_id: str
    status: ForecastStatus
    issued_at: datetime
    forecast_quality: ForecastQuality
    record_count: int
    spread_minutes: int | None = None
    next_use_time: datetime | None = None
    next_use_window_start: datetime | None = None
    next_use_window_end: datetime | None = None
    planned_restart_time: datetime | None = None
    estimated_cooling_minutes: int | None = None
    expires_at: datetime | None = None
    source: str = SOURCE
    prediction_method: str = PREDICTION_METHOD
    cooling_model: str = COOLING_MODEL
    schema_version: str = SCHEMA_VERSION
    message: str | None = None

    def effective_status(self, now: datetime) -> ForecastStatus:
        """ACTIVE becomes EXPIRED once `now` reaches expires_at."""
        if (
            self.status is ForecastStatus.ACTIVE
            and self.expires_at is not None
            and now >= self.expires_at
        ):
            return ForecastStatus.EXPIRED
        return self.status

    def refreshed(self, now: datetime) -> Forecast:
        """Return a copy with status updated for expiry (or self if unchanged)."""
        effective = self.effective_status(now)
        return self if effective is self.status else replace(self, status=effective)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "zone_id": self.zone_id,
            "source": self.source,
            "prediction_method": self.prediction_method,
            "next_use_time": _iso(self.next_use_time),
            "next_use_window_start": _iso(self.next_use_window_start),
            "next_use_window_end": _iso(self.next_use_window_end),
            "planned_restart_time": _iso(self.planned_restart_time),
            "estimated_cooling_minutes": self.estimated_cooling_minutes,
            "cooling_model": self.cooling_model,
            "forecast_quality": self.forecast_quality.value,
            "record_count": self.record_count,
            "spread_minutes": self.spread_minutes,
            "issued_at": _iso(self.issued_at),
            "expires_at": _iso(self.expires_at),
            "status": self.status.value,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Forecast:
        """Parse and validate. Raises ForecastError if the data is unusable."""
        if not isinstance(data, dict):
            raise ForecastError("forecast JSON root must be an object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ForecastError(f"unsupported schema_version {data.get('schema_version')!r}")
        try:
            status = ForecastStatus(data["status"])
            quality = ForecastQuality(data["forecast_quality"])
            zone_id = str(data["zone_id"])
            record_count = int(data["record_count"])
            spread = None if data.get("spread_minutes") is None else int(data["spread_minutes"])
            cooling = (
                None
                if data.get("estimated_cooling_minutes") is None
                else int(data["estimated_cooling_minutes"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ForecastError(f"missing or invalid field ({exc})") from exc

        issued_at = _parse_aware_datetime(data.get("issued_at"), "issued_at")
        next_use = _optional_datetime(data, "next_use_time")
        window_start = _optional_datetime(data, "next_use_window_start")
        window_end = _optional_datetime(data, "next_use_window_end")
        restart = _optional_datetime(data, "planned_restart_time")
        expires_at = _optional_datetime(data, "expires_at")

        if status is ForecastStatus.ACTIVE:
            missing = [
                name
                for name, value in (
                    ("next_use_time", next_use),
                    ("planned_restart_time", restart),
                    ("expires_at", expires_at),
                )
                if value is None
            ]
            if missing:
                raise ForecastError(f"ACTIVE forecast is missing {', '.join(missing)}")
            if not (restart <= next_use <= expires_at):  # type: ignore[operator]
                raise ForecastError("ACTIVE forecast times are inconsistent")

        return cls(
            zone_id=zone_id,
            status=status,
            issued_at=issued_at,
            forecast_quality=quality,
            record_count=record_count,
            spread_minutes=spread,
            next_use_time=next_use,
            next_use_window_start=window_start,
            next_use_window_end=window_end,
            planned_restart_time=restart,
            estimated_cooling_minutes=cooling,
            expires_at=expires_at,
            source=str(data.get("source", SOURCE)),
            prediction_method=str(data.get("prediction_method", PREDICTION_METHOD)),
            cooling_model=str(data.get("cooling_model", COOLING_MODEL)),
            schema_version=SCHEMA_VERSION,
            message=data.get("message") if isinstance(data.get("message"), str) else None,
        )


@dataclass(frozen=True)
class ControllerDecision:
    """The controller_status.json contract."""

    zone_id: str
    mode: ControlMode
    reason: str
    evaluated_at: datetime
    occupancy_detected: bool | None
    forecast_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "mode": self.mode.value,
            "reason": self.reason,
            "evaluated_at": self.evaluated_at.isoformat(),
            "occupancy_detected": self.occupancy_detected,
            "forecast_status": self.forecast_status,
        }

REPLACE_RETRIES = 10
REPLACE_RETRY_DELAY_SECONDS = 0.05


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to a temp file, then atomically replace the target.

    On Windows another program (antivirus, OneDrive, an editor) can briefly lock
    the target file, so the replace is retried a few times before giving up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))
