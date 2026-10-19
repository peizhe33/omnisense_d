"""Command-line entry point.

Run from the project root:
    python -m src.main forecast --date 2026-10-20
    python -m src.main simulate --scenario normal --instant
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from .config import (
    DATA_PATH,
    DEFAULT_CURRENT_TEMPERATURE_C,
    DEFAULT_MAXIMUM_PRECOOLING_MINUTES,
    DEFAULT_MINUTES_PER_DEGREE_C,
    DEFAULT_TARGET_TEMPERATURE_C,
    FORECAST_PATH,
    OUTPUT_DIR,
    SIMULATION_DEFAULT_DATE,
    SIMULATION_SECONDS_PER_HOUR,
    SYNTHETIC_DATA_NOTICE,
    TIMEZONE,
)
from .models import ForecastStatus, write_json_atomic
from .predictor import build_forecast, load_occupancy_records
from .simulation import SCENARIOS, run_scenario

logger = logging.getLogger("omnisense")


def _issued_at(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO datetime: {text!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TIMEZONE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="OmniSense Part D")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    fc = sub.add_parser("forecast", help="generate output/forecast.json")
    fc.add_argument("--date", type=date.fromisoformat, default=None,
                    help="forecast date YYYY-MM-DD (default: today, Malaysia time)")
    fc.add_argument("--issued-at", type=_issued_at, default=None,
                    help="ISO issue time, e.g. 2026-10-20T12:10 (default: now)")
    fc.add_argument("--current-temperature", type=float, default=DEFAULT_CURRENT_TEMPERATURE_C)
    fc.add_argument("--target-temperature", type=float, default=DEFAULT_TARGET_TEMPERATURE_C)
    fc.add_argument("--minutes-per-degree", type=float, default=DEFAULT_MINUTES_PER_DEGREE_C)
    fc.add_argument("--max-precool-minutes", type=float, default=DEFAULT_MAXIMUM_PRECOOLING_MINUTES)
    fc.add_argument("--history", type=Path, default=DATA_PATH)
    fc.add_argument("--output", type=Path, default=FORECAST_PATH)

    sim = sub.add_parser("simulate", help="run a controller scenario")
    sim.add_argument("--scenario", choices=[*SCENARIOS, "all"], required=True)
    sim.add_argument("--instant", action="store_true", help="no sleeping (fast, for tests)")
    sim.add_argument("--seconds-per-hour", type=float, default=SIMULATION_SECONDS_PER_HOUR,
                     help="real seconds per simulated hour (default 10)")
    sim.add_argument("--date", type=date.fromisoformat, default=SIMULATION_DEFAULT_DATE)
    sim.add_argument("--history", type=Path, default=DATA_PATH)
    sim.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser


def run_forecast(args: argparse.Namespace) -> int:
    logger.info(SYNTHETIC_DATA_NOTICE)
    now = datetime.now(TIMEZONE).replace(microsecond=0)
    forecast_date = args.date or now.date()
    issued_at = args.issued_at or now

    records = load_occupancy_records(args.history)
    forecast = build_forecast(
        records, forecast_date, issued_at,
        current_temperature_c=args.current_temperature,
        target_temperature_c=args.target_temperature,
        minutes_per_degree_c=args.minutes_per_degree,
        maximum_precooling_minutes=args.max_precool_minutes,
    )
    write_json_atomic(args.output, forecast.to_dict())
    logger.info("Wrote %s", args.output)
    print(json.dumps(forecast.to_dict(), indent=2))
    ok = forecast.status in (ForecastStatus.ACTIVE, ForecastStatus.EXPIRED)
    return 0 if ok else 1


def run_simulate(args: argparse.Namespace) -> int:
    logger.info(SYNTHETIC_DATA_NOTICE)
    keys = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = [
        run_scenario(
            SCENARIOS[key],
            forecast_date=args.date,
            instant=args.instant,
            seconds_per_sim_hour=args.seconds_per_hour,
            output_dir=args.output_dir,
            history_path=args.history,
        )
        for key in keys
    ]
    all_passed = all(r.passed for r in results)
    if len(results) > 1:
        logger.info("%s", "All scenarios PASSED" if all_passed else "Some scenarios FAILED")
    return 0 if all_passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s | %(message)s",
        stream=sys.stdout,
    )
    return run_forecast(args) if args.command == "forecast" else run_simulate(args)


if __name__ == "__main__":
    raise SystemExit(main())