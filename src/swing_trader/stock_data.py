from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class StockCoverageAudit:
    """Point-in-time coverage evidence for a stock price panel."""

    universe_tickers: int
    price_tickers: int
    missing_tickers: tuple[str, ...]
    expected_member_observations: int
    covered_member_observations: int
    member_observation_coverage: float
    eligible_after_warmup_observations: int
    warmup_coverage: float
    minimum_history_sessions: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def write_stock_coverage_audit(
    close: pd.DataFrame,
    membership: pd.DataFrame,
    output: Path,
    *,
    minimum_history_sessions: int = 252,
) -> StockCoverageAudit:
    audit = audit_stock_coverage(
        close,
        membership,
        minimum_history_sessions=minimum_history_sessions,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit.to_dict(), indent=2) + "\n", encoding="utf-8")
    return audit


def audit_stock_coverage(
    close: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    minimum_history_sessions: int = 252,
    minimum_member_coverage: float = 0.99,
    minimum_warmup_coverage: float = 0.95,
) -> StockCoverageAudit:
    """Reject stock panels that silently omit PIT members or signal history.

    ``membership`` is the dated investment universe. A covered member observation
    has a price on that date. A warm observation has at least the configured number
    of prior non-null price observations, including the current session.
    """
    if close.empty or membership.empty:
        raise ValueError("Close prices and point-in-time membership must be non-empty.")
    if minimum_history_sessions < 1:
        raise ValueError("minimum_history_sessions must be positive.")
    if not 0.0 <= minimum_member_coverage <= 1.0:
        raise ValueError("minimum_member_coverage must be between zero and one.")
    if not 0.0 <= minimum_warmup_coverage <= 1.0:
        raise ValueError("minimum_warmup_coverage must be between zero and one.")

    universe = membership.astype(bool).sort_index()
    prices = close.sort_index().reindex(index=universe.index, columns=universe.columns)
    member_tickers = tuple(sorted(str(ticker) for ticker in universe.columns[universe.any()]))
    available_tickers = {str(ticker) for ticker in prices.columns if prices[ticker].notna().any()}
    missing_tickers = tuple(ticker for ticker in member_tickers if ticker not in available_tickers)

    expected = int(universe.to_numpy().sum())
    covered_mask = universe & prices.notna()
    covered = int(covered_mask.to_numpy().sum())
    member_coverage = covered / expected if expected else 0.0

    history_count = prices.notna().cumsum()
    warm_mask = covered_mask & history_count.ge(minimum_history_sessions)
    warm = int(warm_mask.to_numpy().sum())
    warmup_coverage = warm / expected if expected else 0.0

    passed = (
        not missing_tickers
        and member_coverage >= minimum_member_coverage
        and warmup_coverage >= minimum_warmup_coverage
    )
    return StockCoverageAudit(
        universe_tickers=len(member_tickers),
        price_tickers=len(available_tickers),
        missing_tickers=missing_tickers,
        expected_member_observations=expected,
        covered_member_observations=covered,
        member_observation_coverage=member_coverage,
        eligible_after_warmup_observations=warm,
        warmup_coverage=warmup_coverage,
        minimum_history_sessions=minimum_history_sessions,
        status="passed" if passed else "failed",
    )
