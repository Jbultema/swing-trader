from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from swing_trader.stock_signals import SCORE_COLUMNS, ExitPolicy, exit_reasons


@dataclass(frozen=True)
class StockStrategyPlan:
    """Close-known targets and their human-readable decision ledger."""

    target_weights: pd.DataFrame
    decisions: pd.DataFrame


@dataclass
class _Position:
    entry_price: float
    high_watermark: float
    holding_sessions: int = 0


def build_stock_strategy_plan(
    features: pd.DataFrame,
    adjusted_open: pd.DataFrame,
    family: str,
    *,
    maximum_positions: int = 10,
    maximum_position_weight: float = 0.10,
    policy: ExitPolicy | None = None,
) -> StockStrategyPlan:
    """Create stateful next-open stock targets without using future signals.

    Targets observed at close ``t`` are intended for execution at open ``t+1``.
    Entry prices are therefore recorded from the next session's adjusted open and
    only become available to exit logic at that session's close.
    """
    if family not in SCORE_COLUMNS:
        raise ValueError(f"Unknown stock signal family: {family}")
    if maximum_positions < 1:
        raise ValueError("maximum_positions must be positive.")
    if not 0.0 < maximum_position_weight <= 1.0:
        raise ValueError("maximum_position_weight must be in (0, 1].")
    if not isinstance(features.index, pd.MultiIndex) or features.index.names != [
        "date",
        "ticker",
    ]:
        raise ValueError("Features must use a date/ticker MultiIndex.")

    cfg = policy or ExitPolicy()
    dates = pd.DatetimeIndex(features.index.get_level_values("date").unique()).sort_values()
    tickers = pd.Index(features.index.get_level_values("ticker").unique()).sort_values()
    opens = adjusted_open.reindex(index=dates, columns=tickers)
    target = pd.DataFrame(0.0, index=dates, columns=tickers)
    positions: dict[str, _Position] = {}
    prior_target: set[str] = set()
    ledger: list[dict[str, object]] = []
    score_column = SCORE_COLUMNS[family]

    for date in dates:
        day = features.xs(date, level="date").reindex(tickers)

        # Apply the prior close's intended trades at today's open. This state is
        # first used by today's close signal, so the open is not leaked backward.
        for ticker in tuple(positions):
            if ticker not in prior_target:
                del positions[ticker]
        for ticker in sorted(prior_target - positions.keys()):
            entry = opens.at[date, ticker]
            if pd.notna(entry) and float(entry) > 0.0:
                positions[str(ticker)] = _Position(float(entry), float(entry))

        eligible = day["eligible"].fillna(False).astype(bool)
        positive_trend = day["trend_positive"].fillna(False).astype(bool)
        scores = pd.to_numeric(day[score_column], errors="coerce")
        ranked = scores.where(eligible & positive_trend).dropna().sort_values(ascending=False)
        ranks = pd.Series(range(1, len(ranked) + 1), index=ranked.index, dtype=float)

        exits: dict[str, list[str]] = {}
        for ticker, position in positions.items():
            row = day.loc[ticker]
            close = pd.to_numeric(pd.Series([row.get("close")]), errors="coerce").iloc[0]
            if pd.isna(close):
                exits[ticker] = ["missing_close"]
                continue
            position.holding_sessions += 1
            position.high_watermark = max(position.high_watermark, float(close))
            if not bool(eligible.loc[ticker]):
                exits[ticker] = ["left_point_in_time_universe"]
                continue
            rank = float(ranks.get(ticker, np.inf))
            if cfg.use_atr_trail:
                atr = pd.to_numeric(pd.Series([row.get("atr_fraction_14d")]), errors="coerce").iloc[
                    0
                ]
                if pd.isna(atr):
                    exits[ticker] = ["missing_risk_feature"]
                    continue
            reasons = exit_reasons(
                row,
                entry_price=position.entry_price,
                high_watermark=position.high_watermark,
                holding_sessions=position.holding_sessions,
                cross_section_rank=rank,
                entry_top_n=maximum_positions,
                policy=cfg,
            )
            if reasons:
                exits[ticker] = reasons

        survivors = set(positions) - exits.keys()
        selected = set(survivors)
        for ticker in ranked.index:
            ticker_text = str(ticker)
            if len(selected) >= maximum_positions:
                break
            if ticker_text not in exits:
                selected.add(ticker_text)

        position_weight = min(maximum_position_weight, 1.0 / maximum_positions)
        if selected:
            target.loc[date, list(selected)] = position_weight

        for ticker in sorted(exits):
            position = positions[ticker]
            ledger.append(
                _decision_row(
                    date,
                    ticker,
                    "SELL",
                    exits[ticker],
                    family,
                    day,
                    ranks,
                    score_column,
                    position,
                )
            )
        for ticker in sorted(selected):
            action = "HOLD" if ticker in survivors else "BUY"
            position = positions.get(ticker)
            ledger.append(
                _decision_row(
                    date,
                    ticker,
                    action,
                    ["retained_signal"] if action == "HOLD" else ["top_ranked_entry"],
                    family,
                    day,
                    ranks,
                    score_column,
                    position,
                )
            )
        prior_target = selected

    decisions = pd.DataFrame(ledger)
    return StockStrategyPlan(target_weights=target, decisions=decisions)


def _decision_row(
    date: pd.Timestamp,
    ticker: str,
    action: str,
    reasons: list[str],
    family: str,
    day: pd.DataFrame,
    ranks: pd.Series,
    score_column: str,
    position: _Position | None,
) -> dict[str, object]:
    row = day.loc[ticker]
    return {
        "date": date,
        "ticker": ticker,
        "action": action,
        "reasons": reasons,
        "signal_family": family,
        "rank": float(ranks[ticker]) if ticker in ranks else None,
        "score": float(row[score_column]) if pd.notna(row[score_column]) else None,
        "close": float(row["close"]) if pd.notna(row["close"]) else None,
        "entry_price": position.entry_price if position else None,
        "high_watermark": position.high_watermark if position else None,
        "holding_sessions": position.holding_sessions if position else 0,
    }
