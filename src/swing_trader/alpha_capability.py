from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from swing_trader.alpha_validation import (
    ALPHA_DAILY_FREE_CALL_LIMIT,
    ResponseOpener,
    _reserve_calls,
)
from swing_trader.data import ALPHA_VANTAGE_URL


class AlphaCapabilityError(ValueError):
    """Raised when a redacted provider-capability probe cannot be completed safely."""


@dataclass(frozen=True)
class AlphaCapabilitySnapshot:
    path: Path
    endpoint: str
    status: str
    usable_on_free_tier: bool


def probe_alpha_shares_outstanding(
    api_key: str,
    output_dir: Path,
    *,
    quota_dir: Path,
    symbol: str = "MSFT",
    opener: ResponseOpener = urlopen,
    now: datetime | None = None,
) -> AlphaCapabilitySnapshot:
    """Test free-tier access without retaining credentials or provider message text."""
    if not api_key.strip():
        raise AlphaCapabilityError("ALPHA_VANTAGE_API_KEY is empty.")
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol or not normalized_symbol.replace("-", "").replace(".", "").isalnum():
        raise ValueError("Capability-probe symbol is invalid.")
    checked_at = _as_utc(now or datetime.now(UTC))
    _reserve_calls(
        quota_dir,
        checked_at.date(),
        1,
        daily_limit=ALPHA_DAILY_FREE_CALL_LIMIT,
        purpose=f"capability_probe:SHARES_OUTSTANDING:{normalized_symbol}",
    )
    query = urlencode(
        {
            "function": "SHARES_OUTSTANDING",
            "symbol": normalized_symbol,
            "datatype": "json",
            "apikey": api_key,
        }
    )
    request = Request(
        f"{ALPHA_VANTAGE_URL}?{query}",
        headers={"User-Agent": "swing-trader/0.1"},
    )
    try:
        with opener(request, timeout=30) as response:  # type: ignore[attr-defined]
            raw = response.read()  # type: ignore[attr-defined]
    except Exception:
        raise AlphaCapabilityError("Alpha Vantage capability request failed.") from None
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlphaCapabilityError("Alpha Vantage capability response was not JSON.") from exc
    if not isinstance(payload, dict):
        raise AlphaCapabilityError("Alpha Vantage capability response was not an object.")

    annual = payload.get("annualReports")
    quarterly = payload.get("quarterlyReports")
    annual_rows = len(annual) if isinstance(annual, list) else 0
    quarterly_rows = len(quarterly) if isinstance(quarterly, list) else 0
    provider_message = str(
        payload.get("Information") or payload.get("Note") or payload.get("Error Message") or ""
    ).lower()
    if annual_rows or quarterly_rows:
        status = "available"
        usable = True
    elif "premium" in provider_message:
        status = "premium_only"
        usable = False
    elif "rate limit" in provider_message or "25 requests" in provider_message:
        status = "daily_quota_limited"
        usable = False
    elif "api key" in provider_message:
        status = "key_rejected"
        usable = False
    elif provider_message:
        status = "provider_message_other"
        usable = False
    else:
        status = "empty_or_changed_schema"
        usable = False

    raw_hash = hashlib.sha256(raw).hexdigest()
    output: dict[str, object] = {
        "schema_version": 1,
        "record_type": "redacted_provider_capability_probe",
        "provider": "Alpha Vantage",
        "endpoint": "SHARES_OUTSTANDING",
        "symbol": normalized_symbol,
        "checked_at_utc": checked_at.isoformat(),
        "data_cost_policy": "no_paid_sources",
        "action_authorized": False,
        "raw_response_sha256": raw_hash,
        "response_top_level_keys": sorted(str(key) for key in payload),
        "annual_rows": annual_rows,
        "quarterly_rows": quarterly_rows,
        "status": status,
        "usable_on_free_tier": usable,
        "provider_message_retained": False,
        "interpretation": (
            "Capability evidence only. A successful response would not make provider data open, "
            "complete, point-in-time correct, or trading authority."
        ),
    }
    canonical = json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
    output["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = checked_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"alpha-shares-{stamp}-{raw_hash[:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    return AlphaCapabilitySnapshot(path, "SHARES_OUTSTANDING", status, usable)


def verify_alpha_capability_snapshot(path: Path) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return False
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
