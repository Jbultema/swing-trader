from __future__ import annotations

import pandas as pd
import pytest

from swing_trader.backtest import month_end_mask, run_backtest
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
