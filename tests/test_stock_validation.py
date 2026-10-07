from __future__ import annotations

import pandas as pd

from swing_trader.stock_validation import expanding_walk_forward_selection


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
