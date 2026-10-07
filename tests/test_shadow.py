from __future__ import annotations

import json
from pathlib import Path

import pytest

from swing_trader.shadow import record_shadow_snapshot, verify_shadow_snapshot


def test_shadow_snapshot_is_hashed_and_non_overwriting(tmp_path: Path) -> None:
    reports = tmp_path / "latest"
    reports.mkdir()
    _write_json(
        reports / "manifest.json",
        {
            "created_at_utc": "2026-10-07T01:00:00+00:00",
            "system": "candidate",
            "specification_sha256": "abc",
            "research_status": "research_only",
        },
    )
    _write_json(
        reports / "latest_decisions.json",
        {"action_authorized": False, "hypothetical_actions": []},
    )
    _write_json(reports / "data_quality.json", {"status": "failed"})

    output = record_shadow_snapshot(reports, tmp_path / "shadow")
    assert verify_shadow_snapshot(output)
    with pytest.raises(FileExistsError):
        record_shadow_snapshot(reports, tmp_path / "shadow")


def test_shadow_verification_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    _write_json(path, {"record_sha256": "wrong", "system": "candidate"})
    assert verify_shadow_snapshot(path) is False


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
