from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from swing_trader.provenance import implementation_sha256
from swing_trader.shadow import verify_shadow_snapshot


@dataclass(frozen=True)
class OperationalAudit:
    integrity_passed: bool
    data_gate_passed: bool
    action_authorized: bool
    data_quality_status: str
    as_of_close: str
    mode: str
    panic_action: str
    hypothetical_actions: tuple[str, ...]
    shadow_records: int
    latest_shadow: str
    errors: tuple[str, ...]

    def markdown(self) -> str:
        integrity = "PASS" if self.integrity_passed else "FAIL"
        gate = "PASS" if self.data_gate_passed else "FAIL CLOSED"
        actions = ", ".join(self.hypothetical_actions) or "cash / no positions"
        lines = [
            "## Swing Trader operational audit",
            "",
            f"- Artifact integrity: **{integrity}**",
            f"- Independent data gate: **{gate}** (`{self.data_quality_status}`)",
            f"- Action authorized: **{str(self.action_authorized).lower()}**",
            f"- As-of close: **{self.as_of_close}**",
            f"- Mode: `{self.mode}`",
            f"- Capital-preservation comparator: **{self.panic_action}**",
            f"- Hypothetical holdings/actions: {actions}",
            f"- Verified locked shadow records: **{self.shadow_records}**",
            f"- Latest shadow: `{self.latest_shadow}`",
        ]
        if self.errors:
            lines.extend(["", "### Integrity errors", ""])
            lines.extend(f"- {error}" for error in self.errors)
        else:
            lines.extend(
                [
                    "",
                    "> This workflow is research-only. It cannot place an order, and a failed data gate authorizes no action.",
                ]
            )
        return "\n".join(lines) + "\n"


def audit_operational_artifacts(
    report_dir: Path,
    shadow_dir: Path,
    *,
    prices_path: Path | None = None,
    prices_manifest_path: Path | None = None,
) -> OperationalAudit:
    errors: list[str] = []
    manifest = _read_object(report_dir / "manifest.json", errors)
    decisions = _read_object(report_dir / "latest_decisions.json", errors)
    quality = _read_object(report_dir / "data_quality.json", errors)

    if prices_path is not None and prices_manifest_path is not None:
        prices_manifest = _read_object(prices_manifest_path, errors)
        try:
            prices_digest = hashlib.sha256(prices_path.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"Could not hash {prices_path.name}: {exc}")
            prices_digest = ""
        if prices_manifest and prices_manifest.get("sha256") != prices_digest:
            errors.append("Downloaded price file does not match its source manifest hash.")
        if quality and quality.get("primary_snapshot_sha256") != prices_digest:
            errors.append("Data-quality report does not bind the downloaded price-file hash.")

    if manifest:
        expected = manifest.get("specification_sha256")
        specification = manifest.get("specification")
        if isinstance(specification, dict):
            actual = hashlib.sha256(
                json.dumps(specification, sort_keys=True, default=list).encode()
            ).hexdigest()
        else:
            unsigned = {
                key: value for key, value in manifest.items() if key != "specification_sha256"
            }
            actual = hashlib.sha256(
                json.dumps(unsigned, sort_keys=True, default=list).encode()
            ).hexdigest()
        if expected != actual:
            errors.append("Manifest specification hash does not match its contents.")
        if manifest.get("implementation_sha256") != implementation_sha256():
            errors.append("Manifest implementation hash does not match the current package code.")

    gate = bool(quality.get("decision_data_gate_passed", False))
    quality_status = str(quality.get("status", "unknown"))
    action_authorized = bool(decisions.get("action_authorized", False))
    if manifest and bool(manifest.get("decision_data_gate_passed", False)) != gate:
        errors.append("Manifest and data-quality gate values disagree.")
    if decisions and bool(decisions.get("data_reconciled", False)) != gate:
        errors.append("Decision and data-quality gate values disagree.")
    if manifest and str(manifest.get("data_quality_status")) != quality_status:
        errors.append("Manifest and data-quality statuses disagree.")
    if decisions and str(decisions.get("data_quality_status")) != quality_status:
        errors.append("Decision and data-quality statuses disagree.")
    if manifest and bool(manifest.get("automatic_order_placement", True)):
        errors.append("Manifest must disable automatic order placement.")
    if action_authorized:
        errors.append("Research artifact unexpectedly authorizes an action.")
    if (
        not gate
        and decisions
        and decisions.get("mode") != "research_only_data_unreconciled_no_action"
    ):
        errors.append("Failed data gate is not represented by the no-action mode.")
    if manifest and decisions and manifest.get("data_end") != decisions.get("as_of_close"):
        errors.append("Manifest data end and decision as-of close disagree.")

    shadow_paths = sorted(shadow_dir.glob("*.json")) if shadow_dir.exists() else []
    if not shadow_paths:
        errors.append("No locked shadow records were found.")
    valid_shadows = 0
    for path in shadow_paths:
        try:
            valid = verify_shadow_snapshot(path)
        except (OSError, ValueError, json.JSONDecodeError):
            valid = False
        if not valid:
            errors.append(f"Shadow record failed hash verification: {path.name}")
        else:
            valid_shadows += 1
    latest_shadow = shadow_paths[-1] if shadow_paths else None
    if latest_shadow:
        latest_payload = _read_object(latest_shadow, errors)
        if manifest and latest_payload.get("specification_sha256") != manifest.get(
            "specification_sha256"
        ):
            errors.append("Latest shadow does not bind the current specification hash.")
        shadow_specification = latest_payload.get("specification")
        if latest_payload.get("schema_version") == 3 and isinstance(shadow_specification, dict):
            shadow_specification_hash = hashlib.sha256(
                json.dumps(shadow_specification, sort_keys=True, default=list).encode()
            ).hexdigest()
            if shadow_specification_hash != latest_payload.get("specification_sha256"):
                errors.append("Latest shadow's embedded specification hash is invalid.")
        if manifest and latest_payload.get("implementation_sha256") != manifest.get(
            "implementation_sha256"
        ):
            errors.append("Latest shadow does not bind the current implementation hash.")
        if manifest and latest_payload.get("recorded_from_manifest_utc") != manifest.get(
            "created_at_utc"
        ):
            errors.append("Latest shadow does not bind the current manifest timestamp.")

    overlay = decisions.get("capital_preservation_overlay", {})
    panic_action = (
        str(overlay.get("hypothetical_action", "UNKNOWN"))
        if isinstance(overlay, dict)
        else "UNKNOWN"
    )
    raw_actions = decisions.get("hypothetical_actions", [])
    action_labels: list[str] = []
    if isinstance(raw_actions, list):
        for row in raw_actions:
            if isinstance(row, dict):
                action_labels.append(f"{row.get('ticker', '?')} {row.get('action', '?')}")

    return OperationalAudit(
        integrity_passed=not errors,
        data_gate_passed=gate,
        action_authorized=action_authorized,
        data_quality_status=quality_status,
        as_of_close=str(decisions.get("as_of_close", "unknown")),
        mode=str(decisions.get("mode", "unknown")),
        panic_action=panic_action,
        hypothetical_actions=tuple(action_labels),
        shadow_records=valid_shadows,
        latest_shadow=latest_shadow.name if latest_shadow else "none",
        errors=tuple(errors),
    )


def _read_object(path: Path, errors: list[str]) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Could not read {path.name}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"Expected a JSON object in {path.name}.")
        return {}
    return value
