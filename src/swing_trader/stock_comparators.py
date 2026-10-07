from __future__ import annotations

import pandas as pd

from swing_trader.backtest import month_end_mask


def point_in_time_equal_weight_weights(
    membership: pd.DataFrame,
) -> pd.DataFrame:
    """Monthly equal weight across the point-in-time member set."""
    members = membership.fillna(False).astype(bool).sort_index()
    raw = members.astype(float).div(members.sum(axis=1).replace(0.0, 1.0), axis=0)
    return _month_end_schedule(raw, members)


def classic_stock_momentum_weights(
    close: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    top_n: int = 10,
    formation_days: int = 252,
    skip_days: int = 21,
) -> pd.DataFrame:
    """Long-only monthly top-N 12-1 stock momentum comparator."""
    if top_n < 1:
        raise ValueError("top_n must be positive.")
    prices = close.sort_index().astype(float)
    members = membership.reindex(index=prices.index, columns=prices.columns).fillna(False)
    momentum = prices.shift(skip_days).div(prices.shift(formation_days)).sub(1.0)
    ranks = momentum.where(members.astype(bool)).rank(axis=1, ascending=False, method="first")
    selected = (ranks <= top_n) & momentum.gt(0.0) & members.astype(bool)
    raw = selected.astype(float).div(selected.sum(axis=1).replace(0.0, 1.0), axis=0)
    return _month_end_schedule(raw, members.astype(bool))


def _month_end_schedule(weights: pd.DataFrame, membership: pd.DataFrame) -> pd.DataFrame:
    completed = month_end_mask(weights.index)
    scheduled = weights.loc[completed].reindex(weights.index).ffill().fillna(0.0)
    scheduled = scheduled.where(membership.reindex_like(scheduled).fillna(False), 0.0)
    return scheduled.reindex(columns=weights.columns, fill_value=0.0)
