from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

import numpy as np
import pandas as pd

SHARE_TURNOVER_SIGNAL_FAMILY = "share_turnover_skip3"
SHARE_TURNOVER_METHOD = (
    "independent_top_quintiles_of_return_and_share_turnover_"
    "over_t_minus_20_through_t_minus_3"
)


class ShareTurnoverSignalError(ValueError):
    """Raised when the forward-only share-turnover signal cannot be formed safely."""


@dataclass(frozen=True)
class ShareTurnoverSignalConfig:
    lookback_sessions: int = 21
    skip_recent_sessions: int = 3
    quantile_threshold: float = 0.80
    minimum_price: float = 10.0
    liquidity_sessions: int = 20
    minimum_median_dollar_volume: float = 20_000_000.0
    maximum_share_observation_age_calendar_days: int = 130

    @property
    def measurement_sessions(self) -> int:
        return self.lookback_sessions - self.skip_recent_sessions


def build_share_turnover_features(
    ohlcv: pd.DataFrame,
    shares_snapshot: pd.DataFrame,
    as_of: pd.Timestamp | str,
    *,
    config: ShareTurnoverSignalConfig | None = None,
) -> pd.DataFrame:
    """Build a causal long-only adaptation of Medhat-Schmeling STMOM.

    Return and total-volume measurements exclude the latest three sessions. Shares
    outstanding are a current value that becomes usable only at the snapshot capture;
    provider observation dates are never used to backfill earlier signal dates.
    """
    cfg = config or ShareTurnoverSignalConfig()
    _validate_config(cfg)
    if not isinstance(ohlcv.columns, pd.MultiIndex):
        raise ShareTurnoverSignalError("OHLCV columns must use field/ticker levels.")
    required_fields = {"Close", "Volume"}
    if missing := required_fields - set(ohlcv.columns.get_level_values(0)):
        raise ShareTurnoverSignalError(f"OHLCV is missing fields: {sorted(missing)}")
    if ohlcv.empty or ohlcv.index.has_duplicates or not ohlcv.index.is_monotonic_increasing:
        raise ShareTurnoverSignalError("OHLCV sessions must be non-empty, unique, and sorted.")
    session = pd.Timestamp(as_of).tz_localize(None)
    if session not in ohlcv.index:
        raise ShareTurnoverSignalError("Requested share-turnover session is absent from OHLCV.")
    close = ohlcv["Close"].astype(float).loc[:session]
    volume = ohlcv["Volume"].astype(float).loc[:session]
    if len(close) <= cfg.lookback_sessions:
        raise ShareTurnoverSignalError("Insufficient sessions for share-turnover formation.")

    shares = _usable_shares(shares_snapshot, cfg)
    formation_return = close.shift(cfg.skip_recent_sessions).div(
        close.shift(cfg.lookback_sessions)
    ).sub(1.0).loc[session]
    formation_volume = (
        volume.shift(cfg.skip_recent_sessions)
        .rolling(cfg.measurement_sessions, min_periods=cfg.measurement_sessions)
        .sum()
        .loc[session]
    )
    current_close = close.loc[session]
    median_dollar_volume = close.mul(volume).rolling(cfg.liquidity_sessions).median().loc[session]
    aligned_shares = shares.reindex(current_close.index)
    share_turnover = formation_volume.div(aligned_shares)
    eligible = (
        current_close.ge(cfg.minimum_price)
        & median_dollar_volume.ge(cfg.minimum_median_dollar_volume)
        & formation_return.map(np.isfinite)
        & share_turnover.map(np.isfinite)
        & share_turnover.gt(0.0)
        & aligned_shares.gt(0.0)
    )
    return_percentile = formation_return.where(eligible).rank(pct=True, method="average")
    turnover_percentile = share_turnover.where(eligible).rank(pct=True, method="average")
    combined_score = (return_percentile + turnover_percentile) / 2.0
    winner = return_percentile.gt(cfg.quantile_threshold)
    high_turnover = turnover_percentile.gt(cfg.quantile_threshold)
    candidate_eligible = eligible & winner & high_turnover
    selection_rank = combined_score.where(eligible).rank(ascending=False, method="min")

    result = pd.DataFrame(
        {
            "close": current_close,
            "formation_return_t20_t3": formation_return,
            "formation_volume_t20_t3": formation_volume,
            "shares_outstanding_at_capture": aligned_shares,
            "share_turnover_t20_t3": share_turnover,
            "median_dollar_volume_20d": median_dollar_volume,
            "return_percentile": return_percentile,
            "share_turnover_percentile": turnover_percentile,
            "score_share_turnover_skip3": combined_score,
            "signal_eligible": eligible,
            "winner_quintile": winner.fillna(False),
            "high_turnover_quintile": high_turnover.fillna(False),
            "candidate_eligible": candidate_eligible.fillna(False),
            "selection_rank": selection_rank,
        }
    )
    result.index = result.index.astype(str)
    result.index.name = "ticker"
    return result.sort_index()


def rank_share_turnover_candidates(
    features: pd.DataFrame,
    *,
    top_n: int = 10,
) -> pd.DataFrame:
    if top_n < 1:
        raise ValueError("top_n must be positive.")
    required = {
        "candidate_eligible",
        "selection_rank",
        "formation_return_t20_t3",
        "share_turnover_t20_t3",
    }
    if missing := required - set(features.columns):
        raise ShareTurnoverSignalError(
            f"Share-turnover features are missing columns: {sorted(missing)}"
        )
    selected = features.loc[features["candidate_eligible"].fillna(False).astype(bool)].copy()
    selected.insert(0, "ticker", selected.index.astype(str))
    selected = selected.reset_index(drop=True)
    selected = selected.sort_values(
        [
            "selection_rank",
            "formation_return_t20_t3",
            "share_turnover_t20_t3",
            "ticker",
        ],
        ascending=[True, False, False, True],
    ).head(top_n)
    selected.insert(1, "candidate_rank", range(1, len(selected) + 1))
    selected.insert(2, "signal_family", SHARE_TURNOVER_SIGNAL_FAMILY)
    return selected.reset_index(drop=True)


def _usable_shares(
    frame: pd.DataFrame,
    config: ShareTurnoverSignalConfig,
) -> pd.Series:
    required = {
        "ticker",
        "shares_outstanding",
        "provider_observation_date",
        "captured_at_utc",
        "source_status",
    }
    if missing := required - set(frame.columns):
        raise ShareTurnoverSignalError(
            f"Shares snapshot is missing columns: {sorted(missing)}"
        )
    if frame["ticker"].astype(str).duplicated().any():
        raise ShareTurnoverSignalError("Shares snapshot contains duplicate tickers.")
    captures = pd.to_datetime(frame["captured_at_utc"], errors="coerce", utc=True)
    if captures.isna().any() or captures.nunique() != 1:
        raise ShareTurnoverSignalError("Shares snapshot has inconsistent capture timestamps.")
    captured = captures.iloc[0].to_pydatetime().astimezone(UTC)
    observations = pd.to_datetime(frame["provider_observation_date"], errors="coerce")
    values = pd.to_numeric(frame["shares_outstanding"], errors="coerce")
    ages = pd.Series(pd.NA, index=frame.index, dtype="Int64")
    dated = observations.notna()
    ages.loc[dated] = [
        (captured.date() - value.date()).days for value in observations.loc[dated]
    ]
    usable = (
        frame["source_status"].astype(str).eq("available")
        & values.notna()
        & values.map(np.isfinite)
        & values.gt(0.0)
        & dated
        & ages.ge(0).fillna(False)
        & ages.le(config.maximum_share_observation_age_calendar_days).fillna(False)
    )
    result = pd.Series(
        values.loc[usable].astype(float).to_numpy(),
        index=frame.loc[usable, "ticker"].astype(str),
        name="shares_outstanding",
    )
    return result.sort_index()


def _validate_config(config: ShareTurnoverSignalConfig) -> None:
    if config.lookback_sessions <= config.skip_recent_sessions:
        raise ValueError("Lookback sessions must exceed skipped recent sessions.")
    if config.skip_recent_sessions < 0:
        raise ValueError("Skipped recent sessions cannot be negative.")
    if not 0.0 < config.quantile_threshold < 1.0:
        raise ValueError("Share-turnover quantile threshold must be in (0, 1).")
    if config.minimum_price <= 0.0 or config.minimum_median_dollar_volume <= 0.0:
        raise ValueError("Share-turnover liquidity thresholds must be positive.")
    if config.liquidity_sessions < 1:
        raise ValueError("Share-turnover liquidity sessions must be positive.")
    if config.maximum_share_observation_age_calendar_days < 0:
        raise ValueError("Maximum share-observation age cannot be negative.")
