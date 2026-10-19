from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import DATA_PATH
from src.models import Forecast, ForecastQuality, ForecastStatus, OccupancyRecord
from src.predictor import (
    build_forecast,
    classify_quality,
    load_occupancy_records,
    median_minutes,
    minutes_to_datetime,
    parse_hhmm,
    predict_next_use,
    select_recent_records,
)

KL = ZoneInfo("Asia/Kuala_Lumpur")
FORECAST_DATE = date(2026, 10, 20)


def make_records(times: list[str], start: date = date(2026, 10, 5)) -> list[OccupancyRecord]:
    return [
        OccupancyRecord(start + timedelta(days=i), parse_hhmm(t))  # type: ignore[arg-type]
        for i, t in enumerate(times)
    ]


def test_parse_hhmm_valid_and_invalid():
    assert parse_hhmm("13:58") == 13 * 60 + 58
    for bad in ["", "25:00", "13:61", "abc", "1400", None]:
        assert parse_hhmm(bad) is None


def test_median_minutes_odd_and_even():
    assert median_minutes([838, 845, 835, 842, 840]) == 840
    assert median_minutes([840, 841]) == 841  # 840.5 rounds half up


def test_uses_latest_five_records_only():
    records = make_records(["13:00", "13:10", "14:00", "14:02", "13:58", "14:04", "14:00"])
    assert len(select_recent_records(records, FORECAST_DATE)) == 5
    result = predict_next_use(records, FORECAST_DATE)
    assert result.next_use_time == datetime(2026, 10, 20, 14, 0, tzinfo=KL)
    assert result.record_count == 5
    assert result.spread_minutes == 6
    assert result.forecast_quality is ForecastQuality.STRONG


@pytest.mark.parametrize(
    "count, spread, expected",
    [
        (5, 15, ForecastQuality.STRONG),
        (5, 16, ForecastQuality.MODERATE),
        (5, 30, ForecastQuality.MODERATE),
        (5, 31, ForecastQuality.WEAK),
        (2, 0, ForecastQuality.WEAK),
    ],
)
def test_quality_boundaries(count, spread, expected):
    assert classify_quality(count, spread) is expected


@pytest.mark.parametrize(
    "times, expected",
    [
        (["13:55", "14:05", "14:00"], ForecastQuality.STRONG),    # spread 10
        (["13:50", "14:10", "14:00"], ForecastQuality.MODERATE),  # spread 20
        (["13:30", "14:10", "14:00"], ForecastQuality.WEAK),      # spread 40
    ],
)
def test_quality_from_records(times, expected):
    result = predict_next_use(make_records(times), FORECAST_DATE)
    assert result.status is ForecastStatus.ACTIVE
    assert result.forecast_quality is expected


@pytest.mark.parametrize("times", [[], ["14:00"], ["14:00", "14:05"]])
def test_fewer_than_three_records_is_weak_and_insufficient(times):
    result = predict_next_use(make_records(times), FORECAST_DATE)
    assert result.status is ForecastStatus.INSUFFICIENT_DATA
    assert result.forecast_quality is ForecastQuality.WEAK
    assert result.next_use_time is None
    forecast = build_forecast(make_records(times), FORECAST_DATE, datetime(2026, 10, 20, 12, 0, tzinfo=KL))
    assert forecast.status is ForecastStatus.INSUFFICIENT_DATA
    assert forecast.to_dict()["next_use_time"] is None


def test_three_records_still_predict():
    result = predict_next_use(make_records(["13:58", "14:00", "14:02"]), FORECAST_DATE)
    assert result.status is ForecastStatus.ACTIVE
    assert result.record_count == 3


def test_csv_skips_missing_malformed_and_weekend_rows(tmp_path):
    csv_file = tmp_path / "h.csv"
    csv_file.write_text(
        "date,day_type,first_arrival,lunch_departure,lunch_return,final_departure\n"
        "2026-10-05,weekday,09:00,12:00,14:00,17:00\n"
        "2026-10-06,weekday,09:00,12:00,,17:00\n"        # missing
        "2026-10-07,weekday,09:00,12:00,25:99,17:00\n"   # malformed
        "2026-10-10,weekend,09:00,12:00,14:00,17:00\n"   # not a weekday
        "not-a-date,weekday,09:00,12:00,14:00,17:00\n"   # bad date
    )
    records = load_occupancy_records(csv_file)
    assert [r.record_date for r in records] == [date(2026, 10, 5)]


def test_csv_missing_empty_or_headerless_file_returns_no_records(tmp_path):
    assert load_occupancy_records(tmp_path / "nope.csv") == []
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert load_occupancy_records(empty) == []


def test_bundled_dataset_prediction_is_stable():
    result = predict_next_use(load_occupancy_records(DATA_PATH), FORECAST_DATE)
    assert result.next_use_time == datetime(2026, 10, 20, 14, 0, tzinfo=KL)
    assert result.record_count == 5
    assert result.spread_minutes == 9
    assert result.forecast_quality is ForecastQuality.STRONG


def test_timezone_aware_datetimes():
    result = predict_next_use(make_records(["14:00"] * 5), FORECAST_DATE)
    assert result.next_use_time.utcoffset() == timedelta(hours=8)
    forecast = build_forecast(make_records(["14:00"] * 5), FORECAST_DATE,
                              datetime(2026, 10, 20, 12, 10, tzinfo=KL))
    data = forecast.to_dict()
    assert data["next_use_time"] == "2026-10-20T14:00:00+08:00"
    assert data["issued_at"].endswith("+08:00")


def test_naive_issued_at_is_rejected():
    with pytest.raises(ValueError):
        build_forecast(make_records(["14:00"] * 5), FORECAST_DATE, datetime(2026, 10, 20, 12, 0))


def test_minutes_crossing_midnight_roll_to_next_day():
    assert minutes_to_datetime(FORECAST_DATE, 1500) == datetime(2026, 10, 21, 1, 0, tzinfo=KL)


def test_forecast_contract_values_and_round_trip():
    issued = datetime(2026, 10, 20, 12, 10, tzinfo=KL)
    forecast = build_forecast(make_records(["14:00"] * 5), FORECAST_DATE, issued)
    assert forecast.status is ForecastStatus.ACTIVE
    assert forecast.next_use_window_start == datetime(2026, 10, 20, 13, 50, tzinfo=KL)
    assert forecast.next_use_window_end == datetime(2026, 10, 20, 14, 10, tzinfo=KL)
    assert forecast.planned_restart_time == datetime(2026, 10, 20, 13, 30, tzinfo=KL)
    assert forecast.expires_at == datetime(2026, 10, 20, 14, 15, tzinfo=KL)
    assert forecast.estimated_cooling_minutes == 30
    assert forecast.to_dict()["cooling_model"] == "ASSUMED_COOLING_RESPONSE"
    assert Forecast.from_dict(forecast.to_dict()) == forecast


def test_forecast_issued_after_expiry_is_expired():
    forecast = build_forecast(make_records(["14:00"] * 5), FORECAST_DATE,
                              datetime(2026, 10, 20, 14, 30, tzinfo=KL))
    assert forecast.status is ForecastStatus.EXPIRED


def test_invalid_cooling_inputs_give_invalid_status():
    forecast = build_forecast(make_records(["14:00"] * 5), FORECAST_DATE,
                              datetime(2026, 10, 20, 12, 0, tzinfo=KL), minutes_per_degree_c=-1)
    assert forecast.status is ForecastStatus.INVALID
    assert forecast.next_use_time is None