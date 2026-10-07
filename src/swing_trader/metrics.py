from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def performance_metrics(
    returns: pd.Series,
    equity: pd.Series,
    turnover: pd.Series,
    costs: pd.Series,
) -> dict[str, float | str]:
    clean = returns.dropna()
    if clean.empty:
        raise ValueError("Cannot score empty returns.")
    aligned_equity = equity.reindex(clean.index).dropna()
    years = max((clean.index[-1] - clean.index[0]).days / 365.25, 1 / 365.25)
    initial = float(aligned_equity.iloc[0] / (1.0 + clean.iloc[0]))
    cagr = (float(aligned_equity.iloc[-1]) / initial) ** (1.0 / years) - 1.0
    volatility = float(clean.std(ddof=1) * math.sqrt(TRADING_DAYS))
    annual_return = float(clean.mean() * TRADING_DAYS)
    downside = float(clean.clip(upper=0.0).std(ddof=1) * math.sqrt(TRADING_DAYS))
    drawdown = aligned_equity.div(aligned_equity.cummax()).sub(1.0)
    max_drawdown = float(drawdown.min())
    q05 = float(clean.quantile(0.05))
    cvar95 = float(clean.loc[clean <= q05].mean())
    return {
        "start": str(clean.index[0].date()),
        "end": str(clean.index[-1].date()),
        "years": years,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe_zero_rf": _ratio(annual_return, volatility),
        "sortino_zero_rf": _ratio(annual_return, downside),
        "max_drawdown": max_drawdown,
        "calmar": _ratio(cagr, abs(max_drawdown)),
        "worst_day": float(clean.min()),
        "cvar_95_daily": cvar95,
        "positive_day_rate": float((clean > 0).mean()),
        "average_daily_turnover": float(turnover.reindex(clean.index).mean()),
        "annualized_turnover": float(turnover.reindex(clean.index).mean() * TRADING_DAYS),
        "total_cost_fraction": float(costs.reindex(clean.index).sum()),
    }


def regime_metrics(returns: pd.Series, equity: pd.Series) -> pd.DataFrame:
    windows = {
        "global_financial_crisis": ("2007-10-01", "2009-06-30"),
        "covid_crash_and_rebound": ("2020-02-01", "2020-12-31"),
        "inflation_rate_shock": ("2022-01-01", "2022-12-31"),
        "post_ai_release_cycle": ("2023-01-01", "2026-12-31"),
    }
    rows = []
    for name, (start, end) in windows.items():
        sample = returns.loc[start:end].dropna()
        sample_equity = equity.reindex(sample.index).dropna()
        if len(sample) < 2:
            continue
        drawdown = sample_equity.div(sample_equity.cummax()).sub(1.0)
        rows.append(
            {
                "regime": name,
                "start": str(sample.index.min().date()),
                "end": str(sample.index.max().date()),
                "total_return": float((1.0 + sample).prod() - 1.0),
                "annualized_volatility": float(sample.std() * np.sqrt(TRADING_DAYS)),
                "max_drawdown": float(drawdown.min()),
                "worst_day": float(sample.min()),
            }
        )
    return pd.DataFrame(rows).set_index("regime")


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if abs(denominator) < 1e-12 else numerator / denominator
