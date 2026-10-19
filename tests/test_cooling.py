from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.cooling import calculate_planned_restart_time, estimate_cooling

KL = ZoneInfo("Asia/Kuala_Lumpur")


def test_default_cooling_duration():
    estimate = estimate_cooling(27.0, 24.0, 10, 45)
    assert estimate.cooling_minutes == 30
    assert estimate.capped is False


def test_maximum_precooling_cap():
    estimate = estimate_cooling(35.0, 24.0, 10, 45)  # raw = 110 min
    assert estimate.raw_cooling_minutes == pytest.approx(110)
    assert estimate.cooling_minutes == 45
    assert estimate.capped is True


def test_no_cooling_when_already_below_target():
    estimate = estimate_cooling(23.0, 24.0, 10, 45)
    assert estimate.temperature_difference_c == 0
    assert estimate.cooling_minutes == 0


def test_planned_restart_is_next_use_minus_cooling():
    next_use = datetime(2026, 10, 20, 14, 0, tzinfo=KL)
    assert calculate_planned_restart_time(next_use, 30) == datetime(2026, 10, 20, 13, 30, tzinfo=KL)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes_per_degree_c": -1},
        {"current_temperature_c": 80.0},
        {"target_temperature_c": -5.0},
        {"current_temperature_c": float("nan")},
        {"maximum_precooling_minutes": -10},
    ],
)
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        estimate_cooling(**kwargs)