from __future__ import annotations

import pandas as pd

from swing_trader.stock_comparators import (
    classic_stock_momentum_weights,
    point_in_time_equal_weight_weights,
)


def test_equal_weight_comparator_uses_historical_membership() -> None:
    dates = pd.bdate_range("2025-01-02", "2025-03-31")
    membership = pd.DataFrame({"A": True, "B": False}, index=dates)
    membership.loc[dates >= "2025-02-01", "B"] = True

    weights = point_in_time_equal_weight_weights(membership)

    january_end = dates[dates.to_period("M") == pd.Period("2025-01")][-1]
    february_end = dates[dates.to_period("M") == pd.Period("2025-02")][-1]
    assert weights.loc[january_end, ["A", "B"]].tolist() == [1.0, 0.0]
    assert weights.loc[february_end, ["A", "B"]].tolist() == [0.5, 0.5]


def test_monthly_comparator_exits_a_removed_member_without_waiting_for_month_end() -> None:
    dates = pd.bdate_range("2025-01-02", "2025-03-31")
    membership = pd.DataFrame({"A": True, "B": True}, index=dates)
    removed = pd.Timestamp("2025-02-14")
    membership.loc[dates >= removed, "B"] = False

    weights = point_in_time_equal_weight_weights(membership)

    assert weights.loc[removed, "B"] == 0.0
    assert weights.loc[removed, "A"] == 0.5


def test_classic_stock_momentum_cannot_select_nonmember() -> None:
    dates = pd.bdate_range("2024-01-02", periods=320)
    close = pd.DataFrame(
        {
            "A": [100.0 + value for value in range(len(dates))],
            "B": [100.0 + 2.0 * value for value in range(len(dates))],
        },
        index=dates,
    )
    membership = pd.DataFrame({"A": True, "B": False}, index=dates)

    weights = classic_stock_momentum_weights(close, membership, top_n=1)

    assert weights.iloc[-1]["A"] == 1.0
    assert weights.iloc[-1]["B"] == 0.0
