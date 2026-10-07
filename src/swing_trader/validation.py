from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from swing_trader.backtest import BacktestResult, classic_dual_momentum_weights, run_backtest
from swing_trader.config import AppConfig
from swing_trader.metrics import performance_metrics


def build_validation_artifacts(
    prices: pd.DataFrame,
    config: AppConfig,
    primary_results: dict[str, BacktestResult],
    output_dir: Path,
) -> None:
    close = prices["Close"]
    opens = prices["Open"]
    cash_open = opens[config.data.cash_proxy]
    cash_returns = cash_open.shift(-1).div(cash_open).sub(1.0)
    evaluation_start = prices.index[config.validation.minimum_history_days]
    variants: dict[str, BacktestResult] = {}
    rows: list[dict[str, object]] = []
    for lookback, top_n in itertools.product((189, 252, 315), (2, 3, 4)):
        name = f"dual_{lookback}d_top{top_n}"
        weights = classic_dual_momentum_weights(
            close, config.data.tickers, top_n=top_n, lookback_days=lookback
        )
        result = run_backtest(
            name,
            opens,
            weights,
            config.execution,
            cash_returns=cash_returns,
        )
        variants[name] = result
        for split, start, end in (
            ("development", evaluation_start, config.validation.development_end),
            ("validation", config.validation.validation_start, config.validation.validation_end),
            ("recent_diagnostic", config.validation.recent_diagnostic_start, None),
        ):
            sample = result.returns.loc[start:end]
            metrics = performance_metrics(
                sample,
                result.equity.loc[start:end],
                result.turnover.loc[start:end],
                result.transaction_costs.loc[start:end],
            )
            rows.append(
                {
                    "variant": name,
                    "lookback_days": lookback,
                    "top_n": top_n,
                    "split": split,
                    **metrics,
                }
            )
    pd.DataFrame(rows).to_csv(output_dir / "parameter_stability.csv", index=False)

    cost_rows = []
    champion_weights = classic_dual_momentum_weights(
        close, config.data.tickers, top_n=config.strategy.top_n
    )
    for bps in (0.0, 5.0, 10.0, 25.0, 50.0):
        execution = replace(config.execution, transaction_cost_bps=bps)
        result = run_backtest(
            f"cost_{bps:g}bps",
            opens,
            champion_weights,
            execution,
            cash_returns=cash_returns,
        )
        metrics = performance_metrics(
            result.returns.loc[evaluation_start:],
            result.equity.loc[evaluation_start:],
            result.turnover.loc[evaluation_start:],
            result.transaction_costs.loc[evaluation_start:],
        )
        cost_rows.append({"transaction_cost_bps": bps, **metrics})
    pd.DataFrame(cost_rows).to_csv(output_dir / "cost_sensitivity.csv", index=False)

    universe_rows, universe_summary = _universe_sensitivity(
        close,
        opens,
        config,
        evaluation_start,
        cash_returns,
    )
    pd.DataFrame(universe_rows).to_csv(output_dir / "universe_sensitivity.csv", index=False)

    development_returns = pd.concat(
        {name: result.returns for name, result in variants.items()}, axis=1
    ).loc[evaluation_start : config.validation.development_end]
    pbo = _approximate_pbo(development_returns)
    champion = primary_results["classic_12m_dual_momentum"].returns.loc[evaluation_start:]
    benchmark = primary_results["buy_hold_spy"].returns.reindex(champion.index)
    bootstrap = _paired_block_bootstrap(champion, benchmark)
    summary = {
        "parameter_variants": len(variants),
        "selection_period": f"{evaluation_start.date()} through {config.validation.development_end}",
        "approximate_pbo": pbo,
        "paired_block_bootstrap": bootstrap,
        "universe_sensitivity": universe_summary,
        "interpretation": (
            "PBO is diagnostic because the variant family is small and correlated. "
            "Bootstrap intervals quantify historical sampling uncertainty, not future guarantees."
        ),
    }
    (output_dir / "validation_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )


def _universe_sensitivity(
    close: pd.DataFrame,
    opens: pd.DataFrame,
    config: AppConfig,
    evaluation_start: pd.Timestamp,
    cash_returns: pd.Series,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    all_tickers = config.data.tickers
    sector_tickers = {ticker for ticker in all_tickers if ticker.startswith("XL")}
    named: dict[str, tuple[str, ...]] = {
        "all_assets": all_tickers,
        "no_qqq_or_xlk": tuple(t for t in all_tickers if t not in {"QQQ", "XLK"}),
        "no_sector_etfs": tuple(t for t in all_tickers if t not in sector_tickers),
        "broad_asset_classes": tuple(
            t for t in ("SPY", "IWM", "MDY", "EFA", "EEM", "GLD", "IEF") if t in all_tickers
        ),
    }
    named.update(
        {
            f"leave_out_{ticker.lower()}": tuple(t for t in all_tickers if t != ticker)
            for ticker in all_tickers
        }
    )
    rows: list[dict[str, object]] = []
    full_metrics: dict[str, dict[str, float | str]] = {}
    all_weights: pd.DataFrame | None = None
    for name, tickers in named.items():
        weights = classic_dual_momentum_weights(
            close,
            tickers,
            top_n=min(config.strategy.top_n, len(tickers)),
        )
        if name == "all_assets":
            all_weights = weights
        result = run_backtest(
            name,
            opens,
            weights,
            config.execution,
            cash_returns=cash_returns,
        )
        for split, start, end in (
            ("full", evaluation_start, None),
            ("validation", config.validation.validation_start, config.validation.validation_end),
            ("recent_diagnostic", config.validation.recent_diagnostic_start, None),
        ):
            effective_start = max(pd.Timestamp(start), evaluation_start)
            metrics = performance_metrics(
                result.returns.loc[effective_start:end],
                result.equity.loc[effective_start:end],
                result.turnover.loc[effective_start:end],
                result.transaction_costs.loc[effective_start:end],
            )
            rows.append(
                {
                    "universe_variant": name,
                    "excluded_ticker": (
                        name.removeprefix("leave_out_").upper()
                        if name.startswith("leave_out_")
                        else None
                    ),
                    "ticker_count": len(tickers),
                    "split": split,
                    **metrics,
                }
            )
            if split == "full":
                full_metrics[name] = metrics

    assert all_weights is not None
    tech_proxies = [ticker for ticker in ("QQQ", "XLK") if ticker in all_weights]
    tech_exposure = all_weights.loc[evaluation_start:, tech_proxies].sum(axis=1)
    baseline_cagr = float(full_metrics["all_assets"]["cagr"])
    leave_one_out_cagrs = [
        float(metrics["cagr"])
        for name, metrics in full_metrics.items()
        if name.startswith("leave_out_")
    ]
    return rows, {
        "purpose": (
            "diagnose dependence on overlapping technology proxies and today's surviving ETF universe; "
            "these post-selection diagnostics do not create a new holdout"
        ),
        "champion_mean_qqq_xlk_weight": float(tech_exposure.mean()),
        "champion_max_qqq_xlk_weight": float(tech_exposure.max()),
        "no_qqq_or_xlk_cagr": float(full_metrics["no_qqq_or_xlk"]["cagr"]),
        "no_qqq_or_xlk_cagr_delta": float(full_metrics["no_qqq_or_xlk"]["cagr"] - baseline_cagr),
        "no_sector_etfs_cagr": float(full_metrics["no_sector_etfs"]["cagr"]),
        "broad_asset_classes_cagr": float(full_metrics["broad_asset_classes"]["cagr"]),
        "leave_one_out_cagr_min": min(leave_one_out_cagrs),
        "leave_one_out_cagr_max": max(leave_one_out_cagrs),
        "leave_one_out_variants": len(leave_one_out_cagrs),
    }


def _approximate_pbo(returns: pd.DataFrame, blocks: int = 8) -> dict[str, float | int]:
    index_partitions = np.array_split(np.arange(len(returns)), blocks)
    partitions = [returns.iloc[positions] for positions in index_partitions if len(positions)]
    below_median = 0
    selections = 0
    for train_ids in itertools.combinations(range(len(partitions)), len(partitions) // 2):
        test_ids = [idx for idx in range(len(partitions)) if idx not in train_ids]
        train = pd.concat([partitions[idx] for idx in train_ids])
        test = pd.concat([partitions[idx] for idx in test_ids])
        train_score = _sharpe_columns(train)
        selected = str(train_score.idxmax())
        test_score = _sharpe_columns(test).sort_values(ascending=False)
        rank = int(test_score.index.get_loc(selected)) + 1
        below_median += int(rank > len(test_score) / 2)
        selections += 1
    return {
        "combinatorial_splits": selections,
        "probability_selected_variant_below_oos_median": below_median / selections,
    }


def _sharpe_columns(returns: pd.DataFrame) -> pd.Series:
    volatility = returns.std(ddof=1).replace(0.0, np.nan)
    return (returns.mean() / volatility * np.sqrt(252)).fillna(-np.inf)


def _paired_block_bootstrap(
    champion: pd.Series,
    benchmark: pd.Series,
    *,
    block_days: int = 21,
    samples: int = 2000,
) -> dict[str, float | int]:
    paired = pd.concat(
        [champion.rename("champion"), benchmark.rename("benchmark")], axis=1
    ).dropna()
    spread = paired["champion"] - paired["benchmark"]
    rng = np.random.default_rng(20261006)
    starts = np.arange(0, max(len(spread) - block_days + 1, 1))
    estimates = np.empty(samples)
    blocks_needed = int(np.ceil(len(spread) / block_days))
    values = spread.to_numpy()
    for sample in range(samples):
        chosen = rng.choice(starts, size=blocks_needed, replace=True)
        resampled = np.concatenate([values[start : start + block_days] for start in chosen])[
            : len(values)
        ]
        estimates[sample] = resampled.mean() * 252
    low, median, high = np.quantile(estimates, [0.025, 0.5, 0.975])
    return {
        "samples": samples,
        "block_days": block_days,
        "annualized_mean_return_difference_median": float(median),
        "annualized_mean_return_difference_ci_2_5": float(low),
        "annualized_mean_return_difference_ci_97_5": float(high),
        "probability_champion_mean_return_exceeds_spy": float((estimates > 0).mean()),
    }
