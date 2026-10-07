from __future__ import annotations

import hashlib
import io
import json
import warnings
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from io import StringIO
from pathlib import Path
from typing import Protocol

import pandas as pd
import requests

from swing_trader.provenance import file_sha256, tabular_sha256

WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PROSPECTIVE_UNIVERSE_ROLE = "prospective_current_universe_only_not_historical_backfill"
USER_AGENT = "swing-trader/0.1 research github.com/Jbultema/swing-trader"


class UniverseDataError(ValueError):
    """Raised when a current-universe source cannot support a safe snapshot."""


@dataclass(frozen=True)
class SourceDocument:
    url: str
    content: bytes
    fetched_at_utc: str
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class UniverseValidation:
    status: str
    rows: int
    minimum_rows: int
    maximum_rows: int
    duplicate_tickers: tuple[str, ...]
    invalid_tickers: tuple[str, ...]
    missing_required_fields: dict[str, int]
    sec_tickers_matched: int
    sec_match_fraction: float
    minimum_sec_match_fraction: float
    sec_reconciliation_status: str
    cik_mismatches: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class UniverseSnapshot:
    data_path: Path
    manifest_path: Path
    rows: int
    tabular_sha256: str
    validation: UniverseValidation


@dataclass(frozen=True)
class UniverseSnapshotAudit:
    integrity_passed: bool
    freshness_passed: bool
    age_hours: float
    status: str
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.integrity_passed and self.freshness_passed

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"passed": self.passed}


class DocumentFetcher(Protocol):
    def __call__(self, url: str) -> SourceDocument: ...


def fetch_source_document(url: str) -> SourceDocument:
    """Fetch a public source while recording the exact bytes and response metadata."""
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
        timeout=30,
    )
    response.raise_for_status()
    return SourceDocument(
        url=url,
        content=response.content,
        fetched_at_utc=datetime.now(UTC).isoformat(),
        content_type=response.headers.get("Content-Type"),
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )


def build_current_sp500_universe(
    wikipedia_html: bytes,
    sec_company_tickers_json: bytes | None,
) -> pd.DataFrame:
    """Normalize the currently observed S&P 500 roster and reconcile it to SEC CIKs.

    This function deliberately says nothing about membership before the source was
    observed. It is suitable for prospective research snapshots, never historical
    constituent backfills.
    """
    try:
        tables = pd.read_html(
            StringIO(wikipedia_html.decode("utf-8")),
            attrs={"id": "constituents"},
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise UniverseDataError("Could not parse the S&P 500 constituents table.") from exc
    if len(tables) != 1:
        raise UniverseDataError(f"Expected one constituents table; found {len(tables)}.")
    source = tables[0].copy()
    required_columns = {
        "Symbol",
        "Security",
        "GICS Sector",
        "GICS Sub-Industry",
        "Headquarters Location",
        "Date added",
        "CIK",
        "Founded",
    }
    if missing := required_columns - set(source.columns):
        raise UniverseDataError(f"Constituents table is missing columns: {sorted(missing)}")

    sec_by_ticker: dict[str, str] = {}
    if sec_company_tickers_json is not None:
        try:
            raw_sec = json.loads(sec_company_tickers_json)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UniverseDataError("Could not parse the SEC company-ticker mapping.") from exc
        if not isinstance(raw_sec, dict):
            raise UniverseDataError("SEC company-ticker payload must be a JSON object.")
        sec_rows = [row for row in raw_sec.values() if isinstance(row, dict)]
        if not sec_rows:
            raise UniverseDataError("SEC company-ticker payload contains no records.")
        for row in sec_rows:
            ticker = _canonical_ticker(row.get("ticker"))
            cik = _canonical_cik(row.get("cik_str"))
            if ticker and cik:
                sec_by_ticker[ticker] = cik

    ticker = source["Symbol"].map(_canonical_ticker)
    wikipedia_cik = source["CIK"].map(_canonical_cik)
    sec_cik = ticker.map(sec_by_ticker)
    frame = pd.DataFrame(
        {
            "ticker": ticker,
            "name": source["Security"].map(_clean_text),
            "gics_sector": source["GICS Sector"].map(_clean_text),
            "gics_sub_industry": source["GICS Sub-Industry"].map(_clean_text),
            "headquarters": source["Headquarters Location"].map(_clean_text),
            "date_added": pd.to_datetime(source["Date added"], errors="coerce"),
            "wikipedia_cik": wikipedia_cik,
            "sec_cik": sec_cik,
            "founded": source["Founded"].map(_clean_text),
        }
    )
    frame["cik"] = frame["sec_cik"].fillna(frame["wikipedia_cik"])
    frame["provider_ticker_yahoo"] = frame["ticker"].str.replace(".", "-", regex=False)
    columns = [
        "ticker",
        "name",
        "gics_sector",
        "gics_sub_industry",
        "headquarters",
        "date_added",
        "cik",
        "wikipedia_cik",
        "sec_cik",
        "founded",
        "provider_ticker_yahoo",
    ]
    return frame[columns].sort_values("ticker").reset_index(drop=True)


def validate_current_sp500_universe(
    frame: pd.DataFrame,
    *,
    minimum_rows: int = 490,
    maximum_rows: int = 510,
    minimum_sec_match_fraction: float = 0.99,
    require_sec_reconciliation: bool = False,
) -> UniverseValidation:
    required = ("ticker", "name", "gics_sector", "gics_sub_industry", "cik")
    if missing := set(required) - set(frame.columns):
        raise UniverseDataError(f"Normalized universe is missing columns: {sorted(missing)}")
    if not 0.0 <= minimum_sec_match_fraction <= 1.0:
        raise ValueError("minimum_sec_match_fraction must be between zero and one.")
    tickers = frame["ticker"].astype("string")
    duplicates = tuple(sorted(tickers[tickers.duplicated(keep=False)].dropna().unique()))
    ticker_pattern = r"^[A-Z0-9]+(?:\.[A-Z0-9]+)?$"
    invalid = tuple(sorted(tickers[~tickers.fillna("").str.fullmatch(ticker_pattern)].unique()))
    missing_fields = {
        field: int(frame[field].isna().sum() + frame[field].astype("string").str.strip().eq("").sum())
        for field in required
    }
    sec_matched = int(frame.get("sec_cik", pd.Series(index=frame.index, dtype="string")).notna().sum())
    sec_fraction = sec_matched / len(frame) if len(frame) else 0.0
    comparable = frame.dropna(subset=["wikipedia_cik", "sec_cik"])
    cik_mismatches = tuple(
        sorted(
            comparable.loc[
                comparable["wikipedia_cik"] != comparable["sec_cik"], "ticker"
            ].astype(str)
        )
    )
    if sec_matched == 0:
        sec_status = "unavailable"
    elif sec_fraction >= minimum_sec_match_fraction and not cik_mismatches:
        sec_status = "passed"
    else:
        sec_status = "failed"
    passed = (
        minimum_rows <= len(frame) <= maximum_rows
        and not duplicates
        and not invalid
        and not any(missing_fields.values())
        and sec_status != "failed"
        and (not require_sec_reconciliation or sec_status == "passed")
    )
    return UniverseValidation(
        status="passed" if passed else "failed",
        rows=len(frame),
        minimum_rows=minimum_rows,
        maximum_rows=maximum_rows,
        duplicate_tickers=duplicates,
        invalid_tickers=invalid,
        missing_required_fields=missing_fields,
        sec_tickers_matched=sec_matched,
        sec_match_fraction=sec_fraction,
        minimum_sec_match_fraction=minimum_sec_match_fraction,
        sec_reconciliation_status=sec_status,
        cik_mismatches=cik_mismatches,
    )


def download_current_sp500_snapshot(
    output_dir: Path,
    *,
    fetcher: DocumentFetcher = fetch_source_document,
    captured_at: datetime | None = None,
) -> UniverseSnapshot:
    """Fetch, validate, and lock one non-overwriting current-universe snapshot."""
    wikipedia = fetcher(WIKIPEDIA_SP500_URL)
    sec: SourceDocument | None = None
    sec_fetch_error: str | None = None
    try:
        sec = fetcher(SEC_COMPANY_TICKERS_URL)
    except requests.RequestException as exc:
        sec_fetch_error = f"{type(exc).__name__}: {exc}"
    frame = build_current_sp500_universe(
        wikipedia.content,
        None if sec is None else sec.content,
    )
    return write_current_sp500_snapshot(
        frame,
        wikipedia,
        sec,
        output_dir,
        captured_at=captured_at,
        sec_fetch_error=sec_fetch_error,
    )


def write_current_sp500_snapshot(
    frame: pd.DataFrame,
    wikipedia: SourceDocument,
    sec: SourceDocument | None,
    output_dir: Path,
    *,
    captured_at: datetime | None = None,
    sec_fetch_error: str | None = None,
) -> UniverseSnapshot:
    validation = validate_current_sp500_universe(frame)
    if not validation.passed:
        raise UniverseDataError(
            "Current S&P 500 universe failed validation: "
            + json.dumps(validation.to_dict(), sort_keys=True)
        )
    observed_at = _as_utc(captured_at or datetime.now(UTC))
    normalized = frame.copy()
    normalized["observed_at_utc"] = observed_at.isoformat()
    normalized["universe_role"] = PROSPECTIVE_UNIVERSE_ROLE
    fingerprint = tabular_sha256(normalized)
    stamp = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"sp500-current-{stamp}-{fingerprint[:12]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / f"{stem}.parquet"
    manifest_path = output_dir / f"{stem}.manifest.json"

    buffer = io.BytesIO()
    normalized.to_parquet(buffer, index=False)
    with data_path.open("xb") as handle:
        handle.write(buffer.getvalue())
    try:
        manifest = {
            "schema_version": 1,
            "universe": "S&P 500",
            "data_cost_policy": "no_paid_sources",
            "universe_role": PROSPECTIVE_UNIVERSE_ROLE,
            "historical_backfill_authorized": False,
            "action_authorized": False,
            "captured_at_utc": observed_at.isoformat(),
            "observed_on_utc_date": observed_at.date().isoformat(),
            "data_file": data_path.name,
            "rows": len(normalized),
            "tabular_sha256": fingerprint,
            "data_file_sha256": file_sha256(data_path),
            "validation": validation.to_dict(),
            "universe_data_gate_passed": validation.passed,
            "sources": {
                "wikipedia_current_constituents": _source_manifest(wikipedia),
                "sec_company_tickers": (
                    _source_manifest(sec)
                    if sec is not None
                    else {
                        "url": SEC_COMPANY_TICKERS_URL,
                        "status": "unavailable",
                        "error": sec_fetch_error or "SEC source was not supplied.",
                    }
                ),
            },
            "pitindex_reference": _pitindex_diagnostic(normalized, observed_at),
        }
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
    except Exception:
        data_path.unlink(missing_ok=True)
        raise
    return UniverseSnapshot(data_path, manifest_path, len(normalized), fingerprint, validation)


def audit_current_sp500_snapshot(
    manifest_path: Path,
    *,
    max_age_hours: float = 48.0,
    now: datetime | None = None,
) -> UniverseSnapshotAudit:
    errors: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return UniverseSnapshotAudit(False, False, float("inf"), "unreadable_manifest", (str(exc),))
    if not isinstance(manifest, dict):
        return UniverseSnapshotAudit(
            False, False, float("inf"), "invalid_manifest", ("Manifest must be an object.",)
        )
    data_name = manifest.get("data_file")
    if not isinstance(data_name, str) or Path(data_name).name != data_name:
        errors.append("Manifest data_file must be a local filename.")
        data_path = manifest_path.parent / "__invalid__"
    else:
        data_path = manifest_path.parent / data_name
    if not data_path.exists():
        errors.append("Snapshot data file is missing.")
    elif file_sha256(data_path) != manifest.get("data_file_sha256"):
        errors.append("Snapshot data-file hash does not match the manifest.")
    else:
        try:
            frame = pd.read_parquet(data_path)
            if tabular_sha256(frame) != manifest.get("tabular_sha256"):
                errors.append("Snapshot table fingerprint does not match the manifest.")
            if len(frame) != manifest.get("rows"):
                errors.append("Snapshot row count does not match the manifest.")
        except Exception as exc:
            errors.append(f"Snapshot data file is unreadable: {exc}")
    if manifest.get("universe_role") != PROSPECTIVE_UNIVERSE_ROLE:
        errors.append("Snapshot universe role is not prospective-only.")
    if manifest.get("historical_backfill_authorized") is not False:
        errors.append("Snapshot does not explicitly prohibit historical backfill.")
    try:
        captured = datetime.fromisoformat(str(manifest["captured_at_utc"]))
        captured = _as_utc(captured)
        checked_at = _as_utc(now or datetime.now(UTC))
        age_hours = (checked_at - captured).total_seconds() / 3600.0
    except (KeyError, TypeError, ValueError):
        age_hours = float("inf")
        errors.append("Snapshot capture time is invalid.")
    freshness = 0.0 <= age_hours <= max_age_hours
    integrity = not errors
    status = "passed" if integrity and freshness else "failed"
    if integrity and not freshness:
        status = "stale"
    return UniverseSnapshotAudit(integrity, freshness, age_hours, status, tuple(errors))


def latest_current_sp500_manifest(output_dir: Path) -> Path:
    manifests = sorted(output_dir.glob("sp500-current-*.manifest.json"))
    if not manifests:
        raise FileNotFoundError(f"No current S&P 500 snapshots found in {output_dir}.")
    return manifests[-1]


def _pitindex_diagnostic(frame: pd.DataFrame, observed_at: datetime) -> dict[str, object]:
    """Use bundled pitindex data only as a non-authoritative reconciliation signal."""
    try:
        import pitindex

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=pitindex.StaleDataWarning)
            info = pitindex.info("sp500")
            reference_date = min(observed_at.date(), pd.Timestamp(info["end_date"]).date())
            reference = pitindex.get_constituents(reference_date, "sp500")
        observed = set(frame["ticker"].astype(str))
        expected = {_canonical_ticker(value) for value in reference["ticker"]}
        expected.discard("")
        union = observed | expected
        symmetric = observed ^ expected
        return {
            "status": "stale_diagnostic_only" if bool(info.get("is_stale")) else "diagnostic_only",
            "package_version": version("pitindex"),
            "reference_date": reference_date.isoformat(),
            "build_timestamp_utc": info.get("build_timestamp_utc"),
            "data_age_days_at_capture": info.get("data_age_days"),
            "is_stale_at_capture": bool(info.get("is_stale")),
            "reference_rows": len(expected),
            "overlap_rows": len(observed & expected),
            "symmetric_difference_rows": len(symmetric),
            "symmetric_difference_fraction": len(symmetric) / len(union) if union else 1.0,
            "observed_only": sorted(observed - expected),
            "reference_only": sorted(expected - observed),
            "authoritative_for_snapshot": False,
        }
    except Exception as exc:
        return {
            "status": "unavailable_diagnostic",
            "error": f"{type(exc).__name__}: {exc}",
            "authoritative_for_snapshot": False,
        }


def _source_manifest(document: SourceDocument) -> dict[str, object]:
    return {
        "url": document.url,
        "fetched_at_utc": document.fetched_at_utc,
        "sha256": document.sha256,
        "content_type": document.content_type,
        "etag": document.etag,
        "last_modified": document.last_modified,
    }


def _canonical_ticker(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip().upper().replace("-", ".")


def _canonical_cik(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(10) if text.isdigit() else None


def _clean_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Snapshot timestamps must be timezone-aware.")
    return value.astimezone(UTC)
