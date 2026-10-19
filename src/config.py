"""Central configuration for OmniSense Part D. No magic numbers elsewhere."""
from __future__ import annotations

from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Paths -------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = PROJECT_ROOT / "data" / "occupancy_history.csv"
OUTPUT_DIR = PROJECT_ROOT / "output"
FORECAST_FILENAME = "forecast.json"
CONTROLLER_STATUS_FILENAME = "controller_status.json"
FORECAST_PATH = OUTPUT_DIR / FORECAST_FILENAME
CONTROLLER_STATUS_PATH = OUTPUT_DIR / CONTROLLER_STATUS_FILENAME

# --- Time and identity ---------------------------------------------------------
TIMEZONE_NAME = "Asia/Kuala_Lumpur"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
ZONE_ID = "ZONE_01"

# --- JSON contract labels --------------------------------------------------------
SCHEMA_VERSION = "1.0"
SOURCE = "SYNTHETIC_HISTORY"
PREDICTION_METHOD = "HISTORICAL_MEDIAN"
COOLING_MODEL = "ASSUMED_COOLING_RESPONSE"
SYNTHETIC_DATA_NOTICE = (
    "NOTE: occupancy history is SYNTHETIC demo data. Results are simulated; "
    "they are not measured energy savings or sensing accuracy."
)

# --- Prediction ---------------------------------------------------------------------
RECORDS_TO_USE = 5                   # latest valid matching weekday records
MIN_RECORDS_FOR_FORECAST = 3         # below this: WEAK + INSUFFICIENT_DATA
STRONG_SPREAD_MAX_MINUTES = 15
MODERATE_SPREAD_MAX_MINUTES = 30
NEXT_USE_WINDOW_MINUTES = 10         # +/- around predicted time
EXPIRY_AFTER_NEXT_USE_MINUTES = 15

# --- Assumed cooling model ------------------------------------------------------------
DEFAULT_CURRENT_TEMPERATURE_C = 27.0
DEFAULT_TARGET_TEMPERATURE_C = 24.0
DEFAULT_MINUTES_PER_DEGREE_C = 10
DEFAULT_MAXIMUM_PRECOOLING_MINUTES = 45
MIN_PLAUSIBLE_TEMPERATURE_C = 10.0
MAX_PLAUSIBLE_TEMPERATURE_C = 45.0
MAX_MINUTES_PER_DEGREE_C = 60.0
MAX_PRECOOLING_LIMIT_MINUTES = 180

# --- Controller (Part C demo) ----------------------------------------------------------
LONG_VACANCY_THRESHOLD_MINUTES = 30

# --- Simulation ------------------------------------------------------------------------
SIMULATION_DEFAULT_DATE = date(2026, 10, 20)
SIMULATION_SECONDS_PER_HOUR = 10.0   # accelerated: 1 simulated hour = 10 real seconds
SIMULATION_STEP_MINUTES = 1
SIMULATED_LUNCH_DEPARTURE = time(12, 5)