from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from swing_trader.config import StrategyConfig

TRADING_DAYS = 252


@dataclass(frozen=True)
class StrategyRun:
    target_weights: pd.DataFrame
    scores: pd.DataFrame
    evidence: pd.DataFrame
    market_state: pd.DataFrame
    exit_reasons: pd.DataFrame


def build_strategy(ohlcv: pd.DataFrame, config: StrategyConfig, benchmark: str) -> StrategyRun:
    close = ohlcv["Close"].astype(float)
    high = ohlcv["High"].astype(float)
    low = ohlcv["Low"].astype(float)
    returns = close.pct_change(fill_method=None)
    volatility = returns.rolling(config.volatility_days).std() * np.sqrt(TRADING_DAYS)

    momentum_fast = _skip_return(close, config.fast_days, config.skip_days)
    momentum_medium = _skip_return(close, config.medium_days, config.skip_days)
    momentum_slow = _skip_return(close, config.slow_days, config.skip_days)
    score = 0.20 * momentum_fast + 0.50 * momentum_medium + 0.30 * momentum_slow

    trend_slow = close > close.rolling(config.trend_days).mean()
    trend_fast = close > close.rolling(config.fast_trend_days).mean()
    absolute_positive = momentum_slow > 0.0
    trailing_stop = (
        high.rolling(config.trailing_high_days).max().shift(1)
        - config.atr_multiple * _atr(high, low, close, config.atr_days)
    )
    stop_ok = close > trailing_stop
    eligible = trend_slow & trend_fast & absolute_positive & stop_ok & volatility.gt(0)

    market = close[benchmark]
    market_vol = returns[benchmark].rolling(config.volatility_days).std() * np.sqrt(TRADING_DAYS)
    market_peak = market.rolling(config.trend_days, min_periods=config.fast_trend_days).max()
    market_drawdown = market / market_peak - 1.0
    market_trend = market > market.rolling(config.trend_days).mean()
    stress = (market_vol > config.stress_volatility) | (market_drawdown < config.stress_drawdown)
    risk_on = market_trend & ~stress
    exposure = pd.Series(config.risk_off_exposure, index=close.index, dtype=float)
    exposure.loc[risk_on] = 1.0

    ranks = score.where(eligible).rank(axis=1, ascending=False, method="first")
    rebalance = pd.Series(close.index.weekday == config.rebalance_weekday, index=close.index)
    selected = _buffered_selection(
        ranks,
        eligible,
        rebalance,
        top_n=config.top_n,
        rank_buffer=config.rank_buffer,
    )
    inverse_vol = (1.0 / volatility.where(selected)).replace(
        [np.inf, -np.inf], np.nan
    )
    raw_weights = inverse_vol.div(inverse_vol.sum(axis=1), axis=0).fillna(0.0)
    capped = _cap_and_redistribute(raw_weights, config.max_asset_weight)
    estimated_vol = np.sqrt(((capped * volatility) ** 2).sum(axis=1))
    vol_scale = (config.target_volatility / estimated_vol).clip(lower=0.0, upper=1.0)
    desired = capped.mul(vol_scale, axis=0).fillna(0.0)
    target = _schedule_with_daily_exits(desired, eligible, risk_on, rebalance)
    held_and_safe = target > 0.0

    reason = pd.DataFrame("not_selected", index=close.index, columns=close.columns)
    reason = reason.mask(~trend_slow, "below_slow_trend")
    reason = reason.mask(trend_slow & ~trend_fast, "below_fast_trend")
    reason = reason.mask(trend_slow & trend_fast & ~absolute_positive, "negative_absolute_momentum")
    reason = reason.mask(trend_slow & trend_fast & absolute_positive & ~stop_ok, "trailing_stop")
    reason = reason.mask(held_and_safe, "held")
    reason.loc[~risk_on, :] = "market_kill_switch"

    evidence = _evidence_long(
        close,
        score,
        ranks,
        volatility,
        momentum_fast,
        momentum_medium,
        momentum_slow,
        trend_fast,
        trend_slow,
        trailing_stop,
        target,
        reason,
    )
    market_state = pd.DataFrame(
        {
            "benchmark_close": market,
            "benchmark_slow_average": market.rolling(config.trend_days).mean(),
            "benchmark_volatility": market_vol,
            "benchmark_drawdown": market_drawdown,
            "market_trend_positive": market_trend,
            "stress": stress,
            "risk_on": risk_on,
            "gross_exposure_cap": exposure,
        }
    )
    return StrategyRun(target, score, evidence, market_state, reason)


def _skip_return(prices: pd.DataFrame, lookback: int, skip: int) -> pd.DataFrame:
    return prices.shift(skip).div(prices.shift(lookback + skip)).sub(1.0)


def _atr(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, days: int
) -> pd.DataFrame:
    prior_close = close.shift(1)
    true_range = pd.DataFrame(
        np.maximum.reduce(
            [
                (high - low).to_numpy(),
                (high - prior_close).abs().to_numpy(),
                (low - prior_close).abs().to_numpy(),
            ]
        ),
        index=close.index,
        columns=close.columns,
    )
    return true_range.rolling(days).mean()


def _cap_and_redistribute(weights: pd.DataFrame, cap: float) -> pd.DataFrame:
    result = weights.copy()
    for _ in range(result.shape[1]):
        over = result > cap
        if not over.any(axis=None):
            break
        excess = result.where(over, 0.0).sub(cap).clip(lower=0.0).sum(axis=1)
        result = result.clip(upper=cap)
        room = (cap - result).clip(lower=0.0).where(result > 0.0, 0.0)
        room_total = room.sum(axis=1).replace(0.0, np.nan)
        result = result.add(room.div(room_total, axis=0).mul(excess, axis=0)).fillna(result)
    return result


def _schedule_with_daily_exits(
    desired: pd.DataFrame,
    eligible: pd.DataFrame,
    risk_on: pd.Series,
    rebalance: pd.Series,
) -> pd.DataFrame:
    """Latch entries to rebalance day while allowing irreversible between-cycle exits."""
    target = pd.DataFrame(0.0, index=desired.index, columns=desired.columns)
    active = pd.Series(0.0, index=desired.columns)
    for date in desired.index:
        if bool(rebalance.loc[date]):
            active = desired.loc[date].copy()
        active = active.where(eligible.loc[date], 0.0)
        if not bool(risk_on.loc[date]):
            active[:] = 0.0
        target.loc[date] = active
    return target


def _buffered_selection(
    ranks: pd.DataFrame,
    eligible: pd.DataFrame,
    rebalance: pd.Series,
    *,
    top_n: int,
    rank_buffer: int,
) -> pd.DataFrame:
    """Retain eligible incumbents inside a rank buffer to reduce boundary churn."""
    selected = pd.DataFrame(False, index=ranks.index, columns=ranks.columns)
    active: list[str] = []
    for date in ranks.index:
        active = [ticker for ticker in active if bool(eligible.loc[date, ticker])]
        if bool(rebalance.loc[date]):
            active = [
                ticker
                for ticker in active
                if pd.notna(ranks.loc[date, ticker])
                and float(ranks.loc[date, ticker]) <= top_n + rank_buffer
            ]
            ordered = ranks.loc[date].dropna().sort_values().index
            for ticker in ordered:
                if len(active) >= top_n:
                    break
                if bool(eligible.loc[date, ticker]) and ticker not in active:
                    active.append(ticker)
        selected.loc[date, active] = True
    return selected


def _evidence_long(
    close: pd.DataFrame,
    score: pd.DataFrame,
    ranks: pd.DataFrame,
    volatility: pd.DataFrame,
    momentum_fast: pd.DataFrame,
    momentum_medium: pd.DataFrame,
    momentum_slow: pd.DataFrame,
    trend_fast: pd.DataFrame,
    trend_slow: pd.DataFrame,
    trailing_stop: pd.DataFrame,
    target: pd.DataFrame,
    reason: pd.DataFrame,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for ticker in close.columns:
        frames.append(
            pd.DataFrame(
                {
                    "date": close.index,
                    "ticker": ticker,
                    "close": close[ticker],
                    "score": score[ticker],
                    "rank": ranks[ticker],
                    "volatility": volatility[ticker],
                    "momentum_fast": momentum_fast[ticker],
                    "momentum_medium": momentum_medium[ticker],
                    "momentum_slow": momentum_slow[ticker],
                    "fast_trend_positive": trend_fast[ticker],
                    "slow_trend_positive": trend_slow[ticker],
                    "trailing_stop": trailing_stop[ticker],
                    "target_weight": target[ticker],
                    "decision_reason": reason[ticker],
                }
            )
        )
    return pd.concat(frames, ignore_index=True)
