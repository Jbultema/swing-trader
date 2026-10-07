from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from swing_trader.cli import app
from swing_trader.provenance import file_sha256
from swing_trader.sec_filing_events import (
    SEC_EVENT_ROLE,
    SecFilingEventError,
    SecSourceDocument,
    audit_sec_event_snapshot,
    build_sec_event_diagnostics,
    download_candidate_sec_events,
    parse_sec_submission,
    sec_events_for_candidate,
    write_sec_event_snapshot,
)


def test_sec_parser_uses_acceptance_time_and_maps_class_share_ticker() -> None:
    captured = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    rows = [
        _filing("0000001000-26-000001", "8-K", "2026-10-06T18:05:00.000Z", "2.02,9.01"),
        _filing("0000001000-26-000002", "10-Q", "2026-09-30T12:00:00.000Z"),
        _filing("0000001000-26-000003", "8-K", "2026-10-07T21:00:00.000Z", "5.02"),
        _filing("0000001000-26-000004", "S-8", "2026-10-01T12:00:00.000Z"),
        _filing("0000001000-26-000005", "8-K", "2026-05-01T12:00:00.000Z"),
    ]
    document = _document("0000001000", ("BRK.B",), rows, payload_tickers=["BRK-B"])

    frame, validation = parse_sec_submission(document, captured_at=captured)

    assert validation.passed
    assert validation.excluded_after_capture_rows == 1
    assert validation.excluded_before_lookback_rows == 1
    assert set(frame["form"]) == {"8-K", "10-Q"}
    assert frame["ticker"].eq("BRK.B").all()
    assert frame["accepted_at_utc"].max() <= pd.Timestamp(captured)
    assert frame.loc[frame["form"].eq("8-K"), "filing_url"].iloc[0].endswith(
        "/000000100026000001/primary.htm"
    )


def test_sec_parser_rejects_schema_and_identity_failures() -> None:
    captured = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    payload = _payload("0000001000", ["A"], [_filing("bad", "8-K", "not-a-time")])
    document = SecSourceDocument(
        "0000001000",
        ("A",),
        "https://data.sec.gov/submissions/CIK0000001000.json",
        json.dumps(payload).encode(),
        captured.isoformat(),
    )

    _, validation = parse_sec_submission(document, captured_at=captured)

    assert not validation.passed
    assert validation.invalid_accession_rows == 1
    assert validation.invalid_acceptance_rows == 1

    payload["filings"]["recent"].pop("items")
    malformed = SecSourceDocument(
        document.cik,
        document.tickers,
        document.url,
        json.dumps(payload).encode(),
        document.fetched_at_utc,
    )
    with pytest.raises(SecFilingEventError, match="missing fields"):
        parse_sec_submission(malformed, captured_at=captured)


def test_sec_snapshot_allows_zero_events_but_preserves_source_proof(tmp_path: Path) -> None:
    captured = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    documents = (
        _document(
            "0000001000",
            ("A",),
            [_filing("0000001000-26-000001", "S-8", "2026-10-06T18:05:00.000Z")],
        ),
        _document(
            "0000001001",
            ("B",),
            [_filing("0000001001-26-000001", "S-3", "2026-10-05T18:05:00.000Z")],
        ),
    )

    snapshot = write_sec_event_snapshot(
        documents,
        ("A", "B"),
        tmp_path,
        captured_at=captured,
    )
    manifest = json.loads(snapshot.manifest_path.read_text())
    audit = audit_sec_event_snapshot(snapshot.manifest_path, now=captured)

    assert audit.passed
    assert snapshot.rows == 0
    assert manifest["data_role"] == SEC_EVENT_ROLE
    assert manifest["access_classification"] == (
        "official_public_keyless_free_to_access_and_reuse"
    )
    assert manifest["candidate_ranking_input"] is False
    assert manifest["portfolio_state_input"] is False
    assert manifest["action_authorized"] is False
    assert len(manifest["sources"]) == 2
    assert {row["ticker"] for row in manifest["candidate_diagnostics"]} == {"A", "B"}


def test_sec_snapshot_audit_detects_tampering_and_candidate_reuse(tmp_path: Path) -> None:
    captured = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    candidate: dict[str, object] = {
        "schema_version": 1,
        "as_of_session": "2026-10-06",
        "validation_symbols": ["A"],
    }
    canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    candidate["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate))
    document = _document(
        "0000001000",
        ("A",),
        [_filing("0000001000-26-000001", "8-K", "2026-10-06T18:05:00.000Z", "2.02")],
    )
    output = tmp_path / "sec"
    snapshot = write_sec_event_snapshot(
        (document,),
        ("A",),
        output,
        captured_at=captured,
        bindings={"candidate_file_sha256": file_sha256(candidate_path)},
    )

    assert sec_events_for_candidate(output, candidate_path) == snapshot.manifest_path

    with snapshot.data_path.open("ab") as handle:
        handle.write(b"tampered")
    audit = audit_sec_event_snapshot(snapshot.manifest_path, now=captured)
    assert not audit.integrity_passed
    assert "hash" in " ".join(audit.errors)
    assert sec_events_for_candidate(output, candidate_path) is None


def test_sec_diagnostics_are_event_only_and_explain_timing() -> None:
    captured = datetime(2026, 10, 7, 22, 0, tzinfo=UTC)
    frame, validation = parse_sec_submission(
        _document(
            "0000001000",
            ("A",),
            [
                _filing(
                    "0000001000-26-000001",
                    "8-K",
                    "2026-10-07T20:05:00.000Z",
                    "1.01,2.02,5.02,7.01",
                ),
                _filing("0000001000-26-000002", "4", "2026-10-06T18:05:00.000Z"),
            ],
        ),
        captured_at=captured,
    )
    assert validation.passed

    diagnostic = build_sec_event_diagnostics(frame, ("A",), captured_at=captured)[0]

    assert diagnostic["earnings_release_8k_21d"] == 1
    assert diagnostic["material_agreement_8k_21d"] == 1
    assert diagnostic["management_change_8k_21d"] == 1
    assert diagnostic["reg_fd_8k_21d"] == 1
    assert diagnostic["form4_metadata_21d"] == 1
    assert diagnostic["latest_acceptance_market_phase"] == "after_market"
    assert diagnostic["interpretation"] == (
        "event_metadata_only_no_sentiment_or_directional_claim"
    )


def test_sec_download_rejects_manifests_not_bound_to_candidate(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "schema_version": 1,
        "as_of_session": "2026-10-06",
        "validation_symbols": ["A"],
        "inputs": {
            "universe_manifest_sha256": "expected-universe",
            "price_manifest_sha256": "expected-prices",
        },
    }
    canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    candidate["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate))
    universe = tmp_path / "universe.json"
    prices = tmp_path / "prices.json"
    universe.write_text("{}")
    prices.write_text("{}")

    with pytest.raises(SecFilingEventError, match="not bound"):
        download_candidate_sec_events(candidate_path, universe, prices, tmp_path / "out")


def test_sec_access_probe_preserves_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_fetch(*args: object, **kwargs: object) -> SecSourceDocument:
        raise SecFilingEventError("403 forbidden")

    monkeypatch.setattr("swing_trader.cli.fetch_sec_submission", fail_fetch)
    output = tmp_path / "probe.json"

    result = CliRunner().invoke(
        app,
        [
            "data",
            "probe-sec-access",
            "--cik",
            "0000320193",
            "--ticker",
            "AAPL",
            "--output",
            str(output),
        ],
    )

    report = json.loads(output.read_text())
    assert result.exit_code == 2
    assert report["status"] == "failed"
    assert report["data_cost_policy"] == "no_paid_sources"
    assert report["action_authorized"] is False
    assert "403" in report["error"]


def _document(
    cik: str,
    tickers: tuple[str, ...],
    rows: list[dict[str, object]],
    *,
    payload_tickers: list[str] | None = None,
) -> SecSourceDocument:
    captured = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    payload = _payload(cik, payload_tickers or list(tickers), rows)
    return SecSourceDocument(
        cik,
        tickers,
        f"https://data.sec.gov/submissions/CIK{cik}.json",
        json.dumps(payload).encode(),
        captured.isoformat(),
        "application/json",
    )


def _payload(
    cik: str,
    tickers: list[str],
    rows: list[dict[str, object]],
) -> dict[str, object]:
    fields = rows[0].keys() if rows else _filing("x", "S-8", "2026-01-01T00:00:00Z").keys()
    return {
        "cik": cik,
        "name": f"Issuer {cik}",
        "tickers": tickers,
        "filings": {"recent": {field: [row[field] for row in rows] for field in fields}},
    }


def _filing(
    accession: str,
    form: str,
    acceptance: str,
    items: str = "",
) -> dict[str, object]:
    return {
        "accessionNumber": accession,
        "filingDate": "2026-10-06",
        "reportDate": "2026-09-30",
        "acceptanceDateTime": acceptance,
        "act": "34",
        "form": form,
        "fileNumber": "001-00001",
        "filmNumber": "261234567",
        "items": items,
        "size": 12345,
        "isXBRL": 1,
        "isInlineXBRL": 1,
        "primaryDocument": "primary.htm",
        "primaryDocDescription": "Primary document",
    }
