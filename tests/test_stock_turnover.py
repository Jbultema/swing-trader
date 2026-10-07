from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from swing_trader.stock_turnover import (
    ShareTurnoverSignalConfig,
    build_share_turnover_features,
    rank_share_turnover_candidates,
)


def test_share_turnover_signal_uses_independent_quintiles_and_skips_recent_sessions() -> None:
    dates = pd.bdate_range(end="2026-10-07", periods=30)
    tickers = tuple(f"T{i:03d}" for i in range(100))
    prices = _prices(dates, tickers)
    shares = _shares(tickers, datetime(2026, 10, 7, 22, tzinfo=UTC))
    last_three = dates[-3:]
    prices.loc[last_three, ("Close", "T000")] = [500.0, 700.0, 900.0]
    prices.loc[last_three, ("Volume", "T000")] = 100_000_000.0

    features = build_share_turnover_features(prices, shares, dates[-1])
    selected = rank_share_turnover_candidates(features, top_n=10)

    assert selected["ticker"].tolist() == [f"T{i:03d}" for i in range(99, 89, -1)]
    assert "T000" not in selected["ticker"].tolist()
    assert features.loc["T000", "formation_return_t20_t3"] == 0.0
    assert features.loc["T000", "formation_volume_t20_t3"] == 18_000_000.0
    assert int(features["winner_quintile"].sum()) == 20
    assert int(features["high_turnover_quintile"].sum()) == 20
    assert selected["winner_quintile"].all()
    assert selected["high_turnover_quintile"].all()


def test_share_turnover_signal_excludes_stale_shares() -> None:
    dates = pd.bdate_range(end="2026-10-07", periods=30)
    tickers = ("A", "B")
    prices = _prices(dates, tickers)
    captured = datetime(2026, 10, 7, 22, tzinfo=UTC)
    shares = _shares(tickers, captured)
    shares.loc[shares["ticker"] == "B", "provider_observation_date"] = pd.Timestamp(
        "2026-01-01"
    )

    features = build_share_turnover_features(
        prices,
        shares,
        dates[-1],
        config=ShareTurnoverSignalConfig(quantile_threshold=0.50),
    )

    assert bool(features.loc["A", "signal_eligible"])
    assert not bool(features.loc["B", "signal_eligible"])
    assert pd.isna(features.loc["B", "shares_outstanding_at_capture"])


def _prices(dates: pd.DatetimeIndex, tickers: tuple[str, ...]) -> pd.DataFrame:
    close = pd.DataFrame(100.0, index=dates, columns=tickers)
    volume = pd.DataFrame(index=dates, columns=tickers, dtype=float)
    formation_end = -4
    for position, ticker in enumerate(tickers):
        close.loc[dates[8:formation_end + 1], ticker] = np.linspace(
            100.0,
            100.0 + position,
            len(dates[8:formation_end + 1]),
        )
        close.loc[dates[formation_end + 1 :], ticker] = close.loc[
            dates[formation_end], ticker
        ]
        volume.loc[:, ticker] = 1_000_000.0 + position * 100_000.0
    frame = pd.concat(
        {
            "Open": close,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": volume,
        },
        axis=1,
    )
    frame.columns.names = ["field", "ticker"]
    return frame


def _shares(tickers: tuple[str, ...], captured: datetime) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": tickers,
            "shares_outstanding": pd.array([1_000_000] * len(tickers), dtype="Int64"),
            "provider_observation_date": pd.to_datetime(["2026-10-01"] * len(tickers)),
            "captured_at_utc": [captured.isoformat()] * len(tickers),
            "source_status": ["available"] * len(tickers),
        }
    )
