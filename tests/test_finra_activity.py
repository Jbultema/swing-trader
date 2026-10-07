from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from swing_trader.finra_activity import (
    FINRA_ACTIVITY_ROLE,
    FinraActivityError,
    FinraSourceDocument,
    audit_finra_activity_snapshot,
    build_finra_activity_diagnostics,
    finra_activity_for_candidate,
    parse_finra_short_volume_file,
    write_finra_activity_snapshot,
)
from swing_trader.provenance import file_sha256


def test_finra_parser_preserves_fractional_volume_and_class_share_mapping() -> None:
    session = date(2026, 10, 6)
    content = _file(
        session,
        [
            ("BRK/B", "402856.317890", "3653", "1077580.129474", "B,Q,N"),
            ("NA", "18586.683295", "0", "45350.173649", "Q,N"),
            ("META", "1829653.625401", "5064", "3961770.813837", "B,Q,N"),
        ],
    )

    frame, validation = parse_finra_short_volume_file(content, session, minimum_rows=2)

    assert validation.passed
    assert frame.loc[0, "ticker"] == "BRK.B"
    assert frame.loc[0, "finra_symbol"] == "BRK/B"
    assert frame.loc[0, "short_volume"] == pytest.approx(402856.317890)
    assert frame.loc[0, "reported_total_volume"] == pytest.approx(1077580.129474)
    assert frame.loc[1, "ticker"] == "NA"


def test_finra_parser_rejects_bad_trailer_and_impossible_volume() -> None:
    session = date(2026, 10, 6)
    wrong_count = _file(session, [("A", "10", "0", "20", "Q")], trailer=2)
    frame, validation = parse_finra_short_volume_file(wrong_count, session, minimum_rows=1)

    assert len(frame) == 1
    assert not validation.passed
    assert validation.declared_rows == 2

    impossible = _file(session, [("A", "21", "0", "20", "Q")])
    _, impossible_validation = parse_finra_short_volume_file(
        impossible,
        session,
        minimum_rows=1,
    )
    assert not impossible_validation.passed
    assert impossible_validation.short_volume_above_total_rows == 1


def test_finra_parser_rejects_a_nontrading_error_payload() -> None:
    with pytest.raises(FinraActivityError, match="header"):
        parse_finra_short_volume_file(b"Access Denied", date(2026, 10, 3))


def test_finra_snapshot_is_candidate_context_only_and_auditable(tmp_path: Path) -> None:
    captured = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
    sessions = [date(2026, 9, 30) + timedelta(days=value) for value in range(5)]
    documents = tuple(_document(session, captured) for session in sessions)

    snapshot = write_finra_activity_snapshot(
        documents,
        ("A", "BRK.B"),
        tmp_path,
        captured_at=captured,
        minimum_sessions=5,
    )
    manifest = json.loads(snapshot.manifest_path.read_text())
    frame = pd.read_parquet(snapshot.data_path)
    audit = audit_finra_activity_snapshot(snapshot.manifest_path, now=captured)

    assert audit.passed
    assert manifest["data_role"] == FINRA_ACTIVITY_ROLE
    assert manifest["candidate_ranking_input"] is False
    assert manifest["portfolio_state_input"] is False
    assert manifest["directional_interpretation_authorized"] is False
    assert manifest["short_interest_interpretation_authorized"] is False
    assert manifest["action_authorized"] is False
    assert manifest["access_classification"] == "public_keyless_not_asserted_open_license"
    assert len(manifest["sources"]) == 5
    assert frame["source_file_sha256"].nunique() == 5
    assert frame["short_sale_volume_fraction"].between(0.0, 1.0).all()

    with snapshot.data_path.open("ab") as handle:
        handle.write(b"tampered")
    changed = audit_finra_activity_snapshot(snapshot.manifest_path, now=captured)
    assert not changed.integrity_passed
    assert "hash" in " ".join(changed.errors)


def test_finra_diagnostic_zscore_is_context_not_direction() -> None:
    frame = pd.DataFrame(
        {
            "session": pd.date_range("2026-09-01", periods=20, freq="B"),
            "ticker": ["A"] * 20,
            "short_sale_volume_fraction": [0.40] * 19 + [0.70],
        }
    )

    diagnostic = build_finra_activity_diagnostics(frame)[0]

    assert diagnostic["latest_vs_20_session_z_score"] is not None
    assert diagnostic["latest_vs_20_session_z_score"] > 0
    assert diagnostic["interpretation"] == "activity_context_only_no_directional_claim"


def test_finra_snapshot_can_be_reused_only_for_exact_candidate_bytes(tmp_path: Path) -> None:
    candidate: dict[str, object] = {
        "schema_version": 1,
        "as_of_session": "2026-10-06",
        "validation_symbols": ["A", "BRK.B"],
    }
    import hashlib

    canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    candidate["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate))
    captured = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
    documents = tuple(
        _document(date(2026, 9, 30) + timedelta(days=value), captured) for value in range(5)
    )
    output = tmp_path / "finra"
    snapshot = write_finra_activity_snapshot(
        documents,
        ("A", "BRK.B"),
        output,
        captured_at=captured,
        bindings={"candidate_file_sha256": file_sha256(candidate_path)},
        minimum_sessions=5,
    )

    assert finra_activity_for_candidate(output, candidate_path) == snapshot.manifest_path

    candidate_path.write_text(candidate_path.read_text() + "\n")
    assert finra_activity_for_candidate(output, candidate_path) is None


def _document(session: date, captured: datetime) -> FinraSourceDocument:
    filler = [
        (f"T{value:04d}", "40.5", "0.5", "100.25", "Q")
        for value in range(998)
    ]
    rows = [
        ("A", "50.5", "0.5", "100.25", "B,Q,N"),
        ("BRK/B", "45.25", "0", "100", "B,Q,N"),
        *filler,
    ]
    return FinraSourceDocument(
        session=session,
        url=f"https://cdn.finra.org/{session:%Y%m%d}.txt",
        content=_file(session, rows),
        fetched_at_utc=captured.isoformat(),
        content_type="text/plain",
    )


def _file(
    session: date,
    rows: list[tuple[str, str, str, str, str]],
    *,
    trailer: int | None = None,
) -> bytes:
    lines = ["Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"]
    lines.extend(
        f"{session:%Y%m%d}|{symbol}|{short}|{exempt}|{total}|{market}"
        for symbol, short, exempt, total, market in rows
    )
    lines.append(str(len(rows) if trailer is None else trailer))
    return ("\r\n".join(lines) + "\r\n").encode()
