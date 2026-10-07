from __future__ import annotations

import pandas as pd
import pytest

from swing_trader.cash import fred_3m_cash_returns


def test_fred_cash_return_uses_prior_known_rate_and_calendar_days() -> None:
    raw = pd.DataFrame(
        {
            "DATE": ["2025-01-02", "2025-01-03", "2025-01-06"],
            "DGS3MO": [5.0, 9.0, 8.0],
        }
    )

    returns = fred_3m_cash_returns(raw)

    expected_friday_to_monday = (1.0 + 0.05) ** (3.0 / 365.0) - 1.0
    assert pd.isna(returns.loc["2025-01-02"])
    assert returns.loc["2025-01-03"] == pytest.approx(expected_friday_to_monday)
