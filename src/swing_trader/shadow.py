from __future__ import annotations

import hashlib
import json
from pathlib import Path


def record_shadow_snapshot(report_dir: Path, shadow_dir: Path) -> Path:
    """Lock one non-overwriting prospective research snapshot."""
    manifest = _read_json(report_dir / "manifest.json")
    decisions = _read_json(report_dir / "latest_decisions.json")
    data_quality = _read_json(report_dir / "data_quality.json")
    payload = {
        "schema_version": 2,
        "recorded_from_manifest_utc": manifest["created_at_utc"],
        "system": manifest["system"],
        "specification_sha256": manifest["specification_sha256"],
        "implementation_sha256": manifest["implementation_sha256"],
        "research_status": manifest["research_status"],
        "action_authorized": decisions["action_authorized"],
        "data_quality_status": data_quality["status"],
        "decision": decisions,
        "data_quality": data_quality,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    timestamp = str(manifest["created_at_utc"]).replace("-", "").replace(":", "")
    timestamp = timestamp.replace("+0000", "Z").replace("+00:00", "Z")
    filename = f"{timestamp}-{payload['record_sha256'][:12]}.json"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    output = shadow_dir / filename
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return output


def verify_shadow_snapshot(path: Path) -> bool:
    payload = _read_json(path)
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload
