from __future__ import annotations

import pandas as pd
import pytest

from swing_trader.backtest import (
    long_only_time_series_momentum_weights,
    month_end_mask,
    panic_guarded_dual_momentum_weights,
    run_backtest,
)
from swing_trader.config import ExecutionConfig


def test_close_signal_executes_at_next_open() -> None:
    dates = pd.bdate_range("2025-01-01", periods=5)
    opens = pd.DataFrame({"A": [100.0, 100.0, 110.0, 121.0, 121.0]}, index=dates)
    target = pd.DataFrame({"A": [1.0, 1.0, 0.0, 0.0, 0.0]}, index=dates)
    result = run_backtest(
        "test",
        opens,
        target,
        ExecutionConfig(initial_capital=100.0, transaction_cost_bps=0.0, minimum_trade_weight=0.0),
    )
    assert result.weights.loc[dates[0], "A"] == 0.0
    assert result.weights.loc[dates[1], "A"] == 1.0
    assert result.returns.loc[dates[1]] == pytest.approx(0.10)


def test_costs_apply_to_buys_and_sells() -> None:
    dates = pd.bdate_range("2025-01-01", periods=4)
    opens = pd.DataFrame({"A": 100.0}, index=dates)
    target = pd.DataFrame({"A": [1.0, 0.0, 0.0, 0.0]}, index=dates)
    result = run_backtest(
        "test",
        opens,
        target,
        ExecutionConfig(initial_capital=100.0, transaction_cost_bps=10.0, minimum_trade_weight=0.0),
    )
    assert result.transaction_costs.sum() == 0.002


def test_holdings_drift_without_free_daily_rebalancing() -> None:
    dates = pd.bdate_range("2025-01-01", periods=4)
    opens = pd.DataFrame(
        {"A": [100.0, 100.0, 200.0, 200.0], "B": [100.0, 100.0, 100.0, 100.0]},
        index=dates,
    )
    target = pd.DataFrame({"A": 0.5, "B": 0.5}, index=dates)
    result = run_backtest(
        "test",
        opens,
        target,
        ExecutionConfig(initial_capital=100.0, transaction_cost_bps=0.0, minimum_trade_weight=0.0),
    )

    assert result.weights.loc[dates[2], "A"] == pytest.approx(2.0 / 3.0)
    assert result.weights.loc[dates[2], "B"] == pytest.approx(1.0 / 3.0)
    assert result.turnover.sum() == pytest.approx(1.0)


def test_unallocated_capital_earns_cash_return() -> None:
    dates = pd.bdate_range("2025-01-01", periods=3)
    opens = pd.DataFrame({"A": 100.0}, index=dates)
    target = pd.DataFrame({"A": 0.0}, index=dates)
    cash_returns = pd.Series([0.001, 0.002, 0.0], index=dates)
    result = run_backtest(
        "test",
        opens,
        target,
        ExecutionConfig(initial_capital=100.0, transaction_cost_bps=0.0, minimum_trade_weight=0.0),
        cash_returns=cash_returns,
    )

    assert result.returns.tolist() == pytest.approx([0.001, 0.002])


def test_partial_final_month_is_not_treated_as_month_end() -> None:
    dates = pd.DatetimeIndex(["2026-08-31", "2026-09-30", "2026-10-01", "2026-10-06"])
    assert month_end_mask(dates).tolist() == [True, True, False, False]


def test_panic_guard_exits_to_cash_after_market_crash() -> None:
    dates = pd.bdate_range("2024-01-02", periods=320)
    steady = pd.Series([100.0 + 0.1 * value for value in range(len(dates))], index=dates)
    prices = pd.DataFrame({"SPY": steady, "A": steady * 1.01}, index=dates)
    prices.loc[dates[-20] :, "SPY"] *= pd.Series(
        [1.0 - 0.02 * value for value in range(20)], index=dates[-20:]
    )
    weights = panic_guarded_dual_momentum_weights(
        prices,
        ("SPY", "A"),
        benchmark="SPY",
        top_n=1,
    )
    assert weights.iloc[-1].sum() == 0.0


def test_long_only_time_series_momentum_holds_only_positive_assets() -> None:
    dates = pd.bdate_range("2024-01-02", periods=400)
    prices = pd.DataFrame(
        {
            "A": [100.0 + value for value in range(len(dates))],
            "B": [500.0 - value for value in range(len(dates))],
        },
        index=dates,
    )
    weights = long_only_time_series_momentum_weights(prices, ("A", "B"))

    assert weights.iloc[-1]["A"] == 1.0
    assert weights.iloc[-1]["B"] == 0.0
