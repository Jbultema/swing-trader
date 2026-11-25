"""Planned NYSE month ends without partial-file or future-closure knowledge."""

from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


@lru_cache(maxsize=16)
def planned_month_ends(start_year: int, end_year: int) -> pd.DatetimeIndex:
    start, end = f"{start_year}-01-01", f"{end_year}-12-31"
    calendar = xcals.get_calendar("XNYS", start=start, end=end)
    holidays = calendar.regular_holidays.holidays(start, end)
    dates = pd.date_range(start, end, freq=pd.offsets.CustomBusinessDay(holidays=holidays))
    return pd.DatetimeIndex(
        pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().to_numpy()
    )


def completed_session_at(session: str, captured_at: str) -> bool:
    """Require an explicit UTC offset and the actual close plus feed buffer."""
    try:
        day = pd.Timestamp(session)
        captured = pd.Timestamp(captured_at)
        if (
            pd.isna(day) or pd.isna(captured) or day.tzinfo is not None
            or day != day.normalize() or captured.tzinfo is None
        ):
            return False
        calendar = xcals.get_calendar(
            "XNYS", start=day - pd.Timedelta(days=7), end=day + pd.Timedelta(days=7)
        )
        if not calendar.is_session(day):
            return False
        return captured >= calendar.session_close(day) + pd.Timedelta(minutes=15)
    except (TypeError, ValueError, KeyError):
        return False
