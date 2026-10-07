from __future__ import annotations

import numpy as np
import pandas as pd

from swing_trader.backtest import run_backtest
from swing_trader.config import ExecutionConfig
from swing_trader.stock_validation import (
    approximate_combinatorial_pbo,
    asset_pnl_concentration,
    expanding_walk_forward_selection,
    paired_stationary_bootstrap,
    stationary_bootstrap_family_validation,
)


def test_walk_forward_selection_never_uses_test_year_to_choose() -> None:
    dates = pd.bdate_range("2018-01-02", "2025-12-31")
    variants = pd.DataFrame(
        {
            "steady": 0.0004,
            "future_winner": 0.0001,
        },
        index=dates,
    )
    variants.loc[variants.index.year >= 2024, "future_winner"] = 0.01
    benchmark = pd.Series(0.0, index=dates)

    result = expanding_walk_forward_selection(
        variants,
        benchmark,
        first_test_year=2023,
        last_test_year=2025,
        minimum_train_sessions=252,
    )

    assert result.folds.loc[0, "selected_variant"] == "steady"
    assert result.selected_variants.loc["2023"].eq("steady").all()


def test_future_revision_cannot_change_prior_walk_forward_fold() -> None:
    dates = pd.bdate_range("2018-01-02", "2025-12-31")
    variants = pd.DataFrame({"A": 0.0005, "B": 0.0002}, index=dates)
    benchmark = pd.Series(0.0, index=dates)
    original = expanding_walk_forward_selection(
        variants,
        benchmark,
        first_test_year=2023,
        last_test_year=2025,
        minimum_train_sessions=252,
    )
    revised = variants.copy()
    revised.loc[revised.index.year == 2025, "B"] = 0.10
    changed = expanding_walk_forward_selection(
        revised,
        benchmark,
        first_test_year=2023,
        last_test_year=2025,
        minimum_train_sessions=252,
    )

    pd.testing.assert_series_equal(
        original.selected_returns.loc["2023"],
        changed.selected_returns.loc["2023"],
    )


def test_stationary_bootstrap_controls_the_complete_correlated_family() -> None:
    dates = pd.bdate_range("2010-01-04", periods=1_000)
    rng = np.random.default_rng(7)
    benchmark = pd.Series(rng.normal(0.0002, 0.01, len(dates)), index=dates)
    common_noise = rng.normal(0.0, 0.0005, len(dates))
    variants = pd.DataFrame(
        {
            "durable_edge": benchmark + 0.001 + common_noise,
            "no_edge": benchmark + common_noise,
        },
        index=dates,
    )

    result = stationary_bootstrap_family_validation(
        variants,
        benchmark,
        mean_block_sessions=10,
        samples=499,
        seed=123,
    )

    assert result.summary["variants"] == 2
    assert result.summary["family_wide_best_variant_p_value"] <= 0.01
    assert bool(result.variants.loc["durable_edge", "fdr_reject_positive_excess"])
    assert not bool(result.variants.loc["no_edge", "fdr_reject_positive_excess"])
    assert (
        result.variants.loc["durable_edge", "benjamini_yekutieli_q_value"]
        >= result.variants.loc["durable_edge", "one_sided_bootstrap_p_value"]
    )


def test_paired_stationary_bootstrap_reports_uncertainty_not_just_point_estimate() -> None:
    dates = pd.bdate_range("2015-01-02", periods=750)
    benchmark = pd.Series(0.0001, index=dates)
    cycle = np.sin(np.arange(len(dates)) / 10.0) * 0.0005
    candidate = benchmark + pd.Series(0.0004 + cycle, index=dates)

    result = paired_stationary_bootstrap(
        candidate,
        benchmark,
        mean_block_sessions=21,
        samples=499,
        seed=456,
    )

    assert result["ci_2_5"] < result["observed_annualized_mean_excess_return"]
    assert result["ci_97_5"] > result["observed_annualized_mean_excess_return"]
    assert result["probability_resampled_mean_excess_is_positive"] == 1.0


def test_approximate_pbo_is_explicitly_not_estimable_for_one_variant() -> None:
    dates = pd.bdate_range("2020-01-02", periods=100)

    result = approximate_combinatorial_pbo(pd.DataFrame({"only": 0.001}, index=dates))

    assert result == {
        "status": "not_estimable",
        "reason": "fewer_than_two_variants",
        "variants": 1,
        "sessions": 100,
    }


def test_approximate_pbo_returns_a_bounded_family_diagnostic() -> None:
    dates = pd.bdate_range("2010-01-04", periods=800)
    wave = np.sin(np.arange(len(dates)) / 25.0) * 0.001
    variants = pd.DataFrame(
        {
            "A": 0.0002 + wave,
            "B": 0.0002 - wave,
            "C": 0.0001,
        },
        index=dates,
    )

    result = approximate_combinatorial_pbo(variants, partitions=8)

    assert result["status"] == "estimated"
    assert result["combinatorial_splits"] == 70
    assert 0.0 <= result["probability_selected_variant_below_oos_median"] <= 1.0
    assert 0.0 <= result["median_selected_variant_oos_rank_percentile"] <= 1.0


def test_asset_concentration_uses_actual_held_weights_and_open_returns() -> None:
    dates = pd.bdate_range("2025-01-02", periods=4)
    opens = pd.DataFrame(
        {
            "A": [100.0, 110.0, 121.0, 133.1],
            "B": [100.0, 100.0, 100.0, 100.0],
        },
        index=dates,
    )
    targets = pd.DataFrame(0.0, index=dates, columns=opens.columns)
    targets.loc[:, "A"] = 1.0
    result = run_backtest(
        "candidate",
        opens,
        targets,
        ExecutionConfig(
            initial_capital=100_000.0,
            transaction_cost_bps=0.0,
            minimum_trade_weight=0.0,
        ),
    )

    concentration = asset_pnl_concentration(
        result,
        opens,
        initial_capital=100_000.0,
    )

    assert concentration.assets.index.tolist() == ["A"]
    assert concentration.assets.loc["A", "held_sessions"] == 2
    assert concentration.assets.loc["A", "positive_gross_pnl_share"] == 1.0
    assert concentration.summary["largest_positive_contributor"] == "A"
    assert concentration.summary["top_1_positive_pnl_share"] == 1.0
