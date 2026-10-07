from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from swing_trader.alpha_validation import (
    AlphaValidationError,
    latest_alpha_validation_for_candidate,
    parse_alpha_daily_csv,
    validate_candidate_snapshot_with_alpha,
    verify_alpha_candidate_validation,
)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_alpha_candidate_validation_uses_finite_free_quota_and_matches_closes(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path, {"A": 101.0, "SPY": 501.0})
    payloads = {
        "A": _csv("2026-10-06", 101.0),
        "SPY": _csv("2026-10-06", 501.0),
    }

    def opener(request: object, timeout: int) -> _Response:
        assert timeout == 30
        url = request.full_url  # type: ignore[attr-defined]
        symbol = "SPY" if "symbol=SPY" in url else "A"
        return _Response(payloads[symbol])

    output = validate_candidate_snapshot_with_alpha(
        candidate,
        "secret",
        tmp_path / "output",
        quota_dir=tmp_path / "quota",
        opener=opener,
        request_interval_seconds=0.0,
        now=datetime(2026, 10, 7, 20, tzinfo=UTC),
    )
    result = json.loads(output.read_text())
    ledger = json.loads((tmp_path / "quota/2026-10-07.json").read_text())

    assert result["validation"]["status"] == "passed"
    assert result["validation"]["symbols_matched"] == 2
    assert ledger["reserved_calls"] == 2
    assert "secret" not in output.read_text()
    assert verify_alpha_candidate_validation(output)
    assert latest_alpha_validation_for_candidate(output.parent, candidate) == output


def test_alpha_candidate_validation_retains_failed_reconciliation(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, {"A": 100.0})

    with pytest.raises(AlphaValidationError, match="evidence retained"):
        validate_candidate_snapshot_with_alpha(
            candidate,
            "secret",
            tmp_path / "output",
            quota_dir=tmp_path / "quota",
            opener=lambda *_args, **_kwargs: _Response(_csv("2026-10-05", 120.0)),
            request_interval_seconds=0.0,
            now=datetime(2026, 10, 7, 20, tzinfo=UTC),
        )

    evidence = json.loads(next((tmp_path / "output").glob("*.json")).read_text())
    assert evidence["validation"]["status"] == "failed"
    assert evidence["validation"]["stale_symbols"] == ["A"]
    assert evidence["validation"]["divergent_symbols"] == ["A"]


def test_alpha_daily_parser_rejects_quota_or_entitlement_payload() -> None:
    with pytest.raises(AlphaValidationError, match="quota, entitlement"):
        parse_alpha_daily_csv(b'{"Information":"limit"}', "A")


def _candidate(tmp_path: Path, closes: dict[str, float]) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "as_of_session": "2026-10-06",
        "validation_symbols": list(closes),
        "primary_latest_close": closes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(payload))
    return path


def _csv(day: str, close: float) -> bytes:
    return (
        "timestamp,open,high,low,close,volume\r\n"
        f"{day},{close - 1},{close + 1},{close - 2},{close},1000000\r\n"
    ).encode()
