from __future__ import annotations

import pandas as pd
import pytest

from swing_trader.sharadar import (
    build_sharadar_membership,
    build_sharadar_terminal_returns,
    normalize_sharadar_prices,
    write_sharadar_panel,
)


def test_sharadar_prices_use_one_consistent_total_return_adjustment() -> None:
    raw = pd.DataFrame(
        {
            "ticker": ["A", "A"],
            "date": ["2025-01-02", "2025-01-03"],
            "open": [100.0, 110.0],
            "high": [110.0, 120.0],
            "low": [90.0, 100.0],
            "close": [100.0, 110.0],
            "volume": [1_000.0, 2_000.0],
            "closeadj": [90.0, 110.0],
        }
    )

    panel = normalize_sharadar_prices(raw)

    assert panel.loc[pd.Timestamp("2025-01-02"), ("Open", "A")] == pytest.approx(90.0)
    assert panel.loc[pd.Timestamp("2025-01-02"), ("High", "A")] == pytest.approx(99.0)
    assert panel.loc[pd.Timestamp("2025-01-02"), ("Close", "A")] == pytest.approx(90.0)
    assert panel.loc[pd.Timestamp("2025-01-02"), ("Volume", "A")] == 1_000.0


def test_sharadar_membership_reconstructs_events_from_current_anchor() -> None:
    events = pd.DataFrame(
        {
            "date": [
                "2025-01-10",
                "2025-01-10",
                "2025-01-01",
                "2025-01-02",
                "2025-01-03",
                "2025-01-04",
            ],
            "action": ["current", "current", "historical", "added", "removed", "added"],
            "ticker": ["B", "C", "A", "B", "A", "C"],
        }
    )
    sessions = pd.bdate_range("2025-01-01", "2025-01-06")

    membership = build_sharadar_membership(events, sessions)

    assert membership.loc[pd.Timestamp("2025-01-01")].to_dict() == {
        "A": True,
        "B": False,
        "C": False,
    }
    assert membership.loc[pd.Timestamp("2025-01-02"), ["A", "B", "C"]].tolist() == [
        True,
        True,
        False,
    ]
    assert membership.loc[pd.Timestamp("2025-01-03"), ["A", "B", "C"]].tolist() == [
        False,
        True,
        False,
    ]
    assert membership.loc[pd.Timestamp("2025-01-06"), ["A", "B", "C"]].tolist() == [
        False,
        True,
        True,
    ]


def test_sharadar_historical_snapshot_is_an_independent_reconciliation_gate() -> None:
    events = pd.DataFrame(
        {
            "date": ["2025-01-10", "2025-01-01"],
            "action": ["current", "historical"],
            "ticker": ["A", "B"],
        }
    )

    with pytest.raises(ValueError, match="historical membership snapshot disagrees"):
        build_sharadar_membership(events, pd.bdate_range("2025-01-01", "2025-01-10"))


def test_sharadar_import_writes_hashed_normalized_bundle(tmp_path) -> None:
    stocks = pd.DataFrame(
        {
            "ticker": ["A", "A"],
            "date": ["2025-01-02", "2025-01-03"],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1_000.0, 1_100.0],
            "closeadj": [101.0, 102.0],
        }
    )
    sp500 = pd.DataFrame(
        {
            "date": ["2025-01-02", "2025-01-03"],
            "action": ["historical", "current"],
            "ticker": ["A", "A"],
        }
    )
    stocks_path = tmp_path / "stocks.csv"
    sp500_path = tmp_path / "sp500.csv"
    stocks.to_csv(stocks_path, index=False)
    sp500.to_csv(sp500_path, index=False)

    write_sharadar_panel(stocks_path, sp500_path, tmp_path / "normalized")

    manifest = (tmp_path / "normalized/manifest.json").read_text()
    assert '"provider": "Sharadar Prices"' in manifest
    assert (tmp_path / "normalized/ohlcv.parquet").exists()
    assert (tmp_path / "normalized/membership.parquet").exists()


def test_sharadar_terminal_returns_model_cash_and_bankruptcy_but_not_stock_deals() -> None:
    sessions = pd.bdate_range("2025-01-02", periods=3)
    prices = pd.DataFrame(
        {
            "date": [sessions[0], sessions[0], sessions[0]],
            "ticker": ["CASH", "ZERO", "STOCK"],
            "open": [50.0, 20.0, 30.0],
        }
    )
    actions = pd.DataFrame(
        {
            "date": [sessions[0], sessions[0], sessions[0]],
            "action": ["acquisitioncash", "bankruptcyliquidation", "acquisitionstock"],
            "ticker": ["CASH", "ZERO", "STOCK"],
            "value": [55.0, 1.0, 0.5],
            "contraticker": ["BUYER", "N/A", "BUYER"],
        }
    )

    result = build_sharadar_terminal_returns(actions, prices, sessions)

    assert result.overrides.loc[sessions[0], "CASH"] == pytest.approx(0.10)
    assert result.overrides.loc[sessions[0], "ZERO"] == -1.0
    assert pd.isna(result.overrides.loc[sessions[0], "STOCK"])
    assert result.unsupported_events["ticker"].tolist() == ["STOCK"]
