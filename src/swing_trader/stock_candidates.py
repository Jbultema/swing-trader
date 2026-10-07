from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from swing_trader.provenance import file_sha256, implementation_sha256, tabular_sha256
from swing_trader.stock_live_data import (
    audit_current_stock_price_snapshot,
    load_locked_current_universe,
)
from swing_trader.stock_signals import SCORE_COLUMNS, build_stock_features, rank_stock_candidates


class CandidateSnapshotError(ValueError):
    """Raised when a prospective candidate screen cannot be proven causal and fresh."""


@dataclass(frozen=True)
class CandidateSnapshot:
    path: Path
    as_of_session: str
    family_candidates: int
    validation_symbols: int
    record_sha256: str


def record_current_stock_candidates(
    universe_manifest_path: Path,
    price_manifest_path: Path,
    output_dir: Path,
    *,
    families: tuple[str, ...] = tuple(SCORE_COLUMNS),
    top_n: int = 10,
    validation_symbol_limit: int = 23,
    benchmark: str = "SPY",
    now: datetime | None = None,
) -> CandidateSnapshot:
    """Lock one close-known, research-only candidate screen before future outcomes exist."""
    recorded_at = _as_utc(now or datetime.now(UTC))
    universe, universe_manifest = load_locked_current_universe(
        universe_manifest_path,
        now=recorded_at,
    )
    price_audit = audit_current_stock_price_snapshot(
        price_manifest_path,
        universe_manifest_path=universe_manifest_path,
        now=recorded_at,
    )
    if not price_audit.passed:
        raise CandidateSnapshotError(
            f"Stock-price snapshot failed its gate: {json.dumps(price_audit.to_dict())}"
        )
    price_manifest = _read_json(price_manifest_path)
    prices_path = price_manifest_path.parent / str(price_manifest["data_file"])
    prices = pd.read_parquet(prices_path)
    prices.columns = pd.MultiIndex.from_tuples(prices.columns, names=["field", "ticker"])
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index)).tz_localize(None)
    membership = pd.DataFrame(False, index=prices.index, columns=prices["Close"].columns)
    current = membership.columns.intersection(universe["ticker"].astype(str))
    membership.loc[:, current] = True
    features = build_stock_features(prices, membership)
    as_of = pd.Timestamp(prices.index.max())
    family_rows, validation_symbols = screen_latest_candidates(
        features,
        as_of,
        families=families,
        top_n=top_n,
        validation_symbol_limit=validation_symbol_limit,
        benchmark=benchmark,
    )
    market_state = _market_state(prices["Close"][benchmark].dropna(), as_of)
    primary_close = prices["Close"].loc[as_of]
    payload: dict[str, object] = {
        "schema_version": 1,
        "record_type": "prospective_stock_candidate_screen",
        "data_cost_policy": "no_paid_sources",
        "research_status": "candidate_screen_not_portfolio_state",
        "action_authorized": False,
        "execution_assumption": "signal_after_regular_close_execute_no_earlier_than_next_open",
        "as_of_session": as_of.date().isoformat(),
        "recorded_at_utc": recorded_at.isoformat(),
        "families": list(families),
        "top_n_per_family": top_n,
        "market_state": market_state,
        "family_candidates": family_rows,
        "validation_symbol_limit": validation_symbol_limit,
        "validation_symbols": validation_symbols,
        "primary_latest_close": {
            ticker: float(primary_close[ticker])
            for ticker in validation_symbols
            if ticker in primary_close.index and pd.notna(primary_close[ticker])
        },
        "inputs": {
            "universe_manifest": universe_manifest_path.name,
            "universe_manifest_sha256": file_sha256(universe_manifest_path),
            "universe_tabular_sha256": universe_manifest.get("tabular_sha256"),
            "price_manifest": price_manifest_path.name,
            "price_manifest_sha256": file_sha256(price_manifest_path),
            "price_tabular_sha256": price_manifest.get("tabular_sha256"),
            "feature_tabular_sha256": tabular_sha256(features.loc[[as_of]]),
            "implementation_sha256": implementation_sha256(),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    record_hash = hashlib.sha256(canonical).hexdigest()
    payload["record_sha256"] = record_hash
    stamp = recorded_at.strftime("%Y%m%dT%H%M%S%fZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"candidates-{stamp}-{record_hash[:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    unique = {row["ticker"] for row in family_rows}
    return CandidateSnapshot(path, as_of.date().isoformat(), len(unique), len(validation_symbols), record_hash)


def screen_latest_candidates(
    features: pd.DataFrame,
    as_of: pd.Timestamp | str,
    *,
    families: tuple[str, ...],
    top_n: int,
    validation_symbol_limit: int,
    benchmark: str = "SPY",
) -> tuple[list[dict[str, object]], list[str]]:
    if top_n < 1 or validation_symbol_limit < 1:
        raise ValueError("Candidate and validation limits must be positive.")
    unknown = set(families) - set(SCORE_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown stock signal families: {sorted(unknown)}")
    rows: list[dict[str, object]] = []
    ranks_by_ticker: dict[str, list[int]] = {}
    scores_by_ticker: dict[str, list[float]] = {}
    for family in families:
        candidates = rank_stock_candidates(features, family, as_of, top_n=top_n)
        score_column = SCORE_COLUMNS[family]
        for ticker, row in candidates.iterrows():
            ticker_text = str(ticker)
            rank = int(row["candidate_rank"])
            score = float(row[score_column])
            ranks_by_ticker.setdefault(ticker_text, []).append(rank)
            scores_by_ticker.setdefault(ticker_text, []).append(score)
            rows.append(
                {
                    "ticker": ticker_text,
                    "signal_family": family,
                    "rank": rank,
                    "score": score,
                    "close": _optional_float(row.get("close")),
                    "return_21d": _optional_float(row.get("return_21d")),
                    "return_63d": _optional_float(row.get("return_63d")),
                    "return_12_1": _optional_float(row.get("return_12_1")),
                    "proximity_52w_high": _optional_float(row.get("proximity_52w_high")),
                    "volume_ratio_20_126": _optional_float(row.get("volume_ratio_20_126")),
                    "atr_fraction_14d": _optional_float(row.get("atr_fraction_14d")),
                    "why": [
                        "eligible_current_constituent",
                        "price_and_liquidity_floor_passed",
                        "50_day_average_above_200_day_average",
                        f"top_{top_n}_{family}",
                    ],
                }
            )
    ordered = sorted(
        ranks_by_ticker,
        key=lambda ticker: (
            -len(ranks_by_ticker[ticker]),
            sum(ranks_by_ticker[ticker]) / len(ranks_by_ticker[ticker]),
            -(sum(scores_by_ticker[ticker]) / len(scores_by_ticker[ticker])),
            ticker,
        ),
    )
    shortlist = ordered[:validation_symbol_limit]
    if benchmark not in shortlist:
        shortlist.append(benchmark)
    return rows, shortlist


def verify_candidate_snapshot(path: Path) -> bool:
    payload = _read_json(path)
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def latest_candidate_snapshot(output_dir: Path) -> Path:
    paths = sorted(output_dir.glob("candidates-*.json"))
    if not paths:
        raise FileNotFoundError(f"No prospective stock candidate snapshots found in {output_dir}.")
    return paths[-1]


def _market_state(close: pd.Series, as_of: pd.Timestamp) -> dict[str, object]:
    history = close.loc[:as_of]
    moving_average_200 = float(history.rolling(200).mean().iloc[-1])
    realized_volatility_20 = float(history.pct_change(fill_method=None).rolling(20).std().iloc[-1] * 252**0.5)
    latest = float(history.iloc[-1])
    risk_on = latest > moving_average_200 and realized_volatility_20 <= 0.35
    return {
        "benchmark": str(close.name),
        "close": latest,
        "moving_average_200": moving_average_200,
        "realized_volatility_20_annualized": realized_volatility_20,
        "risk_on": risk_on,
        "rule": "close_above_200dma_and_20d_annualized_volatility_at_or_below_35pct",
        "role": "preregistered_shadow_variant_gate_not_retrospective_evidence",
    }


def _optional_float(value: object) -> float | None:
    return float(value) if pd.notna(value) else None


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CandidateSnapshotError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
