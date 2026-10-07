from __future__ import annotations

import pandas as pd
import pytest

from swing_trader.data import MarketDataError
from swing_trader.events import build_earnings_event_flags, parse_alpha_earnings_calendar


def test_alpha_earnings_parser_rejects_malformed_quota_style_row() -> None:
    payload = (
        "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\r\n"
        "I,n,f,o,r,m,a\r\n"
    )

    with pytest.raises(MarketDataError, match="malformed rows"):
        parse_alpha_earnings_calendar(payload)


def test_earnings_flags_distinguish_pre_and_post_market_timing() -> None:
    sessions = pd.bdate_range("2025-01-06", periods=6)
    calendar = pd.DataFrame(
        {
            "symbol": ["PRE", "POST"],
            "report_date": [sessions[3], sessions[3]],
            "time_of_day": ["pre-market", "after_close"],
        }
    )

    flags = build_earnings_event_flags(calendar, sessions, lead_sessions=2, cooling_sessions=1)
    pre_blocked = flags.loc[(flags["ticker"] == "PRE") & flags["entry_blocked"], "date"]
    post_blocked = flags.loc[(flags["ticker"] == "POST") & flags["entry_blocked"], "date"]

    assert pre_blocked.tolist() == [sessions[1], sessions[2]]
    assert post_blocked.tolist() == [sessions[1], sessions[2], sessions[3]]
    assert flags.loc[
        (flags["ticker"] == "POST") & flags["post_earnings_window"], "date"
    ].tolist() == [sessions[3], sessions[4]]
