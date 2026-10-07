from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from swing_trader.config import ExecutionConfig


class UnmodeledExecutionError(ValueError):
    """Raised when price/corporate-action data cannot support a claimed fill."""


@dataclass(frozen=True)
class BacktestResult:
    name: str
    returns: pd.Series
    gross_returns: pd.Series
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    transaction_costs: pd.Series
    trades: pd.DataFrame


def run_backtest(
    name: str,
    adjusted_open: pd.DataFrame,
    target_weights: pd.DataFrame,
    execution: ExecutionConfig,
    *,
    cash_returns: pd.Series | None = None,
    terminal_return_overrides: pd.DataFrame | None = None,
) -> BacktestResult:
    """Execute close-derived targets at the next session's adjusted open.

    Returns are open-to-next-open, so a target observed at close t becomes a
    position at open t+1 and first earns the interval open(t+1)->open(t+2).
    """
    prices = adjusted_open.sort_index().astype(float)
    target = target_weights.reindex(index=prices.index, columns=prices.columns).fillna(0.0)
    desired = target.shift(1).fillna(0.0).clip(lower=0.0)
    desired_total = desired.sum(axis=1)
    desired.loc[desired_total > 1.0] = desired.loc[desired_total > 1.0].div(
        desired_total.loc[desired_total > 1.0], axis=0
    )
    market_interval_returns = prices.shift(-1).div(prices).sub(1.0)
    terminal_liquidations = pd.DataFrame(False, index=prices.index, columns=prices.columns)
    interval_returns = market_interval_returns
    if terminal_return_overrides is not None:
        overrides = terminal_return_overrides.reindex(
            index=prices.index, columns=prices.columns
        ).astype(float)
        terminal_liquidations = market_interval_returns.isna() & overrides.notna()
        interval_returns = interval_returns.combine_first(overrides)
    cash = (
        pd.Series(0.0, index=prices.index)
        if cash_returns is None
        else cash_returns.reindex(prices.index).fillna(0.0).astype(float)
    )

    dates = prices.index[:-1]
    held = pd.Series(0.0, index=prices.columns)
    prior_desired = pd.Series(0.0, index=prices.columns)
    weight_rows: list[pd.Series] = []
    gross_rows: list[float] = []
    turnover_rows: list[float] = []
    trade_rows: list[pd.Series] = []
    for date in dates:
        proposed = desired.loc[date].copy()
        signal_changed = not proposed.equals(prior_desired)
        traded = 0.0
        trade = pd.Series(0.0, index=prices.columns)
        if signal_changed:
            small = proposed.sub(held).abs() < execution.minimum_trade_weight
            proposed.loc[small] = held.loc[small]
            proposed_total = float(proposed.sum())
            if proposed_total > 1.0:
                proposed /= proposed_total
            trade = proposed.sub(held)
            missing_trade_prices = trade.ne(0.0) & prices.loc[date].isna()
            if missing_trade_prices.any():
                missing = sorted(prices.columns[missing_trade_prices])
                raise UnmodeledExecutionError(
                    f"Cannot execute trades without an adjusted open on {date.date()}: {missing}"
                )
            traded = float(trade.abs().sum())
            held = proposed
        weight_rows.append(held.copy())
        trade_rows.append(trade)
        raw_asset_returns = interval_returns.loc[date]
        missing_held_returns = held.ne(0.0) & raw_asset_returns.isna()
        if missing_held_returns.any():
            missing = sorted(prices.columns[missing_held_returns])
            raise UnmodeledExecutionError(
                "Held securities lack a next-open return; explicit delisting or liquidation "
                f"treatment is required on {date.date()}: {missing}"
            )
        asset_returns = raw_asset_returns.fillna(0.0)
        cash_weight = max(0.0, 1.0 - float(held.sum()))
        gross_return = float((held * asset_returns).sum() + cash_weight * cash.loc[date])
        gross_rows.append(gross_return)
        turnover_rows.append(traded)

        gross_growth = 1.0 + gross_return
        if gross_growth <= 0.0:
            held[:] = 0.0
        else:
            held = held.mul(1.0 + asset_returns).div(gross_growth)
        prior_desired = desired.loc[date].copy()
        liquidated = terminal_liquidations.loc[date] & held.ne(0.0)
        if liquidated.any():
            held.loc[liquidated] = 0.0
            prior_desired.loc[liquidated] = 0.0

    weights = pd.DataFrame(weight_rows, index=dates, columns=prices.columns)
    gross = pd.Series(gross_rows, index=dates, name=name)
    turnover = pd.Series(turnover_rows, index=dates, name=name)
    trades = pd.DataFrame(trade_rows, index=dates, columns=prices.columns)
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
        trades=trades,
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


def month_end_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """Identify completed-month final observations without treating a partial tail as month-end."""
    if not len(index):
        return np.array([], dtype=bool)
    periods = index.to_period("M")
    mask = np.r_[np.asarray(periods[:-1] != periods[1:]), True]
    if (index[-1] + pd.offsets.BDay(1)).to_period("M") == periods[-1]:
        mask[-1] = False
    return mask
