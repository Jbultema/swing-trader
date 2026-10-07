from __future__ import annotations

import pandas as pd

from swing_trader.backtest import run_backtest
from swing_trader.config import ExecutionConfig
from swing_trader.execution import analyze_execution_capacity


def test_capacity_uses_only_prior_session_liquidity() -> None:
    dates = pd.bdate_range("2025-01-02", periods=4)
    opens = pd.DataFrame({"A": 100.0}, index=dates)
    close = pd.DataFrame({"A": 100.0}, index=dates)
    volume = pd.DataFrame({"A": [1_000.0, 1.0, 1.0, 1.0]}, index=dates)
    target = pd.DataFrame({"A": 1.0}, index=dates)
    execution = ExecutionConfig(
        initial_capital=100.0,
        transaction_cost_bps=0.0,
        minimum_trade_weight=0.0,
    )
    result = run_backtest("test", opens, target, execution)

    analysis = analyze_execution_capacity(
        result,
        close,
        volume,
        initial_capital=100.0,
        adv_sessions=1,
        maximum_adv_participation=0.01,
    )

    assert analysis.report.status == "passed"
    assert analysis.report.maximum_adv_participation == 0.001
    assert analysis.trades.iloc[0]["prior_median_dollar_volume"] == 100_000.0


def test_capacity_fails_when_trade_exceeds_adv_limit() -> None:
    dates = pd.bdate_range("2025-01-02", periods=4)
    prices = pd.DataFrame({"A": 100.0}, index=dates)
    volume = pd.DataFrame({"A": 1_000.0}, index=dates)
    target = pd.DataFrame({"A": 1.0}, index=dates)
    execution = ExecutionConfig(
        initial_capital=2_000.0,
        transaction_cost_bps=0.0,
        minimum_trade_weight=0.0,
    )
    result = run_backtest("test", prices, target, execution)

    analysis = analyze_execution_capacity(
        result,
        prices,
        volume,
        initial_capital=2_000.0,
        adv_sessions=1,
        maximum_adv_participation=0.01,
    )

    assert analysis.report.status == "failed"
    assert analysis.report.participation_violations == 1
    assert analysis.report.maximum_adv_participation == 0.02
