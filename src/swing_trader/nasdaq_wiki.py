from __future__ import annotations

import hashlib
import json
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

from swing_trader.provenance import file_sha256

NASDAQ_WIKI_EXPORT_URL = "https://data.nasdaq.com/api/v3/datatables/WIKI/PRICES.json"
NASDAQ_WIKI_PRODUCT_URL = "https://data.nasdaq.com/databases/WIKIP"
WIKI_FINAL_SESSION = "2018-03-27"
WIKI_COLUMNS = (
    "ticker",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ex-dividend",
    "split_ratio",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "adj_volume",
)


class NasdaqWikiError(ValueError):
    """Raised when the public-domain WIKI archive cannot be verified safely."""


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...

    def iter_content(self, chunk_size: int) -> Iterable[bytes]: ...


Requester = Callable[..., _Response]


@dataclass(frozen=True)
class NasdaqWikiCapabilitySnapshot:
    path: Path
    status: str
    usable_with_configured_key: bool


@dataclass(frozen=True)
class NasdaqWikiArchiveSnapshot:
    archive_path: Path
    manifest_path: Path
    sha256: str


def probe_nasdaq_wiki_access(
    api_key: str,
    output_dir: Path | str,
    *,
    requester: Requester = requests.get,
    now: datetime | None = None,
) -> NasdaqWikiCapabilitySnapshot:
    """Probe one frozen WIKI row while retaining neither the key nor response data."""
    key = api_key.strip()
    if not key:
        raise NasdaqWikiError("NASDAQ_DATA_LINK_API_KEY is empty.")
    checked_at = _as_utc(now or datetime.now(UTC))
    try:
        response = requester(
            NASDAQ_WIKI_EXPORT_URL,
            params={
                "ticker": "AAPL",
                "date.gte": WIKI_FINAL_SESSION,
                "date.lte": WIKI_FINAL_SESSION,
                "qopts.columns": "ticker,date,adj_close",
                "qopts.per_page": 1,
            },
            headers={"x-api-token": key, "User-Agent": "swing-trader/0.1"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        raise NasdaqWikiError("Nasdaq Data Link WIKI capability request failed.") from None

    raw_fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    columns, row_count = _probe_shape(payload)
    expected = {"ticker", "date", "adj_close"}
    usable = expected.issubset(columns) and row_count >= 1
    status = "available" if usable else "empty_or_changed_schema"
    output: dict[str, object] = {
        "schema_version": 1,
        "record_type": "redacted_provider_capability_probe",
        "provider": "Nasdaq Data Link",
        "dataset": "WIKI/PRICES",
        "checked_at_utc": checked_at.isoformat(),
        "data_cost_policy": "no_paid_sources",
        "source_license_classification": "provider_states_public_domain",
        "source_ended": WIKI_FINAL_SESSION,
        "action_authorized": False,
        "raw_response_sha256": raw_fingerprint,
        "response_top_level_keys": (
            sorted(str(value) for value in payload) if isinstance(payload, dict) else []
        ),
        "response_columns": sorted(columns),
        "response_rows": row_count,
        "status": status,
        "usable_with_configured_key": usable,
        "credential_retained": False,
        "raw_rows_retained": False,
        "interpretation": (
            "Access evidence only. WIKI ends in 2018 and does not by itself prove PIT coverage, "
            "security identity, corporate-action completeness, or backtest readiness."
        ),
    }
    canonical = json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
    output["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = checked_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = root / f"nasdaq-wiki-{stamp}-{raw_fingerprint[:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    return NasdaqWikiCapabilitySnapshot(path, status, usable)


def verify_nasdaq_wiki_capability(path: Path | str) -> bool:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def download_nasdaq_wiki_archive(
    api_key: str,
    output: Path | str,
    *,
    requester: Requester = requests.get,
    sleep: Callable[[float], None] = time.sleep,
    poll_attempts: int = 12,
    poll_seconds: float = 10.0,
    now: datetime | None = None,
) -> NasdaqWikiArchiveSnapshot:
    """Download and hash the official WIKI bulk archive into an ignored local path."""
    key = api_key.strip()
    if not key:
        raise NasdaqWikiError("NASDAQ_DATA_LINK_API_KEY is empty.")
    if poll_attempts < 1 or poll_seconds < 0.0:
        raise ValueError("Polling configuration is invalid.")
    archive_path = Path(output)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = archive_path.with_suffix(archive_path.suffix + ".partial")
    if partial_path.exists():
        partial_path.unlink()

    link = _request_export_link(
        key,
        requester=requester,
        sleep=sleep,
        poll_attempts=poll_attempts,
        poll_seconds=poll_seconds,
    )
    _validate_export_link(link)
    try:
        response = requester(
            link,
            stream=True,
            headers={"User-Agent": "swing-trader/0.1"},
            timeout=(10, 120),
        )
        response.raise_for_status()
        with partial_path.open("xb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        archive_member, columns = _validate_archive(partial_path)
        partial_path.replace(archive_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise NasdaqWikiError("Nasdaq Data Link WIKI archive download failed validation.") from None

    captured_at = _as_utc(now or datetime.now(UTC))
    digest = file_sha256(archive_path)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "record_type": "nasdaq_wiki_raw_archive",
        "provider": "Nasdaq Data Link",
        "dataset": "WIKI/PRICES",
        "source_url": NASDAQ_WIKI_PRODUCT_URL,
        "captured_at_utc": captured_at.isoformat(),
        "source_ended": WIKI_FINAL_SESSION,
        "archive_file": archive_path.name,
        "archive_member": archive_member,
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": digest,
        "columns": list(columns),
        "source_license_classification": "provider_states_public_domain",
        "data_cost_policy": "no_paid_sources",
        "credential_retained": False,
        "raw_data_committable": False,
        "historical_backtest_ready": False,
        "action_authorized": False,
        "status": "raw_archive_locked_pending_identity_and_coverage_audit",
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    manifest_path = archive_path.with_suffix(archive_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return NasdaqWikiArchiveSnapshot(archive_path, manifest_path, digest)


def verify_nasdaq_wiki_archive(
    archive_path: Path | str,
    manifest_path: Path | str | None = None,
) -> bool:
    archive = Path(archive_path)
    manifest_file = (
        Path(manifest_path)
        if manifest_path is not None
        else archive.with_suffix(archive.suffix + ".manifest.json")
    )
    try:
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        expected_record = payload.pop("record_sha256")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        record_ok = hashlib.sha256(canonical).hexdigest() == expected_record
        member, columns = _validate_archive(archive)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        return False
    return bool(
        record_ok
        and payload.get("archive_file") == archive.name
        and payload.get("archive_member") == member
        and payload.get("archive_sha256") == file_sha256(archive)
        and payload.get("archive_bytes") == archive.stat().st_size
        and payload.get("columns") == list(columns)
        and payload.get("historical_backtest_ready") is False
        and payload.get("action_authorized") is False
    )


def _request_export_link(
    api_key: str,
    *,
    requester: Requester,
    sleep: Callable[[float], None],
    poll_attempts: int,
    poll_seconds: float,
) -> str:
    for attempt in range(poll_attempts):
        try:
            response = requester(
                NASDAQ_WIKI_EXPORT_URL,
                params={"qopts.export": "true"},
                headers={"x-api-token": api_key, "User-Agent": "swing-trader/0.1"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            payload = response.json()
            file_info = payload["datatable_bulk_download"]["file"]
            status = str(file_info["status"])
            link = file_info.get("link")
        except Exception:
            raise NasdaqWikiError("Nasdaq Data Link WIKI export request failed.") from None
        if status == "fresh" and isinstance(link, str) and link:
            return link
        if attempt + 1 < poll_attempts:
            sleep(poll_seconds)
    raise NasdaqWikiError("Nasdaq Data Link did not finish preparing the WIKI export.")


def _probe_shape(payload: object) -> tuple[set[str], int]:
    if not isinstance(payload, dict):
        return set(), 0
    datatable = payload.get("datatable")
    if not isinstance(datatable, dict):
        return set(), 0
    raw_columns = datatable.get("columns")
    data = datatable.get("data")
    if not isinstance(raw_columns, list) or not isinstance(data, list):
        return set(), 0
    columns = {
        str(column.get("name"))
        for column in raw_columns
        if isinstance(column, dict) and column.get("name") is not None
    }
    return columns, len(data)


def _validate_export_link(link: str) -> None:
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    allowed = (
        host == "data.nasdaq.com"
        or host.endswith(".nasdaq.com")
        or host.endswith(".quandl.com")
        or host.endswith(".amazonaws.com")
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise NasdaqWikiError("Nasdaq Data Link returned an unsafe archive link.")


def _validate_archive(path: Path) -> tuple[str, tuple[str, ...]]:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise NasdaqWikiError("WIKI archive must contain exactly one CSV file.")
        member = members[0]
        with archive.open(member) as handle:
            header = handle.readline().decode("utf-8-sig").strip()
    columns = tuple(header.split(","))
    if columns != WIKI_COLUMNS:
        raise NasdaqWikiError("WIKI archive columns do not match the documented schema.")
    return member, columns


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
