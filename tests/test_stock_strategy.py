from __future__ import annotations

import pandas as pd

from swing_trader.stock_signals import ExitPolicy
from swing_trader.stock_strategy import build_stock_strategy_plan


def test_stock_plan_enters_next_open_and_exits_after_gap_loss() -> None:
    dates = pd.bdate_range("2025-01-02", periods=3)
    features = _features(dates, closes=[100.0, 100.0, 101.0])
    opens = pd.DataFrame({"A": [100.0, 110.0, 101.0]}, index=dates)

    plan = build_stock_strategy_plan(
        features,
        opens,
        "short_volume",
        maximum_positions=1,
        maximum_position_weight=1.0,
        policy=ExitPolicy(hard_stop_fraction=0.08, atr_multiple=100.0),
    )

    assert plan.target_weights["A"].tolist() == [1.0, 0.0, 1.0]
    sell = plan.decisions.query("date == @dates[1] and ticker == 'A' and action == 'SELL'").iloc[0]
    assert sell["entry_price"] == 110.0
    assert "hard_loss_limit" in sell["reasons"]


def test_stock_plan_is_unchanged_before_future_feature_revision() -> None:
    dates = pd.bdate_range("2025-01-02", periods=3)
    features = _features(dates, closes=[100.0, 101.0, 102.0])
    opens = pd.DataFrame({"A": [100.0, 101.0, 102.0]}, index=dates)
    original = build_stock_strategy_plan(features, opens, "short_volume", maximum_positions=1)
    changed = features.copy()
    changed.loc[(dates[-1], "A"), "score_short_volume"] = -100.0
    revised = build_stock_strategy_plan(changed, opens, "short_volume", maximum_positions=1)

    pd.testing.assert_series_equal(
        original.target_weights.loc[dates[:-1], "A"],
        revised.target_weights.loc[dates[:-1], "A"],
    )


def test_stock_plan_explains_scheduled_earnings_entry_blackout() -> None:
    dates = pd.bdate_range("2025-01-02", periods=3)
    index = pd.MultiIndex.from_product([dates, ["A", "B"]], names=["date", "ticker"])
    features = pd.DataFrame(
        {
            "close": [100.0, 90.0] * len(dates),
            "eligible": True,
            "trend_positive": True,
            "return_21d": 0.05,
            "atr_fraction_14d": 0.01,
            "score_short_volume": [1.0, 0.5] * len(dates),
            "score_smooth_momentum": [1.0, 0.5] * len(dates),
            "score_volume_breakout": [1.0, 0.5] * len(dates),
        },
        index=index,
    )
    opens = pd.DataFrame({"A": 100.0, "B": 90.0}, index=dates)
    blackout = pd.DataFrame(False, index=dates, columns=["A", "B"])
    blackout.loc[dates[0], "A"] = True

    plan = build_stock_strategy_plan(
        features,
        opens,
        "short_volume",
        maximum_positions=1,
        entry_blackout=blackout,
    )

    assert plan.target_weights.loc[dates[0], ["A", "B"]].tolist() == [0.0, 0.1]
    skipped = plan.decisions.query("date == @dates[0] and ticker == 'A'").iloc[0]
    assert skipped["action"] == "SKIP"
    assert skipped["reasons"] == ["scheduled_earnings_entry_blackout"]


def test_earnings_blackout_does_not_force_or_mislabel_an_existing_position() -> None:
    dates = pd.bdate_range("2025-01-02", periods=3)
    features = _features(dates, closes=[100.0, 101.0, 102.0])
    opens = pd.DataFrame({"A": [100.0, 101.0, 102.0]}, index=dates)
    blackout = pd.DataFrame({"A": [False, True, False]}, index=dates)

    plan = build_stock_strategy_plan(
        features,
        opens,
        "short_volume",
        maximum_positions=1,
        maximum_position_weight=1.0,
        entry_blackout=blackout,
    )

    day = plan.decisions.query("date == @dates[1] and ticker == 'A'")
    assert day["action"].tolist() == ["HOLD"]
    assert day.iloc[0]["reasons"] == ["retained_signal"]


def _features(dates: pd.DatetimeIndex, closes: list[float]) -> pd.DataFrame:
    index = pd.MultiIndex.from_product([dates, ["A"]], names=["date", "ticker"])
    return pd.DataFrame(
        {
            "close": closes,
            "eligible": True,
            "trend_positive": True,
            "return_21d": 0.05,
            "atr_fraction_14d": 0.01,
            "score_short_volume": 1.0,
            "score_smooth_momentum": 1.0,
            "score_volume_breakout": 1.0,
        },
        index=index,
    )
