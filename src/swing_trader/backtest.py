from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from swing_trader.config import ExecutionConfig


@dataclass(frozen=True)
class BacktestResult:
    name: str
    returns: pd.Series
    gross_returns: pd.Series
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    transaction_costs: pd.Series


def run_backtest(
    name: str,
    adjusted_open: pd.DataFrame,
    target_weights: pd.DataFrame,
    execution: ExecutionConfig,
) -> BacktestResult:
    """Execute close-derived targets at the next session's adjusted open.

    Returns are open-to-next-open, so a target observed at close t becomes a
    position at open t+1 and first earns the interval open(t+1)->open(t+2).
    """
    prices = adjusted_open.sort_index().astype(float)
    target = target_weights.reindex(index=prices.index, columns=prices.columns).fillna(0.0)
    target = _apply_no_trade_band(target, execution.minimum_trade_weight)
    weights = target.shift(1).fillna(0.0)
    weights = weights.clip(lower=0.0)
    total = weights.sum(axis=1)
    weights.loc[total > 1.0] = weights.loc[total > 1.0].div(total.loc[total > 1.0], axis=0)
    interval_returns = prices.shift(-1).div(prices).sub(1.0)
    gross = (weights * interval_returns).sum(axis=1).iloc[:-1]
    weights = weights.iloc[:-1]
    turnover = weights.diff().abs().sum(axis=1).fillna(weights.abs().sum(axis=1))
    costs = turnover * execution.transaction_cost_bps / 10_000.0
    net = gross - costs
    equity = execution.initial_capital * (1.0 + net).cumprod()
    return BacktestResult(
        name=name,
        returns=net.rename(name),
        gross_returns=gross.rename(name),
        equity=equity.rename(name),
        weights=weights,
        turnover=turnover.rename(name),
        transaction_costs=costs.rename(name),
    )


def buy_and_hold_weights(prices: pd.DataFrame, ticker: str) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    weights.loc[prices[ticker].notna(), ticker] = 1.0
    return weights


def moving_average_weights(prices: pd.DataFrame, ticker: str, days: int = 200) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    weights.loc[prices[ticker] > prices[ticker].rolling(days).mean(), ticker] = 1.0
    return weights


def classic_dual_momentum_weights(
    prices: pd.DataFrame,
    tickers: tuple[str, ...],
    top_n: int = 3,
    lookback_days: int = 252,
) -> pd.DataFrame:
    momentum = prices[list(tickers)].div(prices[list(tickers)].shift(lookback_days)).sub(1.0)
    ranks = momentum.rank(axis=1, ascending=False, method="first")
    selected = (ranks <= top_n) & (momentum > 0.0)
    weights = selected.astype(float).div(selected.sum(axis=1).replace(0.0, 1.0), axis=0)
    scheduled = weights.loc[month_end_mask(prices.index)].reindex(prices.index).ffill().fillna(0.0)
    return scheduled.reindex(columns=prices.columns, fill_value=0.0)


def long_only_time_series_momentum_weights(
    prices: pd.DataFrame,
    tickers: tuple[str, ...],
    lookback_days: int = 252,
) -> pd.DataFrame:
    """Monthly equal weight across assets with positive own 12-month momentum.

    This is an unlevered, long-only comparator inspired by published time-series
    momentum, not a replication of the literature's long/short futures portfolios.
    """
    momentum = prices[list(tickers)].div(prices[list(tickers)].shift(lookback_days)).sub(1.0)
    selected = momentum > 0.0
    weights = selected.astype(float).div(selected.sum(axis=1).replace(0.0, 1.0), axis=0)
    scheduled = weights.loc[month_end_mask(prices.index)].reindex(prices.index).ffill().fillna(0.0)
    return scheduled.reindex(columns=prices.columns, fill_value=0.0)


def guarded_dual_momentum_weights(
    prices: pd.DataFrame,
    tickers: tuple[str, ...],
    *,
    benchmark: str,
    top_n: int = 3,
    individual_trend_days: int | None = None,
    market_trend_days: int | None = None,
) -> pd.DataFrame:
    """Monthly 12-month dual momentum with optional latched daily trend exits."""
    monthly = classic_dual_momentum_weights(prices, tickers, top_n)
    eligible = pd.DataFrame(True, index=prices.index, columns=prices.columns)
    if individual_trend_days is not None:
        eligible &= prices > prices.rolling(individual_trend_days).mean()
    risk_on = pd.Series(True, index=prices.index)
    if market_trend_days is not None:
        risk_on = prices[benchmark] > prices[benchmark].rolling(market_trend_days).mean()
    rebalance = pd.Series(month_end_mask(prices.index), index=prices.index)
    result = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    active = pd.Series(0.0, index=prices.columns)
    for date in prices.index:
        if bool(rebalance.loc[date]):
            active = monthly.loc[date].copy()
        active = active.where(eligible.loc[date], 0.0)
        if not bool(risk_on.loc[date]):
            active[:] = 0.0
        result.loc[date] = active
    return result


def panic_guarded_dual_momentum_weights(
    prices: pd.DataFrame,
    tickers: tuple[str, ...],
    *,
    benchmark: str,
    top_n: int = 3,
    volatility_days: int = 20,
    stress_volatility: float = 0.35,
    drawdown_days: int = 200,
    stress_drawdown: float = -0.12,
) -> pd.DataFrame:
    """Monthly dual momentum plus a daily panic exit that stays out until month-end."""
    monthly = classic_dual_momentum_weights(prices, tickers, top_n)
    benchmark_returns = prices[benchmark].pct_change(fill_method=None)
    volatility = benchmark_returns.rolling(volatility_days).std() * (252**0.5)
    drawdown = prices[benchmark].div(prices[benchmark].rolling(drawdown_days).max()).sub(1.0)
    panic = (volatility > stress_volatility) | (drawdown < stress_drawdown)
    rebalance = pd.Series(month_end_mask(prices.index), index=prices.index)
    result = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    active = pd.Series(0.0, index=prices.columns)
    for date in prices.index:
        if bool(rebalance.loc[date]):
            active = monthly.loc[date].copy()
        if bool(panic.loc[date]):
            active[:] = 0.0
        result.loc[date] = active
    return result


def _apply_no_trade_band(target: pd.DataFrame, threshold: float) -> pd.DataFrame:
    if threshold <= 0.0 or target.empty:
        return target
    result = pd.DataFrame(0.0, index=target.index, columns=target.columns)
    prior = pd.Series(0.0, index=target.columns)
    for date in target.index:
        proposed = target.loc[date].copy()
        small = proposed.sub(prior).abs() < threshold
        proposed.loc[small] = prior.loc[small]
        total = float(proposed.sum())
        if total > 1.0:
            proposed /= total
        result.loc[date] = proposed
        prior = proposed
    return result


def month_end_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """Identify completed-month final observations without treating a partial tail as month-end."""
    if not len(index):
        return np.array([], dtype=bool)
    periods = index.to_period("M")
    mask = np.r_[np.asarray(periods[:-1] != periods[1:]), True]
    if (index[-1] + pd.offsets.BDay(1)).to_period("M") == periods[-1]:
        mask[-1] = False
    return mask
