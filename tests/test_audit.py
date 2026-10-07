from __future__ import annotations

import hashlib
import json
from pathlib import Path

from swing_trader.audit import audit_operational_artifacts
from swing_trader.shadow import record_shadow_snapshot


def test_operational_audit_accepts_consistent_fail_closed_bundle(tmp_path: Path) -> None:
    reports, shadows, prices, prices_manifest = _bundle(tmp_path)
    result = audit_operational_artifacts(
        reports,
        shadows,
        prices_path=prices,
        prices_manifest_path=prices_manifest,
    )

    assert result.integrity_passed
    assert result.data_gate_passed is False
    assert result.action_authorized is False
    assert result.shadow_records == 1
    assert "FAIL CLOSED" in result.markdown()


def test_operational_audit_rejects_tampering_and_cross_file_disagreement(
    tmp_path: Path,
) -> None:
    reports, shadows, prices, prices_manifest = _bundle(tmp_path)
    quality = _read(reports / "data_quality.json")
    quality["decision_data_gate_passed"] = True
    _write(reports / "data_quality.json", quality)
    shadow_path = next(shadows.glob("*.json"))
    shadow = _read(shadow_path)
    shadow["system"] = "tampered"
    _write(shadow_path, shadow)

    result = audit_operational_artifacts(
        reports,
        shadows,
        prices_path=prices,
        prices_manifest_path=prices_manifest,
    )

    assert not result.integrity_passed
    assert any("disagree" in error for error in result.errors)
    assert any("hash verification" in error for error in result.errors)


def _bundle(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    reports = tmp_path / "latest"
    shadows = tmp_path / "shadow"
    reports.mkdir()
    prices = tmp_path / "prices.parquet"
    prices.write_bytes(b"immutable-price-snapshot")
    prices_digest = hashlib.sha256(prices.read_bytes()).hexdigest()
    prices_manifest = tmp_path / "prices.manifest.json"
    _write(prices_manifest, {"sha256": prices_digest})
    manifest = {
        "created_at_utc": "2026-10-07T02:00:00+00:00",
        "system": "candidate",
        "research_status": "research_only",
        "automatic_order_placement": False,
        "decision_data_gate_passed": False,
        "data_quality_status": "secondary_source_unavailable",
        "data_end": "2026-10-06",
    }
    manifest["specification_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, default=list).encode()
    ).hexdigest()
    _write(reports / "manifest.json", manifest)
    _write(
        reports / "latest_decisions.json",
        {
            "as_of_close": "2026-10-06",
            "mode": "research_only_data_unreconciled_no_action",
            "action_authorized": False,
            "data_reconciled": False,
            "data_quality_status": "secondary_source_unavailable",
            "capital_preservation_overlay": {"hypothetical_action": "HOLD"},
            "hypothetical_actions": [{"ticker": "SPY", "action": "HOLD"}],
        },
    )
    _write(
        reports / "data_quality.json",
        {
            "status": "secondary_source_unavailable",
            "decision_data_gate_passed": False,
            "primary_snapshot_sha256": prices_digest,
        },
    )
    record_shadow_snapshot(reports, shadows)
    return reports, shadows, prices, prices_manifest


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
