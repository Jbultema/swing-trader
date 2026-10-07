from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from swing_trader.cli import app
from swing_trader.provenance import file_sha256, implementation_sha256
from swing_trader.stock_audit import audit_stock_research_bundle


def test_stock_artifact_audit_accepts_a_bound_research_only_bundle(tmp_path: Path) -> None:
    _bundle(tmp_path)

    result = audit_stock_research_bundle(tmp_path)

    assert result.integrity_passed
    assert result.research_only
    assert result.current_implementation_matches
    assert result.verified_artifacts == 1


def test_stock_artifact_audit_rejects_tampering_and_untracked_files(tmp_path: Path) -> None:
    artifact = _bundle(tmp_path)
    artifact.write_text("tampered\n", encoding="utf-8")
    (tmp_path / "untracked.csv").write_text("not-bound\n", encoding="utf-8")

    result = audit_stock_research_bundle(tmp_path)

    assert not result.integrity_passed
    assert any("hash mismatch" in error for error in result.errors)
    assert any("Untracked files" in error for error in result.errors)


def test_stock_verification_cli_exits_nonzero_after_tampering(tmp_path: Path) -> None:
    artifact = _bundle(tmp_path)
    runner = CliRunner()

    valid = runner.invoke(app, ["research", "verify-stocks", "--reports", str(tmp_path)])
    artifact.write_text("tampered\n", encoding="utf-8")
    invalid = runner.invoke(app, ["research", "verify-stocks", "--reports", str(tmp_path)])

    assert valid.exit_code == 0
    assert '"integrity_passed": true' in valid.stdout
    assert invalid.exit_code == 2
    assert '"integrity_passed": false' in invalid.stdout


def _bundle(root: Path) -> Path:
    artifact = root / "metrics.csv"
    artifact.write_text("strategy,cagr\ncandidate,0.1\n", encoding="utf-8")
    manifest = {
        "research_status": "retrospective_candidate_not_live_approved",
        "automatic_order_placement": False,
        "implementation_sha256": implementation_sha256(),
        "input_fingerprints": {
            "ohlcv": "a" * 64,
            "membership": "b" * 64,
            "benchmark": "c" * 64,
            "cash_returns": "d" * 64,
            "terminal_return_overrides": "e" * 64,
        },
        "artifact_sha256": {artifact.name: file_sha256(artifact)},
    }
    (root / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return artifact
