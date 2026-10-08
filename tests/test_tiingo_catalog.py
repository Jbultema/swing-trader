from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

from swing_trader.tiingo_catalog import audit_tiingo_catalog_coverage


def _catalog_archive(rows: list[dict[str, object]]) -> bytes:
    columns = ["ticker", "exchange", "assetType", "priceCurrency", "startDate", "endDate"]
    payload = pd.DataFrame(rows, columns=columns).to_csv(index=False).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("supported_tickers.csv", payload)
    return buffer.getvalue()


def test_catalog_audit_is_only_an_upper_bound_and_quarantines_recycled_symbols() -> None:
    membership = pd.DataFrame(
        [
            {"ticker": "A", "start": "2019-01-01", "end": None},
            {"ticker": "OLD-202101", "start": "2019-01-01", "end": "2021-01-01"},
            {"ticker": "MISS", "start": "2019-01-01", "end": None},
            {"ticker": "RECYCLE", "start": "2019-01-01", "end": "2021-01-01"},
        ]
    )
    prices = pd.DataFrame(
        [
            {"ticker": "A", "date": "2019-01-02"},
            {"ticker": "A", "date": "2021-01-04"},
            {"ticker": "RECYCLE", "date": "2022-01-03"},
        ]
    )
    catalog = _catalog_archive(
        [
            {
                "ticker": "OLD",
                "exchange": "NYSE",
                "assetType": "Stock",
                "priceCurrency": "USD",
                "startDate": "2010-01-01",
                "endDate": "2021-01-01",
            },
            {
                "ticker": "MISS",
                "exchange": "NYSE",
                "assetType": "Stock",
                "priceCurrency": "USD",
                "startDate": "",
                "endDate": "",
            },
        ]
    )

    result = audit_tiingo_catalog_coverage(
        membership,
        prices,
        catalog,
        probe_dates=("2020-01-02",),
    )

    date = result.dates[0]
    assert result.quarantined_recycled_tickers == ("RECYCLE",)
    assert date.point_in_time_members == 3
    assert date.existing_price_ranges == 1
    assert date.suffixed_symbol_candidates == ("OLD-202101",)
    assert date.unresolved_tickers == ("MISS",)
    assert date.catalog_upper_bound_coverage == pytest.approx(2 / 3)
    assert result.status == "research_only_catalog_upper_bound"
    assert result.authenticated_api_verified is False
    assert result.historical_backtest_ready is False
    assert result.action_authorized is False


def test_membership_end_is_exclusive() -> None:
    membership = pd.DataFrame(
        [{"ticker": "A", "start": "2019-01-01", "end": "2020-01-02"}]
    )
    prices = pd.DataFrame([{"ticker": "A", "date": "2020-01-02"}])
    catalog = _catalog_archive([])

    result = audit_tiingo_catalog_coverage(
        membership,
        prices,
        catalog,
        probe_dates=("2020-01-02",),
    )

    assert result.dates[0].point_in_time_members == 0


def test_catalog_requires_documented_schema() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("supported_tickers.csv", b"ticker,startDate,endDate\nA,2020-01-01,2020-02-01\n")

    with pytest.raises(ValueError, match="missing columns"):
        audit_tiingo_catalog_coverage(
            pd.DataFrame([{"ticker": "A", "start": "2020-01-01", "end": None}]),
            pd.DataFrame([{"ticker": "A", "date": "2020-01-02"}]),
            buffer.getvalue(),
            probe_dates=("2020-01-02",),
        )
