from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from swing_trader.stock_universe import (
    SEC_COMPANY_TICKERS_URL,
    WIKIPEDIA_SP500_URL,
    SourceDocument,
    UniverseDataError,
    audit_current_sp500_snapshot,
    build_current_sp500_universe,
    download_current_sp500_snapshot,
    validate_current_sp500_universe,
)


def test_current_universe_normalizes_symbols_and_reconciles_sec_ciks() -> None:
    wikipedia, sec = _source_payloads(3, dotted_ticker=True)

    frame = build_current_sp500_universe(wikipedia, sec)
    audit = validate_current_sp500_universe(frame, minimum_rows=3, maximum_rows=3)

    assert audit.passed
    assert frame.loc[0, "ticker"] == "BRK.B"
    assert frame.loc[0, "provider_ticker_yahoo"] == "BRK-B"
    assert frame.loc[0, "cik"] == "0000001000"
    assert frame["sec_cik"].notna().all()


def test_current_universe_rejects_duplicate_tickers_and_cik_disagreement() -> None:
    wikipedia, sec = _source_payloads(3)
    frame = build_current_sp500_universe(wikipedia, sec)
    frame.loc[1, "ticker"] = frame.loc[0, "ticker"]
    frame.loc[2, "sec_cik"] = "0000099999"

    audit = validate_current_sp500_universe(frame, minimum_rows=3, maximum_rows=3)

    assert not audit.passed
    assert audit.duplicate_tickers
    assert audit.cik_mismatches == (frame.loc[2, "ticker"],)


def test_current_universe_rejects_malformed_source_table() -> None:
    bad_html = pd.DataFrame({"Symbol": ["A"]}).to_html(table_id="constituents").encode()
    _, sec = _source_payloads(1)

    with pytest.raises(UniverseDataError, match="missing columns"):
        build_current_sp500_universe(bad_html, sec)


def test_current_universe_records_unavailable_sec_as_nonfatal_diagnostic() -> None:
    wikipedia, _ = _source_payloads(3)

    frame = build_current_sp500_universe(wikipedia, None)
    audit = validate_current_sp500_universe(frame, minimum_rows=3, maximum_rows=3)

    assert audit.passed
    assert audit.sec_reconciliation_status == "unavailable"
    assert frame["cik"].notna().all()


def test_snapshot_is_non_overwriting_hashed_and_prospective_only(tmp_path: Path) -> None:
    captured = datetime(2026, 10, 7, 21, 15, tzinfo=UTC)
    wikipedia, sec = _source_payloads(490)
    documents = {
        WIKIPEDIA_SP500_URL: SourceDocument(
            WIKIPEDIA_SP500_URL,
            wikipedia,
            captured.isoformat(),
            "text/html",
        ),
        SEC_COMPANY_TICKERS_URL: SourceDocument(
            SEC_COMPANY_TICKERS_URL,
            sec,
            captured.isoformat(),
            "application/json",
        ),
    }

    snapshot = download_current_sp500_snapshot(
        tmp_path,
        fetcher=documents.__getitem__,
        captured_at=captured,
    )
    manifest = json.loads(snapshot.manifest_path.read_text())
    audit = audit_current_sp500_snapshot(
        snapshot.manifest_path,
        now=captured + timedelta(hours=1),
    )

    assert audit.passed
    assert manifest["universe_role"] == (
        "prospective_current_universe_only_not_historical_backfill"
    )
    assert manifest["historical_backfill_authorized"] is False
    assert manifest["action_authorized"] is False
    assert manifest["sources"]["sec_company_tickers"]["sha256"]
    with pytest.raises(FileExistsError):
        download_current_sp500_snapshot(
            tmp_path,
            fetcher=documents.__getitem__,
            captured_at=captured,
        )


def test_snapshot_audit_detects_tampering_and_staleness(tmp_path: Path) -> None:
    captured = datetime(2026, 10, 7, 21, 15, tzinfo=UTC)
    wikipedia, sec = _source_payloads(490)
    documents = {
        WIKIPEDIA_SP500_URL: SourceDocument(
            WIKIPEDIA_SP500_URL, wikipedia, captured.isoformat()
        ),
        SEC_COMPANY_TICKERS_URL: SourceDocument(
            SEC_COMPANY_TICKERS_URL, sec, captured.isoformat()
        ),
    }
    snapshot = download_current_sp500_snapshot(
        tmp_path,
        fetcher=documents.__getitem__,
        captured_at=captured,
    )

    stale = audit_current_sp500_snapshot(
        snapshot.manifest_path,
        max_age_hours=24,
        now=captured + timedelta(hours=25),
    )
    assert stale.integrity_passed
    assert stale.status == "stale"

    with snapshot.data_path.open("ab") as handle:
        handle.write(b"tampered")
    tampered = audit_current_sp500_snapshot(
        snapshot.manifest_path,
        now=captured + timedelta(hours=1),
    )
    assert not tampered.integrity_passed
    assert "hash" in " ".join(tampered.errors)


def _source_payloads(count: int, *, dotted_ticker: bool = False) -> tuple[bytes, bytes]:
    symbols = [f"T{i:03d}" for i in range(count)]
    if dotted_ticker:
        symbols[0] = "BRK.B"
    table = pd.DataFrame(
        {
            "Symbol": symbols,
            "Security": [f"Company {i}" for i in range(count)],
            "GICS Sector": ["Industrials"] * count,
            "GICS Sub-Industry": ["Research"] * count,
            "Headquarters Location": ["Denver, Colorado"] * count,
            "Date added": ["2020-01-02"] * count,
            "CIK": [1000 + i for i in range(count)],
            "Founded": ["2000"] * count,
        }
    )
    sec = {
        str(i): {
            "cik_str": 1000 + i,
            "ticker": symbol.replace(".", "-"),
            "title": f"Company {i}",
        }
        for i, symbol in enumerate(symbols)
    }
    return (
        table.to_html(index=False, table_id="constituents").encode(),
        json.dumps(sec).encode(),
    )
