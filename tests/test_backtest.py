from __future__ import annotations

import pandas as pd
import pytest

from swing_trader.backtest import (
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
