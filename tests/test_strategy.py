from __future__ import annotations

import numpy as np
import pandas as pd

from swing_trader.config import StrategyConfig
from swing_trader.strategy import build_strategy


def _config() -> StrategyConfig:
    return StrategyConfig(
        top_n=2,
        rank_buffer=1,
        fast_days=21,
        medium_days=63,
        slow_days=126,
        skip_days=5,
        trend_days=200,
        fast_trend_days=50,
        volatility_days=20,
        atr_days=20,
        trailing_high_days=63,
        atr_multiple=3.0,
        target_volatility=0.12,
        max_asset_weight=0.6,
        stress_volatility=0.35,
        stress_drawdown=-0.12,
        risk_off_exposure=0.0,
        rebalance_weekday=4,
    )


def _prices() -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=360)
    fields = {}
    for idx, ticker in enumerate(("SPY", "A", "B")):
        close = pd.Series(100 * np.exp(np.linspace(0, 0.30 - idx * 0.05, len(dates))), index=dates)
        fields[("Close", ticker)] = close
        fields[("Open", ticker)] = close * 0.999
        fields[("High", ticker)] = close * 1.01
        fields[("Low", ticker)] = close * 0.99
        fields[("Volume", ticker)] = pd.Series(1_000_000, index=dates)
    frame = pd.DataFrame(fields)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["field", "ticker"])
    return frame


def test_weights_are_long_only_unlevered_and_capped() -> None:
    run = build_strategy(_prices(), _config(), "SPY")
    assert run.target_weights.min().min() >= 0.0
    assert run.target_weights.sum(axis=1).max() <= 1.0 + 1e-12
    assert run.target_weights.max().max() <= 0.6 + 1e-12


def test_future_price_change_does_not_change_prior_signals() -> None:
    prices = _prices()
    original = build_strategy(prices, _config(), "SPY").target_weights
    cutoff = prices.index[-20]
    changed = prices.copy()
    changed.loc[cutoff:, ("Close", "A")] *= 4.0
    revised = build_strategy(changed, _config(), "SPY").target_weights
    pd.testing.assert_frame_equal(original.loc[original.index < cutoff], revised.loc[revised.index < cutoff])


def test_market_kill_switch_moves_to_cash() -> None:
    prices = _prices()
    prices.loc[prices.index[-30]:, ("Close", "SPY")] *= np.linspace(1.0, 0.5, 30)
    run = build_strategy(prices, _config(), "SPY")
    assert not bool(run.market_state.iloc[-1]["risk_on"])
    assert run.target_weights.iloc[-1].sum() == 0.0


def test_ordinary_position_sizes_only_change_on_rebalance_day() -> None:
    run = build_strategy(_prices(), _config(), "SPY")
    changes = run.target_weights.diff().abs().sum(axis=1)
    changed_weekdays = set(changes.loc[changes > 1e-12].index.weekday)
    # Daily protective exits can occur on any day; this smooth synthetic series has none.
    assert changed_weekdays <= {4}
