from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from swing_trader.provenance import file_sha256, implementation_sha256


@dataclass(frozen=True)
class StockArtifactAudit:
    integrity_passed: bool
    research_only: bool
    current_implementation_matches: bool
    verified_artifacts: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def audit_stock_research_bundle(report_dir: Path | str) -> StockArtifactAudit:
    """Verify a stock report before a CLI or dashboard presents its evidence."""
    root = Path(report_dir)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Could not read stock manifest: {exc}")
        manifest = {}
    if not isinstance(manifest, dict):
        errors.append("Stock manifest must be a JSON object.")
        manifest = {}

    research_only = (
        manifest.get("research_status") == "retrospective_candidate_not_live_approved"
        and manifest.get("automatic_order_placement") is False
    )
    if not research_only:
        errors.append("Stock report does not preserve research-only authority.")

    recorded_implementation = manifest.get("implementation_sha256")
    current_matches = recorded_implementation == implementation_sha256()
    if not current_matches:
        errors.append("Stock report implementation hash does not match the current package code.")

    expected = manifest.get("artifact_sha256")
    verified = 0
    expected_names: set[str] = set()
    if not isinstance(expected, dict) or not expected:
        errors.append("Stock manifest has no artifact hash map.")
    else:
        for raw_name, raw_digest in expected.items():
            name = str(raw_name)
            if Path(name).name != name:
                errors.append(f"Unsafe stock artifact name in manifest: {name}")
                continue
            expected_names.add(name)
            path = root / name
            if not path.is_file():
                errors.append(f"Stock artifact is missing: {name}")
                continue
            actual = file_sha256(path)
            if not isinstance(raw_digest, str) or actual != raw_digest:
                errors.append(f"Stock artifact hash mismatch: {name}")
                continue
            verified += 1

    actual_names = (
        {path.name for path in root.iterdir() if path.is_file()} if root.exists() else set()
    )
    unexpected = sorted(actual_names - expected_names - {"manifest.json"})
    if unexpected:
        errors.append(f"Untracked files exist in the stock report bundle: {unexpected}")

    inputs = manifest.get("input_fingerprints")
    required_inputs = {"ohlcv", "membership", "benchmark", "cash_returns"}
    if not isinstance(inputs, dict) or not required_inputs.issubset(inputs):
        errors.append("Stock manifest does not bind every required research input.")
    elif any(
        not isinstance(inputs[name], str) or len(inputs[name]) != 64 for name in required_inputs
    ):
        errors.append("Stock manifest contains an invalid research-input fingerprint.")
    if isinstance(inputs, dict) and inputs.get("terminal_return_overrides") is None:
        warnings.append("No terminal-return override table was supplied to this stock run.")

    return StockArtifactAudit(
        integrity_passed=not errors,
        research_only=research_only,
        current_implementation_matches=current_matches,
        verified_artifacts=verified,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
