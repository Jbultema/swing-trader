from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from swing_trader.historical_identity import (
    HistoricalIdentityError,
    build_yahoo_identity_recovery,
    verify_yahoo_identity_recovery,
    write_yahoo_identity_recovery,
)


def _inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    calendar = pd.bdate_range("2016-01-04", "2023-05-12")
    membership = pd.DataFrame(
        [
            {"ticker": "A", "start": "2016-01-04", "end": None},
            {"ticker": "HFC", "start": "2018-06-18", "end": "2021-06-04"},
            {"ticker": "FRC", "start": "2019-01-02", "end": "2023-05-04"},
        ]
    )
    prices = pd.DataFrame({"date": calendar, "ticker": "A"})
    return membership, prices


def _downloader(
    tickers: list[str],
    *,
    start: str,
    end: str,
    **_kwargs: object,
) -> pd.DataFrame:
    ticker = tickers[0]
    dates = pd.bdate_range(start, end, inclusive="left")
    base = np.linspace(50.0, 60.0, len(dates))
    fields = {
        "Open": base,
        "High": base + 1.0,
        "Low": base - 1.0,
        "Close": base + 0.5,
        "Adj Close": base + 0.25,
        "Volume": np.full(len(dates), 1_000_000.0),
    }
    frame = pd.DataFrame(fields, index=dates)
    frame.columns = pd.MultiIndex.from_product(
        [frame.columns, [ticker]], names=["Price", "Ticker"]
    )
    return frame


def test_explicit_aliases_close_two_missing_tickers_without_leaking_future_rows() -> None:
    membership, prices = _inputs()

    result = build_yahoo_identity_recovery(
        membership,
        prices,
        downloader=_downloader,
        audit_start="2018-01-02",
    )

    assert result.original_coverage.missing_tickers == ("FRC", "HFC")
    assert result.supplemented_coverage.missing_tickers == ()
    assert result.supplemented_coverage.status == "passed"
    assert {rule.status for rule in result.rules} == {"mapping_coverage_passed"}
    assert {rule.pre_member_sessions for rule in result.rules} == {252}
    assert result.recovered.groupby("ticker")["date"].max().dt.date.astype(str).to_dict() == {
        "FRC": "2023-05-04",
        "HFC": "2021-06-04",
    }
    assert set(result.recovered["provider_ticker"]) == {"DINO", "FRCB"}


def test_identity_rule_fails_closed_when_membership_interval_changes() -> None:
    membership, prices = _inputs()
    membership.loc[membership["ticker"].eq("FRC"), "end"] = "2023-05-05"

    with pytest.raises(HistoricalIdentityError, match="no longer matches"):
        build_yahoo_identity_recovery(
            membership,
            prices,
            downloader=_downloader,
            audit_start="2018-01-02",
        )


def test_small_range_error_is_enumerated_and_conservatively_expanded() -> None:
    membership, prices = _inputs()

    def imperfect_downloader(
        tickers: list[str],
        *,
        start: str,
        end: str,
        **kwargs: object,
    ) -> pd.DataFrame:
        frame = _downloader(tickers, start=start, end=end, **kwargs)
        if tickers == ["FRCB"]:
            session = frame.index[10]
            frame.loc[session, ("High", "FRCB")] = (
                frame.loc[session, ("Close", "FRCB")] - 0.25
            )
        return frame

    result = build_yahoo_identity_recovery(
        membership,
        prices,
        downloader=imperfect_downloader,
        audit_start="2018-01-02",
    )
    frc = next(rule for rule in result.rules if rule.canonical_ticker == "FRC")
    repaired = result.recovered.loc[
        result.recovered["date"].eq(pd.Timestamp(frc.range_repair_sessions[0]))
        & result.recovered["ticker"].eq("FRC")
    ].iloc[0]

    assert frc.expanded_range_rows == 1
    assert 0.0 < frc.maximum_relative_range_expansion < 0.01
    assert repaired["High"] == max(
        repaired["Open"], repaired["Low"], repaired["Close"]
    )


def test_excessive_range_repair_fails_closed() -> None:
    membership, prices = _inputs()

    def broken_downloader(
        tickers: list[str],
        *,
        start: str,
        end: str,
        **kwargs: object,
    ) -> pd.DataFrame:
        frame = _downloader(tickers, start=start, end=end, **kwargs)
        session = frame.index[10]
        frame.loc[session, ("High", tickers[0])] = (
            frame.loc[session, ("Close", tickers[0])] * 0.95
        )
        return frame

    with pytest.raises(HistoricalIdentityError, match="excessive OHLC range repair"):
        build_yahoo_identity_recovery(
            membership,
            prices,
            downloader=broken_downloader,
            audit_start="2018-01-02",
        )


def test_snapshot_is_hash_verified_and_never_backtest_ready(tmp_path: Path) -> None:
    membership, prices = _inputs()
    membership_path = tmp_path / "membership.parquet"
    prices_path = tmp_path / "prices.parquet"
    membership.to_parquet(membership_path, index=False)
    prices.to_parquet(prices_path, index=False)

    snapshot = write_yahoo_identity_recovery(
        membership_path,
        prices_path,
        tmp_path / "output",
        downloader=_downloader,
        now=datetime(2026, 10, 8, tzinfo=UTC),
        audit_start="2018-01-02",
    )
    payload = json.loads(snapshot.manifest_path.read_text())

    assert verify_yahoo_identity_recovery(snapshot.manifest_path)
    assert payload["historical_backtest_ready"] is False
    assert payload["action_authorized"] is False
    assert payload["raw_data_committable"] is False
    assert payload["original_coverage"]["missing_tickers"] == ["FRC", "HFC"]
    assert payload["supplemented_coverage"]["missing_tickers"] == []

    payload["status"] = "tampered"
    snapshot.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert not verify_yahoo_identity_recovery(snapshot.manifest_path)
