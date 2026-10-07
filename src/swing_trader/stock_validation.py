from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class WalkForwardSelection:
    folds: pd.DataFrame
    selected_returns: pd.Series
    selected_variants: pd.Series


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
