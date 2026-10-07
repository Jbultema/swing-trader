from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from swing_trader.stock_shares import (
    audit_current_stock_share_snapshot,
    download_current_stock_shares,
    validate_current_stock_shares,
    write_current_stock_share_snapshot,
)
from swing_trader.stock_universe import (
    SEC_COMPANY_TICKERS_URL,
    WIKIPEDIA_SP500_URL,
    SourceDocument,
    build_current_sp500_universe,
    write_current_sp500_snapshot,
)


def test_share_snapshot_is_current_roster_only_and_forward_usable(tmp_path: Path) -> None:
    captured = datetime(2026, 10, 7, 22, tzinfo=UTC)
    universe = _universe_snapshot(tmp_path / "universe", captured)

    def fetcher(symbol: str, start: date, end: date) -> pd.Series:
        assert start < captured.date() < end
        suffix = int(symbol.removeprefix("T"))
        return pd.Series(
            [1_000_000 + suffix, 1_100_000 + suffix],
            index=pd.to_datetime(["2026-07-01", "2026-10-01"], utc=True),
        )

    snapshot = download_current_stock_shares(
        universe.manifest_path,
        tmp_path / "shares",
        share_fetcher=fetcher,
        now=captured,
        minimum_coverage_fraction=1.0,
        max_workers=4,
        retry_delay_seconds=0.0,
    )
    frame = pd.read_parquet(snapshot.data_path)
    manifest = json.loads(snapshot.manifest_path.read_text())

    assert snapshot.validation.passed
    assert snapshot.validation.coverage_fraction == 1.0
    assert len(frame) == 490
    assert frame["ticker"].is_unique
    assert frame["provider_observation_date"].dt.date.eq(date(2026, 10, 1)).all()
    assert frame["source_observation_count"].eq(2).all()
    assert frame["discarded_historical_observations"].eq(1).all()
    assert frame["captured_at_utc"].eq(captured.isoformat()).all()
    assert manifest["historical_backfill_authorized"] is False
    assert manifest["action_authorized"] is False
    assert "becomes usable no earlier than captured_at_utc" in manifest["provider_history_policy"]

    audit = audit_current_stock_share_snapshot(
        snapshot.manifest_path,
        universe_manifest_path=universe.manifest_path,
        now=captured + timedelta(hours=1),
    )
    assert audit.passed

    with snapshot.data_path.open("ab") as handle:
        handle.write(b"tampered")
    tampered = audit_current_stock_share_snapshot(
        snapshot.manifest_path,
        universe_manifest_path=universe.manifest_path,
        now=captured + timedelta(hours=1),
    )
    assert not tampered.integrity_passed
    assert "hash" in " ".join(tampered.errors)


def test_share_validation_rejects_invalid_or_future_values_and_lists_gaps() -> None:
    captured = datetime(2026, 10, 7, 22, tzinfo=UTC)
    frame = _share_frame(
        captured,
        [
            ("A", 1_000_000, "2026-10-01", "available"),
            ("B", pd.NA, None, "missing"),
            ("C", -10, "2026-10-01", "invalid"),
            ("D", 2_000_000, "2026-01-01", "available"),
            ("E", 3_000_000, "2026-10-08", "available"),
        ],
    )

    validation = validate_current_stock_shares(
        frame,
        ("A", "B", "C", "D", "E"),
        captured_at=captured,
        minimum_coverage_fraction=0.0,
        maximum_observation_age_calendar_days=130,
    )

    assert not validation.passed
    assert validation.usable_tickers == 1
    assert validation.missing_tickers == ("B",)
    assert validation.invalid_tickers == ("C",)
    assert validation.stale_tickers == ("D",)
    assert validation.future_observation_tickers == ("E",)


def test_failed_coverage_snapshot_is_preserved_but_not_usable(tmp_path: Path) -> None:
    captured = datetime(2026, 10, 7, 22, tzinfo=UTC)
    universe_manifest = tmp_path / "universe.manifest.json"
    universe_manifest.write_text(json.dumps({"tabular_sha256": "universe"}))
    frame = _share_frame(
        captured,
        [
            ("A", 1_000_000, "2026-10-01", "available"),
            ("B", pd.NA, None, "fetch_error"),
        ],
    )

    snapshot = write_current_stock_share_snapshot(
        frame,
        ("A", "B"),
        tmp_path / "shares",
        universe_manifest_path=universe_manifest,
        universe_manifest={
            "tabular_sha256": "universe",
            "universe_role": "prospective_current_universe_only_not_historical_backfill",
        },
        captured_at=captured,
        request_start=date(2025, 9, 2),
        request_end_exclusive=date(2026, 10, 8),
        minimum_coverage_fraction=1.0,
    )
    manifest = json.loads(snapshot.manifest_path.read_text())

    assert snapshot.data_path.exists()
    assert snapshot.manifest_path.exists()
    assert not snapshot.validation.passed
    assert snapshot.validation.fetch_error_tickers == ("B",)
    assert manifest["shares_data_gate_passed"] is False


def test_share_validation_rejects_inconsistent_source_accounting() -> None:
    captured = datetime(2026, 10, 7, 22, tzinfo=UTC)
    frame = _share_frame(
        captured,
        [("A", 1_000_000, "2026-10-01", "available")],
    )
    frame.loc[0, "source_observation_count"] = 3
    frame.loc[0, "discarded_historical_observations"] = 0

    validation = validate_current_stock_shares(
        frame,
        ("A",),
        captured_at=captured,
        minimum_coverage_fraction=1.0,
    )

    assert not validation.passed
    assert validation.inconsistent_source_tickers == ("A",)


def _share_frame(
    captured: datetime,
    rows: list[tuple[str, object, str | None, str]],
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": [row[0] for row in rows],
            "shares_outstanding": pd.array([row[1] for row in rows], dtype="Int64"),
            "provider_observation_date": pd.to_datetime([row[2] for row in rows]),
            "captured_at_utc": [captured.isoformat()] * len(rows),
            "source_status": [row[3] for row in rows],
            "source_observation_count": [0 if row[1] is pd.NA else 1 for row in rows],
            "discarded_historical_observations": [0] * len(rows),
            "provider_request_attempts": [1] * len(rows),
        }
    )


def _universe_snapshot(output: Path, captured: datetime):
    symbols = [f"T{i:03d}" for i in range(490)]
    table = pd.DataFrame(
        {
            "Symbol": symbols,
            "Security": [f"Company {i}" for i in range(490)],
            "GICS Sector": ["Industrials"] * 490,
            "GICS Sub-Industry": ["Research"] * 490,
            "Headquarters Location": ["Denver, Colorado"] * 490,
            "Date added": ["2020-01-02"] * 490,
            "CIK": [1000 + i for i in range(490)],
            "Founded": ["2000"] * 490,
        }
    )
    sec = {
        str(i): {
            "cik_str": 1000 + i,
            "ticker": symbol,
            "title": f"Company {i}",
        }
        for i, symbol in enumerate(symbols)
    }
    wikipedia = SourceDocument(
        WIKIPEDIA_SP500_URL,
        table.to_html(index=False, table_id="constituents").encode(),
        captured.isoformat(),
        "text/html",
    )
    sec_document = SourceDocument(
        SEC_COMPANY_TICKERS_URL,
        json.dumps(sec).encode(),
        captured.isoformat(),
        "application/json",
    )
    frame = build_current_sp500_universe(wikipedia.content, sec_document.content)
    return write_current_sp500_snapshot(
        frame,
        wikipedia,
        sec_document,
        output,
        captured_at=captured,
    )
