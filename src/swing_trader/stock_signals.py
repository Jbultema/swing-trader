from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StockFeatureConfig:
    short_days: int = 21
    medium_days: int = 63
    formation_days: int = 252
    skip_days: int = 21
    high_days: int = 252
    fast_volume_days: int = 20
    slow_volume_days: int = 126
    volatility_days: int = 20
    atr_days: int = 14
    minimum_price: float = 10.0
    minimum_median_dollar_volume: float = 20_000_000.0


@dataclass(frozen=True)
class ExitPolicy:
    hard_stop_fraction: float = 0.08
    atr_multiple: float = 3.0
    maximum_holding_sessions: int = 63
    rank_exit_multiple: float = 2.0
    use_hard_stop: bool = True
    use_atr_trail: bool = True
    use_trend_break: bool = True
    use_short_momentum: bool = True
    use_rank_decay: bool = True
    use_maximum_holding: bool = True


SCORE_COLUMNS = {
    "short_volume": "score_short_volume",
    "smooth_momentum": "score_smooth_momentum",
    "volume_breakout": "score_volume_breakout",
}


def build_stock_features(
    ohlcv: pd.DataFrame,
    membership: pd.DataFrame,
    config: StockFeatureConfig | None = None,
) -> pd.DataFrame:
    """Build close-known stock features for next-session execution.

    ``membership`` must be a point-in-time boolean matrix. Present-day constituent
    lists are intentionally not accepted as a one-dimensional ticker collection.
    """
    cfg = config or StockFeatureConfig()
    _validate_inputs(ohlcv, membership)
    close = ohlcv["Close"].astype(float)
    high = ohlcv["High"].astype(float)
    low = ohlcv["Low"].astype(float)
    volume = ohlcv["Volume"].astype(float)
    membership = membership.reindex(index=close.index, columns=close.columns).fillna(False)

    daily_returns = close.pct_change(fill_method=None)
    return_short = close.div(close.shift(cfg.short_days)).sub(1.0)
    return_medium = close.div(close.shift(cfg.medium_days)).sub(1.0)
    return_12_1 = close.shift(cfg.skip_days).div(close.shift(cfg.formation_days)).sub(1.0)
    proximity_52w_high = close.div(high.rolling(cfg.high_days).max())
    dollar_volume = close.mul(volume)
    median_dollar_volume = dollar_volume.rolling(cfg.fast_volume_days).median()
    volume_ratio = (
        volume.rolling(cfg.fast_volume_days).mean().div(volume.rolling(cfg.slow_volume_days).mean())
    )
    realized_volatility = daily_returns.rolling(cfg.volatility_days).std() * np.sqrt(252)
    atr_fraction = _average_true_range(high, low, close, cfg.atr_days).div(close)

    formation_observations = cfg.formation_days - cfg.skip_days
    lagged_signs = daily_returns.shift(cfg.skip_days)
    positive_fraction = lagged_signs.gt(0.0).rolling(formation_observations).mean()
    negative_fraction = lagged_signs.lt(0.0).rolling(formation_observations).mean()
    information_discreteness = np.sign(return_12_1).mul(negative_fraction - positive_fraction)

    trend_positive = (close > close.rolling(50).mean()) & (
        close.rolling(50).mean() > close.rolling(200).mean()
    )
    eligible = (
        membership.astype(bool)
        & close.ge(cfg.minimum_price)
        & median_dollar_volume.ge(cfg.minimum_median_dollar_volume)
        & return_12_1.notna()
        & proximity_52w_high.notna()
    )

    short_rank = _cross_sectional_rank(return_short, eligible)
    medium_rank = _cross_sectional_rank(return_medium, eligible)
    formation_rank = _cross_sectional_rank(return_12_1, eligible)
    high_rank = _cross_sectional_rank(proximity_52w_high, eligible)
    volume_rank = _cross_sectional_rank(volume_ratio, eligible)
    smooth_rank = _cross_sectional_rank(-information_discreteness, eligible)

    fields = {
        "close": close,
        "return_21d": return_short,
        "return_63d": return_medium,
        "return_12_1": return_12_1,
        "proximity_52w_high": proximity_52w_high,
        "volume_ratio_20_126": volume_ratio,
        "median_dollar_volume_20d": median_dollar_volume,
        "realized_volatility_20d": realized_volatility,
        "atr_fraction_14d": atr_fraction,
        "information_discreteness": information_discreteness,
        "trend_positive": trend_positive,
        "eligible": eligible,
        "score_short_volume": 0.50 * short_rank + 0.30 * high_rank + 0.20 * volume_rank,
        "score_smooth_momentum": (0.50 * formation_rank + 0.25 * medium_rank + 0.25 * smooth_rank),
        "score_volume_breakout": 0.45 * high_rank + 0.35 * medium_rank + 0.20 * volume_rank,
    }
    long_fields = {
        name: values.stack(future_stack=True).rename(name) for name, values in fields.items()
    }
    result = pd.concat(long_fields.values(), axis=1)
    result.index.names = ["date", "ticker"]
    return result.sort_index()


def rank_stock_candidates(
    features: pd.DataFrame,
    family: str,
    as_of: pd.Timestamp | str,
    *,
    top_n: int = 10,
) -> pd.DataFrame:
    """Return the strongest eligible, positive-trend candidates for one close."""
    if family not in SCORE_COLUMNS:
        raise ValueError(f"Unknown stock signal family: {family}")
    date = pd.Timestamp(as_of)
    day = features.xs(date, level="date").copy()
    score_column = SCORE_COLUMNS[family]
    selected = day.loc[day["eligible"].astype(bool) & day["trend_positive"].astype(bool)]
    selected = selected.sort_values(score_column, ascending=False).head(top_n)
    selected.insert(0, "candidate_rank", range(1, len(selected) + 1))
    selected.insert(1, "signal_family", family)
    return selected


def exit_reasons(
    feature_row: pd.Series,
    *,
    entry_price: float,
    high_watermark: float,
    holding_sessions: int,
    cross_section_rank: float,
    entry_top_n: int,
    policy: ExitPolicy | None = None,
) -> list[str]:
    """Explain which close-known conditions require a next-session exit."""
    cfg = policy or ExitPolicy()
    close = float(feature_row["close"])
    triggers = exit_trigger_levels(
        feature_row,
        entry_price=entry_price,
        high_watermark=high_watermark,
        policy=cfg,
    )
    reasons: list[str] = []
    hard_loss_trigger = triggers["hard_loss_trigger_adjusted_close"]
    if hard_loss_trigger is not None and close <= hard_loss_trigger:
        reasons.append("hard_loss_limit")
    if cfg.use_atr_trail:
        atr_trailing_trigger = triggers["atr_trailing_trigger_adjusted_close"]
        if atr_trailing_trigger is None:
            reasons.append("missing_risk_feature")
        elif close <= atr_trailing_trigger:
            reasons.append("atr_trailing_exit")
    if cfg.use_trend_break and not bool(feature_row["trend_positive"]):
        reasons.append("trend_broken")
    if cfg.use_short_momentum and float(feature_row["return_21d"]) <= 0.0:
        reasons.append("short_momentum_non_positive")
    if cfg.use_rank_decay and cross_section_rank > entry_top_n * cfg.rank_exit_multiple:
        reasons.append("rank_decay")
    if cfg.use_maximum_holding and holding_sessions >= cfg.maximum_holding_sessions:
        reasons.append("maximum_holding_period")
    return reasons


def exit_trigger_levels(
    feature_row: pd.Series,
    *,
    entry_price: float,
    high_watermark: float,
    policy: ExitPolicy | None = None,
) -> dict[str, float | None]:
    """Return close-known risk thresholds in adjusted-price units.

    The ATR trail is a close-based Chandelier-style trigger: the post-entry
    high-water close minus ``atr_multiple`` times the current 14-session ATR.
    ``atr_fraction_14d`` is converted back to price units using the current
    adjusted close before that distance is subtracted.
    """
    cfg = policy or ExitPolicy()
    close = _finite_non_negative(feature_row.get("close"))
    entry = _finite_non_negative(entry_price)
    high_water = _finite_non_negative(high_watermark)
    atr_fraction = _finite_non_negative(feature_row.get("atr_fraction_14d"))

    hard_loss_trigger = (
        entry * (1.0 - cfg.hard_stop_fraction)
        if cfg.use_hard_stop and entry is not None
        else None
    )
    atr_adjusted_price = (
        close * atr_fraction
        if cfg.use_atr_trail and close is not None and atr_fraction is not None
        else None
    )
    atr_trailing_trigger = (
        high_water - cfg.atr_multiple * atr_adjusted_price
        if high_water is not None and atr_adjusted_price is not None
        else None
    )
    return {
        "hard_loss_trigger_adjusted_close": hard_loss_trigger,
        "atr_adjusted_price": atr_adjusted_price,
        "atr_trailing_trigger_adjusted_close": atr_trailing_trigger,
    }


def exit_policy_for_family(
    family: str,
    *,
    hard_stop_fraction: float = 0.08,
    atr_multiple: float = 3.0,
    maximum_holding_sessions: int = 63,
    rank_exit_multiple: float = 2.0,
) -> ExitPolicy:
    """Create preregistered exit ablations with a common emergency loss limit."""
    enabled = {
        "time_stop": {"use_maximum_holding"},
        "rank_decay": {"use_rank_decay", "use_maximum_holding"},
        "trend_break": {
            "use_trend_break",
            "use_short_momentum",
            "use_maximum_holding",
        },
        "atr_trailing": {"use_atr_trail", "use_maximum_holding"},
        "combined": {
            "use_atr_trail",
            "use_trend_break",
            "use_short_momentum",
            "use_rank_decay",
            "use_maximum_holding",
        },
    }
    if family not in enabled:
        raise ValueError(f"Unknown exit family: {family}")
    active = enabled[family]
    return ExitPolicy(
        hard_stop_fraction=hard_stop_fraction,
        atr_multiple=atr_multiple,
        maximum_holding_sessions=maximum_holding_sessions,
        rank_exit_multiple=rank_exit_multiple,
        use_hard_stop=True,
        use_atr_trail="use_atr_trail" in active,
        use_trend_break="use_trend_break" in active,
        use_short_momentum="use_short_momentum" in active,
        use_rank_decay="use_rank_decay" in active,
        use_maximum_holding="use_maximum_holding" in active,
    )


def _cross_sectional_rank(values: pd.DataFrame, eligible: pd.DataFrame) -> pd.DataFrame:
    return values.where(eligible).rank(axis=1, pct=True, method="average")


def _finite_non_negative(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) and number >= 0.0 else None


def _average_true_range(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, days: int
) -> pd.DataFrame:
    previous_close = close.shift(1)
    components = pd.concat(
        {
            "intraday": high - low,
            "gap_up": (high - previous_close).abs(),
            "gap_down": (low - previous_close).abs(),
        },
        axis=1,
    )
    true_range = components.T.groupby(level=1).max().T
    return true_range.rolling(days).mean()


def _validate_inputs(ohlcv: pd.DataFrame, membership: pd.DataFrame) -> None:
    if not isinstance(ohlcv.columns, pd.MultiIndex):
        raise ValueError("OHLCV data must use field/ticker MultiIndex columns.")
    required = {"Close", "High", "Low", "Volume"}
    fields = set(ohlcv.columns.get_level_values(0))
    if missing := required - fields:
        raise ValueError(f"Missing stock feature fields: {sorted(missing)}")
    if not isinstance(membership, pd.DataFrame) or membership.empty:
        raise ValueError("Point-in-time membership must be a non-empty date/ticker matrix.")
