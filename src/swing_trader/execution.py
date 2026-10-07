from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from swing_trader.backtest import BacktestResult


@dataclass(frozen=True)
class ExecutionCapacityReport:
    trade_count: int
    missing_liquidity_estimates: int
    maximum_adv_participation: float
    p95_adv_participation: float
    participation_limit: float
    participation_violations: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionCapacityAnalysis:
    report: ExecutionCapacityReport
    trades: pd.DataFrame


def analyze_execution_capacity(
    result: BacktestResult,
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    initial_capital: float,
    adv_sessions: int = 20,
    maximum_adv_participation: float = 0.01,
) -> ExecutionCapacityAnalysis:
    """Estimate capacity using only dollar volume known before each open.

    The execution at open ``t`` uses median daily dollar volume through close
    ``t-1``. This avoids using the execution day's eventual total volume.
    """
    if initial_capital <= 0.0:
        raise ValueError("initial_capital must be positive.")
    if adv_sessions < 1:
        raise ValueError("adv_sessions must be positive.")
    if not 0.0 < maximum_adv_participation <= 1.0:
        raise ValueError("maximum_adv_participation must be in (0, 1].")

    trades = result.trades.astype(float)
    prices = close.reindex(index=trades.index, columns=trades.columns).astype(float)
    shares = volume.reindex(index=trades.index, columns=trades.columns).astype(float)
    dollar_volume = close.astype(float).mul(volume.astype(float))
    known_adv = (
        dollar_volume.rolling(adv_sessions, min_periods=adv_sessions)
        .median()
        .shift(1)
        .reindex(index=trades.index, columns=trades.columns)
    )

    pretrade_equity = result.equity.shift(1).reindex(trades.index)
    if len(pretrade_equity):
        pretrade_equity.iloc[0] = initial_capital
    notional = trades.abs().mul(pretrade_equity, axis=0)
    participation = notional.div(known_adv)
    traded_mask = trades.abs() > 1e-12

    detail = pd.concat(
        {
            "trade_weight": trades.where(traded_mask),
            "estimated_notional": notional.where(traded_mask),
            "prior_median_dollar_volume": known_adv.where(traded_mask),
            "adv_participation": participation.where(traded_mask),
            "reference_close": prices.where(traded_mask),
            "daily_share_volume": shares.where(traded_mask),
        },
        axis=1,
    ).stack(level=1, future_stack=True)
    detail.index.names = ["date", "ticker"]
    detail = detail.loc[detail["trade_weight"].notna()].reset_index()

    observed = detail["adv_participation"].dropna()
    missing = int(detail["adv_participation"].isna().sum())
    violations = int((observed > maximum_adv_participation).sum())
    maximum = float(observed.max()) if len(observed) else np.nan
    p95 = float(observed.quantile(0.95)) if len(observed) else np.nan
    status = "passed" if len(detail) and missing == 0 and violations == 0 else "failed"
    report = ExecutionCapacityReport(
        trade_count=len(detail),
        missing_liquidity_estimates=missing,
        maximum_adv_participation=maximum,
        p95_adv_participation=p95,
        participation_limit=maximum_adv_participation,
        participation_violations=violations,
        status=status,
    )
    return ExecutionCapacityAnalysis(report=report, trades=detail)
