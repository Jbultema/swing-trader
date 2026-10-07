from __future__ import annotations

import pandas as pd

from swing_trader.stock_signals import (
    ExitPolicy,
    StockFeatureConfig,
    build_stock_features,
    exit_reasons,
    rank_stock_candidates,
)


def test_stock_features_are_causal_and_respect_point_in_time_membership() -> None:
    prices, membership = _panel()
    config = StockFeatureConfig(minimum_price=1.0, minimum_median_dollar_volume=1.0)
    original = build_stock_features(prices, membership, config)
    changed = prices.copy()
    changed.loc[changed.index[-1], ("Close", "A")] *= 5.0
    revised = build_stock_features(changed, membership, config)

    prior_date = prices.index[-2]
    pd.testing.assert_series_equal(original.loc[(prior_date, "A")], revised.loc[(prior_date, "A")])
    assert bool(original.loc[(prices.index[-1], "B"), "eligible"]) is False


def test_candidate_ranking_requires_eligibility_and_positive_trend() -> None:
    prices, membership = _panel()
    features = build_stock_features(
        prices,
        membership,
        StockFeatureConfig(minimum_price=1.0, minimum_median_dollar_volume=1.0),
    )
    ranked = rank_stock_candidates(features, "smooth_momentum", prices.index[-1], top_n=5)

    assert ranked.index.tolist() == ["A"]
    assert ranked.iloc[0]["candidate_rank"] == 1


def test_exit_hierarchy_reports_multiple_independent_reasons() -> None:
    row = pd.Series(
        {
            "close": 90.0,
            "atr_fraction_14d": 0.02,
            "trend_positive": False,
            "return_21d": -0.01,
        }
    )
    reasons = exit_reasons(
        row,
        entry_price=100.0,
        high_watermark=110.0,
        holding_sessions=70,
        cross_section_rank=25,
        entry_top_n=10,
        policy=ExitPolicy(),
    )

    assert reasons == [
        "hard_loss_limit",
        "atr_trailing_exit",
        "trend_broken",
        "short_momentum_non_positive",
        "rank_decay",
        "maximum_holding_period",
    ]


def _panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2024-01-02", periods=320)
    close = pd.DataFrame(
        {
            "A": [100.0 + 0.5 * value for value in range(len(dates))],
            "B": [300.0 - 0.2 * value for value in range(len(dates))],
        },
        index=dates,
    )
    fields = {
        "Close": close,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Volume": pd.DataFrame(1_000_000.0, index=dates, columns=close.columns),
    }
    prices = pd.concat(fields, axis=1)
    prices.columns.names = ["field", "ticker"]
    membership = pd.DataFrame(True, index=dates, columns=close.columns)
    membership.loc[dates[-1], "B"] = False
    return prices, membership
