import pandas as pd
import pytest

from swing_trader.backtest import month_end_mask
from swing_trader.scheduling import completed_session_at


def test_good_friday_month_end_is_available_at_that_close():
    dates = pd.bdate_range("2024-03-01", "2024-03-28")
    assert month_end_mask(dates)[-1]


def test_partial_file_does_not_change_month_end_decisions():
    dates = pd.bdate_range("2024-01-02", "2026-11-25")
    full = month_end_mask(dates)
    for stop in (63, 150, 307, 410):
        assert (full[:stop] == month_end_mask(dates[:stop])).all()


@pytest.mark.parametrize("day,capture,expected", [
    ("2026-11-27", "2026-11-27T18:15:00Z", True),
    ("2026-11-27", "2026-11-27T18:14:59Z", False),
    ("2026-11-24", "2026-11-24T21:15:00Z", True),
    ("2026-11-24", "2026-11-24T18:30:00Z", False),
    ("2026-11-26", "2026-11-27T23:00:00Z", False),
    ("2026-11-24", "2026-11-25T23:00:00", False),
    ("2026-11-24", "NaT", False),
])
def test_completed_session_requires_real_session_and_explicit_timezone(day, capture, expected):
    assert completed_session_at(day, capture) is expected
