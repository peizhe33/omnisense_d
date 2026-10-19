"""Cooling-duration estimate for Part D.

IMPORTANT: this is an ASSUMED_COOLING_RESPONSE, a transparent linear assumption
(minutes per degree C). It is NOT a measured property of a real AC system.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from .config import (
    DEFAULT_CURRENT_TEMPERATURE_C,
    DEFAULT_MAXIMUM_PRECOOLING_MINUTES,
    DEFAULT_MINUTES_PER_DEGREE_C,
    DEFAULT_TARGET_TEMPERATURE_C,
    MAX_MINUTES_PER_DEGREE_C,
    MAX_PLAUSIBLE_TEMPERATURE_C,
    MAX_PRECOOLING_LIMIT_MINUTES,
    MIN_PLAUSIBLE_TEMPERATURE_C,
)
from .models import CoolingEstimate


def _check_number(value: float, name: str, low: float, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")


def validate_cooling_inputs(
    current_temperature_c: float,
    target_temperature_c: float,
    minutes_per_degree_c: float,
    maximum_precooling_minutes: float,
) -> None:
    """Raise ValueError for negative, non-finite or unrealistic inputs."""
    _check_number(current_temperature_c, "current_temperature_c",
                  MIN_PLAUSIBLE_TEMPERATURE_C, MAX_PLAUSIBLE_TEMPERATURE_C)
    _check_number(target_temperature_c, "target_temperature_c",
                  MIN_PLAUSIBLE_TEMPERATURE_C, MAX_PLAUSIBLE_TEMPERATURE_C)
    _check_number(minutes_per_degree_c, "minutes_per_degree_c", 0, MAX_MINUTES_PER_DEGREE_C)
    _check_number(maximum_precooling_minutes, "maximum_precooling_minutes",
                  0, MAX_PRECOOLING_LIMIT_MINUTES)


def estimate_cooling(
    current_temperature_c: float = DEFAULT_CURRENT_TEMPERATURE_C,
    target_temperature_c: float = DEFAULT_TARGET_TEMPERATURE_C,
    minutes_per_degree_c: float = DEFAULT_MINUTES_PER_DEGREE_C,
    maximum_precooling_minutes: float = DEFAULT_MAXIMUM_PRECOOLING_MINUTES,
) -> CoolingEstimate:
    """ASSUMED_COOLING_RESPONSE: cooling minutes = degrees to cool x minutes/degree, capped."""
    validate_cooling_inputs(
        current_temperature_c, target_temperature_c,
        minutes_per_degree_c, maximum_precooling_minutes,
    )
    temperature_difference = max(0.0, current_temperature_c - target_temperature_c)
    raw_cooling_minutes = temperature_difference * minutes_per_degree_c
    cooling_minutes = min(raw_cooling_minutes, maximum_precooling_minutes)
    return CoolingEstimate(
        temperature_difference_c=temperature_difference,
        raw_cooling_minutes=raw_cooling_minutes,
        cooling_minutes=int(round(cooling_minutes)),  # whole minutes for the JSON contract
        capped=raw_cooling_minutes > maximum_precooling_minutes,
    )


def calculate_planned_restart_time(next_use_time: datetime, cooling_minutes: int) -> datetime:
    """planned_restart_time = predicted next use minus the assumed cooling duration."""
    return next_use_time - timedelta(minutes=cooling_minutes)