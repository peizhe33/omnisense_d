import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.controller import ZoneController, decide_mode, load_forecast
from src.models import ControlMode, Forecast, ForecastQuality, ForecastStatus, write_json_atomic
from src.simulation import SCENARIOS, run_scenario

KL = ZoneInfo("Asia/Kuala_Lumpur")
ZONE = "ZONE_01"


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 20, hour, minute, tzinfo=KL)


def make_forecast(status: ForecastStatus = ForecastStatus.ACTIVE) -> Forecast:
    return Forecast(
        zone_id=ZONE, status=status, issued_at=at(12, 40),
        forecast_quality=ForecastQuality.STRONG, record_count=5, spread_minutes=9,
        next_use_time=at(14, 0), next_use_window_start=at(13, 50), next_use_window_end=at(14, 10),
        planned_restart_time=at(13, 30), estimated_cooling_minutes=30, expires_at=at(14, 15),
    )


def decide(now, occupied, forecast, warning=None):
    return decide_mode(
        zone_id=ZONE, now=now, occupancy_detected=occupied,
        vacancy_started_at=at(12, 5), forecast=forecast, forecast_warning=warning,
    )


def test_occupied_overrides_precool():
    result = decide(at(13, 40), True, make_forecast())  # inside the precool window
    assert result.mode is ControlMode.OCCUPIED
    assert "overrides the forecast" in result.reason


def test_early_return_does_not_wait_for_planned_restart():
    assert decide(at(13, 20), True, make_forecast()).mode is ControlMode.OCCUPIED


def test_precool_starts_at_planned_restart():
    assert decide(at(13, 29), False, make_forecast()).mode is ControlMode.LONG_VACANCY
    result = decide(at(13, 30), False, make_forecast())
    assert result.mode is ControlMode.PRECOOL
    assert result.reason == ("Valid forecast expects reuse at 14:00; "
                             "assumed cooling duration is 30 minutes.")


def test_expired_forecast_does_not_precool():
    # Stored status is still ACTIVE, but `now` has reached expires_at.
    result = decide(at(14, 15), False, make_forecast())
    assert result.mode is ControlMode.LONG_VACANCY
    assert result.forecast_status == "EXPIRED"
    assert "expired" in result.reason


def test_cancelled_forecast_does_not_precool():
    result = decide(at(13, 40), False, make_forecast(ForecastStatus.CANCELLED))
    assert result.mode is not ControlMode.PRECOOL
    assert result.forecast_status == "CANCELLED"


def test_short_vs_long_vacancy():
    assert decide(at(12, 20), False, None).mode is ControlMode.SHORT_VACANCY  # 15 min
    assert decide(at(12, 35), False, None).mode is ControlMode.LONG_VACANCY   # 30 min = threshold


def test_missing_forecast_file_falls_back_safely(tmp_path):
    forecast, warning = load_forecast(tmp_path / "missing.json")
    assert forecast is None and "not found" in warning
    result = decide(at(13, 40), False, forecast, warning)
    assert result.mode is ControlMode.LONG_VACANCY
    assert "WARNING" in result.reason
    assert result.forecast_status == "UNAVAILABLE"


def test_malformed_forecast_file_falls_back_safely(tmp_path):
    path = tmp_path / "forecast.json"
    path.write_text('{"schema_version": "1.0", "status": "ACT')
    forecast, warning = load_forecast(path)
    assert forecast is None and "not valid JSON" in warning
    assert "WARNING" in decide(at(13, 40), False, forecast, warning).reason


def test_timezone_naive_forecast_is_rejected(tmp_path):
    data = make_forecast().to_dict()
    data["expires_at"] = "2026-10-20T14:15:00"  # no offset
    path = tmp_path / "forecast.json"
    path.write_text(json.dumps(data))
    forecast, warning = load_forecast(path)
    assert forecast is None and "timezone" in warning


def test_unavailable_occupancy_gives_safe_fallback():
    assert decide(at(13, 40), None, make_forecast()).mode is ControlMode.SAFE_FALLBACK


def test_timezone_aware_comparison_across_zones():
    now_utc = datetime(2026, 10, 20, 5, 30, tzinfo=timezone.utc)  # = 13:30 +08:00
    result = decide(now_utc, False, make_forecast())
    assert result.mode is ControlMode.PRECOOL
    assert result.evaluated_at.isoformat() == "2026-10-20T13:30:00+08:00"


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        decide(datetime(2026, 10, 20, 13, 30), False, make_forecast())


def test_zone_controller_writes_status_file(tmp_path):
    forecast_path = tmp_path / "forecast.json"
    status_path = tmp_path / "controller_status.json"
    write_json_atomic(forecast_path, make_forecast().to_dict())
    controller = ZoneController(forecast_path=forecast_path, status_path=status_path)
    controller.evaluate(at(13, 30), False, at(12, 5))
    status = json.loads(status_path.read_text())
    assert status["mode"] == "PRECOOL"
    assert status["forecast_status"] == "ACTIVE"
    assert status["occupancy_detected"] is False
    assert status["evaluated_at"] == "2026-10-20T13:30:00+08:00"


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_all_scenarios_pass_instantly(key, tmp_path):
    result = run_scenario(SCENARIOS[key], instant=True, output_dir=tmp_path)
    assert result.observed_modes == SCENARIOS[key].expected_modes
    assert (tmp_path / "controller_status.json").exists()


def test_cancelled_return_marks_forecast_expired_on_disk(tmp_path):
    run_scenario(SCENARIOS["cancelled-return"], instant=True, output_dir=tmp_path)
    assert json.loads((tmp_path / "forecast.json").read_text())["status"] == "EXPIRED"