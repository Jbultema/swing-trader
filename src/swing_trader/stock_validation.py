from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from swing_trader.backtest import BacktestResult


@dataclass(frozen=True)
class WalkForwardSelection:
    folds: pd.DataFrame
    selected_returns: pd.Series
    selected_variants: pd.Series


@dataclass(frozen=True)
class FamilyBootstrapValidation:
    """Dependence-aware tests for a complete, preregistered strategy family."""

    variants: pd.DataFrame
    summary: dict[str, object]


@dataclass(frozen=True)
class AssetConcentrationValidation:
    """Additive gross P&L attribution for winner-concentration diagnostics."""

    assets: pd.DataFrame
    summary: dict[str, object]


def asset_pnl_concentration(
    result: BacktestResult,
    adjusted_open: pd.DataFrame,
    *,
    initial_capital: float,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    terminal_return_overrides: pd.DataFrame | None = None,
) -> AssetConcentrationValidation:
    """Attribute gross dollar P&L without converting winners into a new model.

    The diagnostic uses the weights actually held by the backtest and the same
    open-to-open/terminal return semantics. It is descriptive and deliberately
    ignores current sector or narrative labels that would create hindsight bias.
    """
    if initial_capital <= 0.0:
        raise ValueError("initial_capital must be positive.")
    prices = adjusted_open.sort_index().astype(float)
    interval_returns = prices.shift(-1).div(prices).sub(1.0)
    if terminal_return_overrides is not None:
        overrides = terminal_return_overrides.reindex(
            index=prices.index, columns=prices.columns
        ).astype(float)
        interval_returns = interval_returns.combine_first(overrides)
    weights = result.weights.reindex(columns=prices.columns).loc[start:end]
    returns = interval_returns.reindex(index=weights.index, columns=weights.columns)
    missing = weights.ne(0.0) & returns.isna()
    if missing.any(axis=None):
        pairs = list(missing.stack().loc[lambda values: values].index[:10])
        raise ValueError(f"Held asset returns are missing from concentration analysis: {pairs}")

    pre_return_equity = result.equity.shift(1).reindex(weights.index)
    if len(result.equity) and result.equity.index[0] in pre_return_equity.index:
        pre_return_equity.loc[result.equity.index[0]] = initial_capital
    if pre_return_equity.isna().any():
        raise ValueError("Pre-return equity is unavailable for concentration analysis.")
    contributions = weights.mul(returns.fillna(0.0)).mul(pre_return_equity, axis=0)
    gross_pnl = contributions.sum(axis=0)
    positive = gross_pnl.clip(lower=0.0)
    positive_total = float(positive.sum())
    positive_share = (
        positive.div(positive_total)
        if positive_total > 0.0
        else pd.Series(0.0, index=positive.index)
    )
    assets = pd.DataFrame(
        {
            "gross_dollar_pnl_contribution": gross_pnl,
            "positive_gross_pnl_share": positive_share,
            "average_portfolio_weight": weights.mean(axis=0),
            "maximum_portfolio_weight": weights.max(axis=0),
            "held_sessions": weights.gt(0.0).sum(axis=0),
        }
    )
    assets.index.name = "ticker"
    assets = assets.loc[assets["held_sessions"] > 0].sort_values(
        "gross_dollar_pnl_contribution", ascending=False
    )
    shares = assets["positive_gross_pnl_share"].to_numpy(dtype=float)
    summary: dict[str, object] = {
        "status": "estimated" if positive_total > 0.0 else "no_positive_stock_pnl",
        "start": str(weights.index.min().date()) if len(weights) else None,
        "end": str(weights.index.max().date()) if len(weights) else None,
        "sessions": len(weights),
        "tickers_held": len(assets),
        "positive_contributing_tickers": int((assets["gross_dollar_pnl_contribution"] > 0.0).sum()),
        "gross_stock_pnl": float(gross_pnl.sum()),
        "top_1_positive_pnl_share": float(shares[:1].sum()),
        "top_5_positive_pnl_share": float(shares[:5].sum()),
        "top_10_positive_pnl_share": float(shares[:10].sum()),
        "positive_pnl_herfindahl": float(np.square(shares).sum()),
        "largest_positive_contributor": str(assets.index[0])
        if len(assets) and positive_total > 0
        else None,
        "interpretation": (
            "Descriptive gross P&L concentration from actually held tickers. Current sector or "
            "AI labels are intentionally not projected backward."
        ),
    }
    return AssetConcentrationValidation(assets=assets, summary=summary)


def stationary_bootstrap_family_validation(
    variant_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
    *,
    mean_block_sessions: int = 21,
    samples: int = 2_000,
    seed: int = 20261007,
    fdr_level: float = 0.05,
) -> FamilyBootstrapValidation:
    """Test net excess returns while retaining serial and cross-strategy dependence.

    A common stationary-bootstrap index is applied to every strategy. Individual
    one-sided p-values use centered excess returns. The family p-value compares
    the best observed t-statistic with the bootstrap distribution of the maximum
    null t-statistic. Benjamini-Yekutieli q-values remain valid under arbitrary
    dependence, at the cost of being deliberately conservative.
    """
    variants, benchmark = _aligned_returns(variant_returns, benchmark_returns)
    if mean_block_sessions < 1:
        raise ValueError("mean_block_sessions must be positive.")
    if samples < 100:
        raise ValueError("At least 100 bootstrap samples are required.")
    if not 0.0 < fdr_level < 1.0:
        raise ValueError("fdr_level must be in (0, 1).")

    spreads = variants.sub(benchmark, axis=0)
    values = spreads.to_numpy(dtype=float)
    observed_mean = values.mean(axis=0)
    standard_errors = np.asarray(
        [
            newey_west_mean_standard_error(values[:, column], mean_block_sessions)
            for column in range(values.shape[1])
        ]
    )
    observed_t = _studentize(observed_mean, standard_errors)
    centered = values - observed_mean
    exceedances = np.zeros(values.shape[1], dtype=int)
    family_exceedances = 0
    rng = np.random.default_rng(seed)
    observed_max = float(np.max(observed_t))
    for _ in range(samples):
        indices = _stationary_bootstrap_indices(
            len(values), mean_block_sessions=mean_block_sessions, rng=rng
        )
        null_mean = centered[indices].mean(axis=0)
        null_t = _studentize(null_mean, standard_errors)
        exceedances += null_t >= observed_t
        family_exceedances += int(float(np.max(null_t)) >= observed_max)

    p_values = (exceedances + 1.0) / (samples + 1.0)
    q_values = _benjamini_yekutieli_adjust(p_values)
    table = pd.DataFrame(
        {
            "annualized_mean_excess_return": observed_mean * 252.0,
            "newey_west_t_statistic": observed_t,
            "one_sided_bootstrap_p_value": p_values,
            "benjamini_yekutieli_q_value": q_values,
            "fdr_reject_positive_excess": (q_values <= fdr_level) & (observed_mean > 0.0),
        },
        index=variants.columns,
    )
    table.index.name = "variant"
    best_position = int(np.argmax(observed_t))
    summary: dict[str, object] = {
        "method": "common_stationary_bootstrap_centered_excess_returns",
        "sessions": len(variants),
        "variants": len(variants.columns),
        "samples": samples,
        "mean_block_sessions": mean_block_sessions,
        "seed": seed,
        "fdr_method": "Benjamini-Yekutieli arbitrary-dependence adjustment",
        "fdr_level": fdr_level,
        "best_observed_variant": str(variants.columns[best_position]),
        "best_observed_t_statistic": float(observed_t[best_position]),
        "family_wide_best_variant_p_value": (family_exceedances + 1.0) / (samples + 1.0),
        "fdr_discoveries": int(table["fdr_reject_positive_excess"].sum()),
        "interpretation": (
            "Historical sampling and multiple-testing diagnostic only; it does not prove a "
            "persistent edge or authorize trading."
        ),
    }
    return FamilyBootstrapValidation(variants=table, summary=summary)


def paired_stationary_bootstrap(
    candidate_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    mean_block_sessions: int = 21,
    samples: int = 2_000,
    seed: int = 20261007,
) -> dict[str, float | int | str]:
    """Estimate uncertainty around annualized mean net excess return."""
    paired = pd.concat(
        [candidate_returns.rename("candidate"), benchmark_returns.rename("benchmark")], axis=1
    ).dropna()
    if len(paired) < 2:
        raise ValueError("At least two paired returns are required.")
    if mean_block_sessions < 1:
        raise ValueError("mean_block_sessions must be positive.")
    if samples < 100:
        raise ValueError("At least 100 bootstrap samples are required.")
    spread = paired["candidate"].sub(paired["benchmark"]).to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    estimates = np.empty(samples, dtype=float)
    for sample in range(samples):
        indices = _stationary_bootstrap_indices(
            len(spread), mean_block_sessions=mean_block_sessions, rng=rng
        )
        estimates[sample] = float(spread[indices].mean() * 252.0)
    low, median, high = np.quantile(estimates, [0.025, 0.5, 0.975])
    return {
        "method": "paired_stationary_bootstrap",
        "sessions": len(spread),
        "samples": samples,
        "mean_block_sessions": mean_block_sessions,
        "seed": seed,
        "observed_annualized_mean_excess_return": float(spread.mean() * 252.0),
        "bootstrap_median_annualized_mean_excess_return": float(median),
        "ci_2_5": float(low),
        "ci_97_5": float(high),
        "probability_resampled_mean_excess_is_positive": float((estimates > 0.0).mean()),
    }


def approximate_combinatorial_pbo(
    variant_returns: pd.DataFrame,
    *,
    partitions: int = 8,
) -> dict[str, float | int | str]:
    """Estimate how often an in-sample winner falls below the OOS median.

    This is a transparent CSCV-style diagnostic rather than a claim that a small,
    correlated variant family satisfies every asymptotic assumption of formal PBO.
    """
    clean = variant_returns.dropna().sort_index().astype(float)
    if clean.empty:
        raise ValueError("Variant returns must be non-empty.")
    if clean.shape[1] < 2:
        return {
            "status": "not_estimable",
            "reason": "fewer_than_two_variants",
            "variants": clean.shape[1],
            "sessions": len(clean),
        }
    usable = min(partitions, len(clean))
    if usable % 2:
        usable -= 1
    if usable < 4:
        return {
            "status": "not_estimable",
            "reason": "fewer_than_four_time_partitions",
            "variants": clean.shape[1],
            "sessions": len(clean),
        }
    blocks = [clean.iloc[positions] for positions in np.array_split(np.arange(len(clean)), usable)]
    below_median = 0
    selections = 0
    percentile_ranks: list[float] = []
    for training_ids in itertools.combinations(range(usable), usable // 2):
        test_ids = [index for index in range(usable) if index not in training_ids]
        training = pd.concat([blocks[index] for index in training_ids])
        test = pd.concat([blocks[index] for index in test_ids])
        training_score = _sharpe_columns(training)
        selected = str(training_score.sort_index().idxmax())
        test_score = _sharpe_columns(test).sort_values(ascending=False, kind="stable")
        rank = int(test_score.index.get_loc(selected)) + 1
        percentile = (rank - 0.5) / len(test_score)
        percentile_ranks.append(percentile)
        below_median += int(percentile > 0.5)
        selections += 1
    return {
        "status": "estimated",
        "partitions": usable,
        "combinatorial_splits": selections,
        "variants": clean.shape[1],
        "sessions": len(clean),
        "probability_selected_variant_below_oos_median": below_median / selections,
        "median_selected_variant_oos_rank_percentile": float(np.median(percentile_ranks)),
    }


def expanding_walk_forward_selection(
    variant_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
    *,
    first_test_year: int,
    last_test_year: int,
    minimum_train_sessions: int = 5 * 252,
) -> WalkForwardSelection:
    """Select only on expanding past data, then stitch untouched annual tests.

    The training score is the median annual compounded excess return. This limits
    the influence of a single extreme momentum year. Variant names break exact
    ties deterministically; no test-period result influences selection.
    """
    if variant_returns.empty or benchmark_returns.empty:
        raise ValueError("Variant and benchmark returns must be non-empty.")
    if first_test_year > last_test_year:
        raise ValueError("first_test_year must not exceed last_test_year.")
    if minimum_train_sessions < 2:
        raise ValueError("minimum_train_sessions must be at least two.")

    variants = variant_returns.sort_index().astype(float)
    benchmark = benchmark_returns.reindex(variants.index).astype(float)
    complete = variants.notna().all(axis=1) & benchmark.notna()
    variants = variants.loc[complete]
    benchmark = benchmark.loc[complete]
    fold_rows: list[dict[str, object]] = []
    selected_parts: list[pd.Series] = []
    choice_parts: list[pd.Series] = []

    for year in range(first_test_year, last_test_year + 1):
        test_mask = variants.index.year == year
        train_mask = variants.index < pd.Timestamp(year=year, month=1, day=1)
        train = variants.loc[train_mask]
        test = variants.loc[test_mask]
        if len(train) < minimum_train_sessions or test.empty:
            continue
        training_benchmark = benchmark.loc[train.index]
        scores = median_annual_excess_return(train, training_benchmark)
        selected = str(scores.sort_index().idxmax())
        selected_test = test[selected].rename("selected_walk_forward")
        test_benchmark = benchmark.loc[test.index]
        selected_parts.append(selected_test)
        choice_parts.append(pd.Series(selected, index=test.index, dtype="string"))
        strategy_total = float((1.0 + selected_test).prod() - 1.0)
        benchmark_total = float((1.0 + test_benchmark).prod() - 1.0)
        fold_rows.append(
            {
                "test_year": year,
                "train_start": str(train.index.min().date()),
                "train_end": str(train.index.max().date()),
                "train_sessions": len(train),
                "test_start": str(test.index.min().date()),
                "test_end": str(test.index.max().date()),
                "test_sessions": len(test),
                "selected_variant": selected,
                "training_median_annual_excess_return": float(scores[selected]),
                "test_strategy_total_return": strategy_total,
                "test_benchmark_total_return": benchmark_total,
                "test_excess_total_return": strategy_total - benchmark_total,
            }
        )

    if not fold_rows:
        raise ValueError("No walk-forward folds met the training and test requirements.")
    return WalkForwardSelection(
        folds=pd.DataFrame(fold_rows),
        selected_returns=pd.concat(selected_parts).sort_index(),
        selected_variants=pd.concat(choice_parts).sort_index().rename("selected_variant"),
    )


def median_annual_excess_return(
    variants: pd.DataFrame,
    benchmark: pd.Series,
) -> pd.Series:
    aligned = pd.concat([variants, benchmark.rename("__benchmark__")], axis=1).dropna()
    if aligned.empty:
        raise ValueError("No complete variant/benchmark returns are available for scoring.")
    variants = aligned.drop(columns="__benchmark__")
    benchmark = aligned["__benchmark__"]
    yearly_variant = variants.groupby(variants.index.year).agg(
        lambda values: (1.0 + values).prod() - 1.0
    )
    yearly_benchmark = benchmark.groupby(benchmark.index.year).agg(
        lambda values: (1.0 + values).prod() - 1.0
    )
    excess = yearly_variant.sub(yearly_benchmark, axis=0)
    return excess.median(axis=0)


def _aligned_returns(
    variant_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
) -> tuple[pd.DataFrame, pd.Series]:
    if variant_returns.empty or benchmark_returns.empty:
        raise ValueError("Variant and benchmark returns must be non-empty.")
    if variant_returns.columns.duplicated().any():
        raise ValueError("Variant names must be unique.")
    aligned = pd.concat(
        [variant_returns.astype(float), benchmark_returns.astype(float).rename("__benchmark__")],
        axis=1,
    ).dropna()
    if len(aligned) < 2:
        raise ValueError("At least two complete variant/benchmark sessions are required.")
    return aligned.drop(columns="__benchmark__"), aligned["__benchmark__"]


def _stationary_bootstrap_indices(
    observations: int,
    *,
    mean_block_sessions: int,
    rng: np.random.Generator,
) -> np.ndarray:
    if observations < 1:
        raise ValueError("At least one observation is required.")
    probability = min(1.0, 1.0 / mean_block_sessions)
    chunks: list[np.ndarray] = []
    remaining = observations
    while remaining:
        start = int(rng.integers(0, observations))
        length = min(int(rng.geometric(probability)), remaining)
        chunks.append((start + np.arange(length)) % observations)
        remaining -= length
    return np.concatenate(chunks).astype(int, copy=False)


def newey_west_mean_standard_error(values: np.ndarray, maximum_lag: int) -> float:
    """Estimate the standard error of a mean with Bartlett-weighted HAC lags."""
    clean = np.asarray(values, dtype=float)
    observations = len(clean)
    centered = clean - clean.mean()
    lag = min(maximum_lag, observations - 1)
    long_run_variance = float(centered @ centered / observations)
    for offset in range(1, lag + 1):
        covariance = float(centered[offset:] @ centered[:-offset] / observations)
        weight = 1.0 - offset / (lag + 1.0)
        long_run_variance += 2.0 * weight * covariance
    return float(np.sqrt(max(long_run_variance, 0.0) / observations))


def _studentize(means: np.ndarray, standard_errors: np.ndarray) -> np.ndarray:
    result = np.zeros_like(means, dtype=float)
    estimable = standard_errors > np.finfo(float).eps
    result[estimable] = means[estimable] / standard_errors[estimable]
    result[~estimable & (means > 0.0)] = np.inf
    result[~estimable & (means < 0.0)] = -np.inf
    return result


def _benjamini_yekutieli_adjust(p_values: np.ndarray) -> np.ndarray:
    values = np.asarray(p_values, dtype=float)
    if values.ndim != 1 or not len(values):
        raise ValueError("p_values must be a non-empty one-dimensional array.")
    if ((values < 0.0) | (values > 1.0) | ~np.isfinite(values)).any():
        raise ValueError("p_values must be finite values in [0, 1].")
    count = len(values)
    harmonic = float(np.sum(1.0 / np.arange(1, count + 1)))
    order = np.argsort(values, kind="stable")
    ranked = values[order]
    adjusted_ranked = ranked * count * harmonic / np.arange(1, count + 1)
    adjusted_ranked = np.minimum.accumulate(adjusted_ranked[::-1])[::-1]
    adjusted = np.empty(count, dtype=float)
    adjusted[order] = np.minimum(adjusted_ranked, 1.0)
    return adjusted


def _sharpe_columns(returns: pd.DataFrame) -> pd.Series:
    volatility = returns.std(ddof=1).replace(0.0, np.nan)
    return (returns.mean().div(volatility).mul(np.sqrt(252.0))).fillna(-np.inf)
