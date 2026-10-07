from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from swing_trader.data import (
    MarketDataError,
    _parse_alpha_vantage_monthly,
    download_alpha_vantage_monthly,
    load_cached_alpha_vantage_monthly,
    reconcile_monthly_adjusted,
    validate_prices,
)


def test_alpha_vantage_requests_are_paced(tmp_path: Path) -> None:
    payload = json.dumps(
        {"Monthly Adjusted Time Series": {"2026-09-30": {"5. adjusted close": "123.45"}}}
    ).encode()
    delays: list[float] = []

    frame = download_alpha_vantage_monthly(
        ("A", "B"),
        "test-key",
        tmp_path / "alpha.parquet",
        opener=lambda *_args, **_kwargs: io.BytesIO(payload),
        request_interval_seconds=1.1,
        sleeper=delays.append,
    )

    assert list(frame.columns) == ["A", "B"]
    assert delays == [1.1]


def test_recent_complete_alpha_vantage_snapshot_is_reused(tmp_path: Path) -> None:
    path = tmp_path / "alpha.parquet"
    pd.DataFrame({"A": [1.0], "B": [2.0]}, index=pd.DatetimeIndex(["2026-09-30"])).to_parquet(path)
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    cached = load_cached_alpha_vantage_monthly(
        path,
        ("A", "B"),
        now=modified_at + timedelta(hours=23),
    )
    stale = load_cached_alpha_vantage_monthly(
        path,
        ("A", "B"),
        now=modified_at + timedelta(hours=25),
    )

    assert cached is not None
    assert stale is None


def test_rejects_impossible_high_low() -> None:
    dates = pd.bdate_range("2024-01-01", periods=300)
    values = {}
    for field, value in {
        "Open": 10.0,
        "High": 9.0,
        "Low": 11.0,
        "Close": 10.0,
        "Volume": 100.0,
    }.items():
        values[(field, "A")] = value
    frame = pd.DataFrame(values, index=dates)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["field", "ticker"])
    with pytest.raises(MarketDataError, match="High below Low"):
        validate_prices(frame, ("A",))


def test_parses_alpha_vantage_adjusted_monthly_payload() -> None:
    series = _parse_alpha_vantage_monthly(
        {
            "Monthly Adjusted Time Series": {
                "2026-09-30": {"5. adjusted close": "123.45"},
                "2026-08-31": {"5. adjusted close": "120.00"},
            }
        },
        "A",
    )
    assert series.loc[pd.Timestamp("2026-09-30")] == 123.45
    assert series.index.is_monotonic_increasing


def test_monthly_reconciliation_passes_matching_independent_returns() -> None:
    primary, secondary = _reconciliation_frames()
    result = reconcile_monthly_adjusted(
        primary,
        secondary,
        ("A", "B"),
        months=12,
        return_tolerance=0.005,
        as_of=date(2026, 10, 1),
    )
    assert result["status"] == "passed"
    assert result["decision_data_gate_passed"] is True


def test_monthly_reconciliation_fails_material_disagreement() -> None:
    primary, secondary = _reconciliation_frames()
    secondary.loc[secondary.index[-1], "B"] *= 1.10
    result = reconcile_monthly_adjusted(
        primary,
        secondary,
        ("A", "B"),
        months=12,
        return_tolerance=0.005,
        as_of=date(2026, 10, 1),
    )
    assert result["status"] == "failed_reconciliation"
    assert result["decision_data_gate_passed"] is False


def test_monthly_reconciliation_fails_closed_without_secondary() -> None:
    primary, _ = _reconciliation_frames()
    result = reconcile_monthly_adjusted(
        primary,
        None,
        ("A", "B"),
        months=12,
        return_tolerance=0.005,
        as_of=date(2026, 10, 1),
        secondary_error="missing key",
    )
    assert result["decision_data_gate_passed"] is False
    assert result["secondary_error"] == "missing key"


def test_monthly_reconciliation_fails_stale_primary_snapshot() -> None:
    primary, secondary = _reconciliation_frames()
    result = reconcile_monthly_adjusted(
        primary,
        secondary,
        ("A", "B"),
        months=12,
        return_tolerance=0.005,
        max_primary_age_calendar_days=4,
        as_of=date(2026, 10, 10),
    )
    assert result["primary_fresh"] is False
    assert result["decision_data_gate_passed"] is False


def _reconciliation_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.date_range("2025-06-30", periods=16, freq="BME")
    primary_values = {
        ("Close", "A"): [100.0 + value for value in range(len(dates))],
        ("Close", "B"): [80.0 + value * 0.5 for value in range(len(dates))],
    }
    primary = pd.DataFrame(primary_values, index=dates)
    primary.columns = pd.MultiIndex.from_tuples(primary.columns, names=["field", "ticker"])
    secondary = primary["Close"].copy()
    return primary, secondary
