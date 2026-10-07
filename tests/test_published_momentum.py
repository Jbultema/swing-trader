from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from swing_trader.provenance import file_sha256
from swing_trader.published_momentum import (
    FACTORS_URL,
    MOMENTUM_URL,
    SHORT_REVERSAL_URL,
    SIZE_MOMENTUM_URL,
    SIZE_SHORT_REVERSAL_URL,
    PublishedDocument,
    PublishedMomentumError,
    build_published_momentum_returns,
    parse_daily_factors,
    parse_daily_portfolios,
    parse_daily_size_portfolios,
    write_published_momentum_report,
)


def test_published_daily_parsers_align_and_normalize_percent_returns() -> None:
    momentum = parse_daily_portfolios(_portfolio_zip((1.0, 2.0), (1.5, 2.5), (-1.0, 0.5)))
    reversal = parse_daily_portfolios(_portfolio_zip((2.0, -1.0), (1.0, 0.0), (0.5, -0.5)))
    size_momentum = parse_daily_size_portfolios(
        _size_portfolio_zip((1.0, 2.0), (1.5, 2.5), (-1.0, 0.5))
    )
    size_reversal = parse_daily_size_portfolios(
        _size_portfolio_zip((2.0, -1.0), (1.0, 0.0), (0.5, -0.5))
    )
    factors = parse_daily_factors(_factor_zip((0.5, 0.01), (0.4, 0.01), (-0.2, 0.01)))

    result = build_published_momentum_returns(
        momentum,
        reversal,
        size_momentum,
        size_reversal,
        factors,
    )

    assert result.index.tolist() == list(pd.date_range("2026-08-27", periods=3, freq="D"))
    assert result.loc["2026-08-27", "momentum_winner_12_2"] == pytest.approx(0.02)
    assert result.loc["2026-08-27", "short_term_loser_1_0"] == pytest.approx(0.02)
    assert result.loc["2026-08-27", "large_momentum_winner_12_2"] == pytest.approx(0.02)
    assert result.loc["2026-08-27", "market"] == pytest.approx(0.0051)
    assert result.loc["2026-08-27", "risk_free"] == pytest.approx(0.0001)


def test_published_parser_rejects_missing_value_weighted_section() -> None:
    content = _zip("source.csv", "not the expected section\n")

    with pytest.raises(PublishedMomentumError, match="missing section"):
        parse_daily_portfolios(content)


def test_published_report_locks_sources_and_labels_gross_limitations(tmp_path: Path) -> None:
    documents = {
        MOMENTUM_URL: PublishedDocument(
            MOMENTUM_URL,
            _portfolio_zip((1.0, 2.0), (1.5, 2.5), (-1.0, 0.5)),
            "2026-10-07T20:00:00+00:00",
        ),
        SHORT_REVERSAL_URL: PublishedDocument(
            SHORT_REVERSAL_URL,
            _portfolio_zip((2.0, -1.0), (1.0, 0.0), (0.5, -0.5)),
            "2026-10-07T20:00:00+00:00",
        ),
        SIZE_MOMENTUM_URL: PublishedDocument(
            SIZE_MOMENTUM_URL,
            _size_portfolio_zip((1.0, 2.0), (1.5, 2.5), (-1.0, 0.5)),
            "2026-10-07T20:00:00+00:00",
        ),
        SIZE_SHORT_REVERSAL_URL: PublishedDocument(
            SIZE_SHORT_REVERSAL_URL,
            _size_portfolio_zip((2.0, -1.0), (1.0, 0.0), (0.5, -0.5)),
            "2026-10-07T20:00:00+00:00",
        ),
        FACTORS_URL: PublishedDocument(
            FACTORS_URL,
            _factor_zip((0.5, 0.01), (0.4, 0.01), (-0.2, 0.01)),
            "2026-10-07T20:00:00+00:00",
        ),
    }

    result = write_published_momentum_report(
        tmp_path / "sources",
        tmp_path / "reports",
        fetcher=lambda url: documents[url],
        now=datetime(2026, 10, 7, 21, tzinfo=UTC),
    )

    assert result.source_status == "passed"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["action_authorized"] is False
    assert manifest["daily_return_rows"] == 3
    assert "gross" in manifest["research_status"]
    assert len(manifest["sources"]) == 5
    assert manifest["artifacts"][result.metrics_path.name] == file_sha256(result.metrics_path)
    metrics = pd.read_csv(result.metrics_path)
    assert set(metrics["strategy"]) == {
        "market",
        "large_momentum_loser_12_2",
        "large_momentum_winner_12_2",
        "large_short_term_loser_1_0",
        "large_short_term_winner_1_0",
        "momentum_loser_12_2",
        "momentum_winner_12_2",
        "short_term_loser_1_0",
        "short_term_winner_1_0",
    }


def _portfolio_zip(*lo_hi: tuple[float, float]) -> bytes:
    rows = [
        "This file was created using a test database.",
        "",
        "  Average Value Weighted Returns -- Daily",
        ",Lo PRIOR,PRIOR 2,PRIOR 3,PRIOR 4,PRIOR 5,PRIOR 6,PRIOR 7,PRIOR 8,PRIOR 9,Hi PRIOR",
    ]
    for offset, (low, high) in enumerate(lo_hi):
        date = pd.Timestamp("2026-08-27") + timedelta(days=offset)
        middle = ",".join("0.10" for _ in range(8))
        rows.append(f"{date:%Y%m%d},{low},{middle},{high}")
    rows.append("")
    return _zip("portfolios.csv", "\n".join(rows))


def _factor_zip(*market_rf: tuple[float, float]) -> bytes:
    rows = [
        "This file was created using a test database.",
        ",Mkt-RF,SMB,HML,RF",
    ]
    for offset, (market, risk_free) in enumerate(market_rf):
        date = pd.Timestamp("2026-08-27") + timedelta(days=offset)
        rows.append(f"{date:%Y%m%d},{market},0.0,0.0,{risk_free}")
    rows.append("")
    return _zip("factors.csv", "\n".join(rows))


def _size_portfolio_zip(*lo_hi: tuple[float, float]) -> bytes:
    rows = [
        "This file was created using a test database.",
        "",
        "  Average Value Weighted Returns -- Daily",
        ",SMALL LoPRIOR,ME1 PRIOR2,SMALL HiPRIOR,BIG LoPRIOR,ME2 PRIOR2,BIG HiPRIOR",
    ]
    for offset, (low, high) in enumerate(lo_hi):
        date = pd.Timestamp("2026-08-27") + timedelta(days=offset)
        rows.append(f"{date:%Y%m%d},0.0,0.0,0.0,{low},0.0,{high}")
    rows.append("")
    return _zip("size_portfolios.csv", "\n".join(rows))


def _zip(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()
