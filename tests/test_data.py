from __future__ import annotations

import pandas as pd
import pytest

from swing_trader.data import MarketDataError, validate_prices


def test_rejects_impossible_high_low() -> None:
    dates = pd.bdate_range("2024-01-01", periods=300)
    values = {}
    for field, value in {"Open": 10.0, "High": 9.0, "Low": 11.0, "Close": 10.0, "Volume": 100.0}.items():
        values[(field, "A")] = value
    frame = pd.DataFrame(values, index=dates)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["field", "ticker"])
    with pytest.raises(MarketDataError, match="High below Low"):
        validate_prices(frame, ("A",))
