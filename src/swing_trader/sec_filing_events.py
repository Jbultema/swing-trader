from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from swing_trader.provenance import file_sha256, tabular_sha256
from swing_trader.stock_candidates import verify_candidate_snapshot
from swing_trader.stock_live_data import (
    audit_current_stock_price_snapshot,
    load_locked_current_universe,
)

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DEFAULT_SEC_USER_AGENT = (
    "swing-trader/0.1 research contact https://github.com/Jbultema/swing-trader"
)
SEC_EVENT_ROLE = (
    "experimental_public_filing_metadata_context_not_sentiment_or_directional_signal"
)
SEC_AVAILABILITY = "edgar_acceptance_timestamp_use_only_if_at_or_before_snapshot_capture"
TRACKED_FORMS = frozenset(
    {
        "8-K",
        "8-K/A",
        "10-Q",
        "10-Q/A",
        "10-K",
        "10-K/A",
        "4",
        "4/A",
        "6-K",
        "6-K/A",
        "SC 13D",
        "SC 13D/A",
        "SC 13G",
        "SC 13G/A",
    }
)
RECENT_FIELDS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "act",
    "form",
    "fileNumber",
    "filmNumber",
    "items",
    "size",
    "isXBRL",
    "isInlineXBRL",
    "primaryDocument",
    "primaryDocDescription",
)


class SecFilingEventError(ValueError):
    """Raised when public SEC metadata cannot support a causal event snapshot."""


@dataclass(frozen=True)
class SecSourceDocument:
    cik: str
    tickers: tuple[str, ...]
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
class SecSubmissionValidation:
    status: str
    cik: str
    tickers: tuple[str, ...]
    payload_cik_matches: bool
    payload_tickers_match: bool
    recent_rows: int
    tracked_rows_in_lookback: int
    duplicate_accessions: tuple[str, ...]
    invalid_accession_rows: int
    invalid_acceptance_rows: int
    excluded_after_capture_rows: int
    excluded_before_lookback_rows: int

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SecEventSnapshotValidation:
    status: str
    requested_tickers: int
    source_ciks: int
    source_gates_passed: int
    source_gates_total: int
    event_rows: int
    lookback_calendar_days: int
    capture_time_utc: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SecEventSnapshot:
    data_path: Path
    manifest_path: Path
    tickers: int
    rows: int
    validation: SecEventSnapshotValidation


@dataclass(frozen=True)
class SecEventAudit:
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


SecFetcher = Callable[[str, tuple[str, ...], str], SecSourceDocument]


def fetch_sec_submission(
    cik: str,
    tickers: tuple[str, ...],
    user_agent: str = DEFAULT_SEC_USER_AGENT,
    *,
    attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> SecSourceDocument:
    """Fetch one issuer's keyless public submissions JSON with a declared contact."""
    normalized_cik = _canonical_cik(cik)
    declared_agent = user_agent.strip()
    if not declared_agent or ("@" not in declared_agent and "http" not in declared_agent.lower()):
        raise SecFilingEventError(
            "SEC_USER_AGENT must declare the application and an email or URL contact."
        )
    if attempts < 1:
        raise ValueError("attempts must be positive.")
    url = SEC_SUBMISSIONS_URL.format(cik=normalized_cik)
    last_error: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": declared_agent,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=30,
            )
            response.raise_for_status()
            return SecSourceDocument(
                cik=normalized_cik,
                tickers=tuple(tickers),
                url=url,
                content=response.content,
                fetched_at_utc=datetime.now(UTC).isoformat(),
                content_type=response.headers.get("Content-Type"),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
        except requests.RequestException as exc:
            last_error = exc
            status = getattr(exc.response, "status_code", None)
            if status is not None and status < 500:
                break
            if attempt + 1 < attempts and retry_delay_seconds > 0:
                sleep(retry_delay_seconds * (attempt + 1))
    raise SecFilingEventError(f"SEC submissions are unavailable for CIK {normalized_cik}: {last_error}")


def parse_sec_submission(
    document: SecSourceDocument,
    *,
    captured_at: datetime,
    lookback_calendar_days: int = 90,
) -> tuple[pd.DataFrame, SecSubmissionValidation]:
    """Validate current submissions metadata and retain only causally available tracked forms."""
    if lookback_calendar_days < 1:
        raise ValueError("lookback_calendar_days must be positive.")
    capture = _as_utc(captured_at)
    try:
        payload = json.loads(document.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecFilingEventError("SEC submissions payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise SecFilingEventError("SEC submissions payload must be an object.")
    payload_cik = _canonical_cik(payload.get("cik"))
    payload_tickers_raw = payload.get("tickers")
    if not isinstance(payload_tickers_raw, list):
        raise SecFilingEventError("SEC submissions payload has no ticker list.")
    payload_tickers = {_canonical_ticker(value) for value in payload_tickers_raw}
    requested_tickers = tuple(_canonical_ticker(value) for value in document.tickers)
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise SecFilingEventError("SEC submissions payload has no recent-filings object.")
    missing = set(RECENT_FIELDS) - set(recent)
    if missing:
        raise SecFilingEventError(f"SEC recent-filings data is missing fields: {sorted(missing)}")
    lengths = {
        field: len(recent[field]) if isinstance(recent[field], list) else -1
        for field in RECENT_FIELDS
    }
    if len(set(lengths.values())) != 1 or next(iter(lengths.values())) < 0:
        raise SecFilingEventError(
            "SEC recent-filings arrays have inconsistent lengths: " + json.dumps(lengths)
        )
    raw = pd.DataFrame({field: recent[field] for field in RECENT_FIELDS})
    accepted = pd.to_datetime(raw["acceptanceDateTime"], utc=True, errors="coerce")
    accession = raw["accessionNumber"].astype("string").str.strip()
    duplicates = tuple(sorted(accession[accession.duplicated(keep=False)].dropna().unique()))
    invalid_accession = ~accession.str.fullmatch(r"\d{10}-\d{2}-\d{6}", na=False)
    invalid_acceptance = accepted.isna()
    payload_cik_matches = payload_cik == _canonical_cik(document.cik)
    payload_tickers_match = set(requested_tickers).issubset(payload_tickers)

    form = raw["form"].astype("string").str.strip()
    tracked = form.isin(TRACKED_FORMS)
    after_capture = tracked & accepted.gt(pd.Timestamp(capture))
    lookback_start = pd.Timestamp(capture - timedelta(days=lookback_calendar_days))
    before_lookback = tracked & accepted.lt(lookback_start)
    retained = tracked & accepted.le(pd.Timestamp(capture)) & accepted.ge(lookback_start)
    company_name = str(payload.get("name") or "").strip()
    rows: list[pd.DataFrame] = []
    for ticker in requested_tickers:
        subset = raw.loc[retained].copy()
        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "cik": payload_cik,
                    "company_name": company_name,
                    "accession_number": subset["accessionNumber"].astype("string").str.strip(),
                    "form": subset["form"].astype("string").str.strip(),
                    "filing_date": pd.to_datetime(subset["filingDate"], errors="coerce"),
                    "report_date": pd.to_datetime(subset["reportDate"], errors="coerce"),
                    "accepted_at_utc": accepted.loc[retained],
                    "act": subset["act"].astype("string").str.strip(),
                    "items": subset["items"].astype("string").str.strip(),
                    "filing_size_bytes": pd.to_numeric(subset["size"], errors="coerce").astype(
                        "Int64"
                    ),
                    "is_xbrl": subset["isXBRL"].map(_as_bool).astype("boolean"),
                    "is_inline_xbrl": subset["isInlineXBRL"].map(_as_bool).astype("boolean"),
                    "primary_document": subset["primaryDocument"].astype("string").str.strip(),
                    "primary_document_description": subset["primaryDocDescription"]
                    .astype("string")
                    .str.strip(),
                },
                index=subset.index,
            )
        )
    normalized = pd.concat(rows, ignore_index=True) if rows else _empty_event_frame()
    if not normalized.empty:
        normalized["filing_url"] = normalized.apply(
            lambda row: _filing_url(
                str(row["cik"]),
                str(row["accession_number"]),
                str(row["primary_document"]),
            ),
            axis=1,
        )
    else:
        normalized["filing_url"] = pd.Series(dtype="string")
    normalized["availability_role"] = SEC_AVAILABILITY
    normalized = normalized.sort_values(
        ["accepted_at_utc", "ticker", "accession_number"]
    ).reset_index(drop=True)
    passed = (
        payload_cik_matches
        and payload_tickers_match
        and not duplicates
        and not bool(invalid_accession.any())
        and not bool(invalid_acceptance.any())
    )
    validation = SecSubmissionValidation(
        status="passed" if passed else "failed",
        cik=_canonical_cik(document.cik),
        tickers=requested_tickers,
        payload_cik_matches=payload_cik_matches,
        payload_tickers_match=payload_tickers_match,
        recent_rows=len(raw),
        tracked_rows_in_lookback=int(retained.sum()),
        duplicate_accessions=duplicates,
        invalid_accession_rows=int(invalid_accession.sum()),
        invalid_acceptance_rows=int(invalid_acceptance.sum()),
        excluded_after_capture_rows=int(after_capture.sum()),
        excluded_before_lookback_rows=int(before_lookback.sum()),
    )
    return normalized, validation


def download_candidate_sec_events(
    candidate_path: Path,
    universe_manifest_path: Path,
    price_manifest_path: Path,
    output_dir: Path,
    *,
    user_agent: str = DEFAULT_SEC_USER_AGENT,
    lookback_calendar_days: int = 90,
    request_delay_seconds: float = 0.15,
    fetcher: SecFetcher = fetch_sec_submission,
    now: datetime | None = None,
) -> SecEventSnapshot:
    """Lock official SEC filing metadata for the exact candidate/holding shortlist."""
    recorded_at = _as_utc(now or datetime.now(UTC))
    if not verify_candidate_snapshot(candidate_path):
        raise SecFilingEventError("Candidate snapshot failed its content-hash check.")
    candidate = _read_json(candidate_path)
    candidate_inputs = candidate.get("inputs")
    if not isinstance(candidate_inputs, dict):
        raise SecFilingEventError("Candidate snapshot has no input bindings.")
    if file_sha256(universe_manifest_path) != candidate_inputs.get("universe_manifest_sha256"):
        raise SecFilingEventError("Candidate snapshot is not bound to the supplied universe.")
    if file_sha256(price_manifest_path) != candidate_inputs.get("price_manifest_sha256"):
        raise SecFilingEventError("Candidate snapshot is not bound to the supplied prices.")
    symbols = candidate.get("validation_symbols")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(x, str) for x in symbols):
        raise SecFilingEventError("Candidate snapshot has no valid validation-symbol list.")
    universe, universe_manifest = load_locked_current_universe(
        universe_manifest_path,
        now=recorded_at,
    )
    price_audit = audit_current_stock_price_snapshot(
        price_manifest_path,
        universe_manifest_path=universe_manifest_path,
        now=recorded_at,
    )
    if not price_audit.passed:
        raise SecFilingEventError(
            f"Bound stock-price snapshot failed its gate: {json.dumps(price_audit.to_dict())}"
        )
    price_manifest = _read_json(price_manifest_path)
    by_ticker = universe.set_index("ticker")["cik"].astype(str)
    tracked_tickers = tuple(
        dict.fromkeys(_canonical_ticker(value) for value in symbols if value in by_ticker.index)
    )
    if not tracked_tickers:
        raise SecFilingEventError("Candidate shortlist has no SEC-mapped issuers.")
    by_cik: dict[str, list[str]] = {}
    for ticker in tracked_tickers:
        cik = _canonical_cik(by_ticker.loc[ticker])
        by_cik.setdefault(cik, []).append(ticker)
    documents: list[SecSourceDocument] = []
    for position, (cik, tickers) in enumerate(sorted(by_cik.items())):
        if position and request_delay_seconds > 0:
            sleep(request_delay_seconds)
        documents.append(fetcher(cik, tuple(sorted(tickers)), user_agent))
    bindings = {
        "candidate_file": candidate_path.name,
        "candidate_file_sha256": file_sha256(candidate_path),
        "candidate_record_sha256": candidate.get("record_sha256"),
        "price_manifest": price_manifest_path.name,
        "price_manifest_sha256": file_sha256(price_manifest_path),
        "price_tabular_sha256": price_manifest.get("tabular_sha256"),
        "universe_manifest": universe_manifest_path.name,
        "universe_manifest_sha256": file_sha256(universe_manifest_path),
        "universe_tabular_sha256": universe_manifest.get("tabular_sha256"),
    }
    return write_sec_event_snapshot(
        documents,
        tracked_tickers,
        output_dir,
        captured_at=recorded_at,
        lookback_calendar_days=lookback_calendar_days,
        bindings=bindings,
    )


def write_sec_event_snapshot(
    documents: Iterable[SecSourceDocument],
    requested_tickers: Iterable[str],
    output_dir: Path,
    *,
    captured_at: datetime,
    lookback_calendar_days: int = 90,
    bindings: Mapping[str, object] | None = None,
) -> SecEventSnapshot:
    capture = _as_utc(captured_at)
    sources = tuple(sorted(documents, key=lambda value: value.cik))
    tickers = tuple(dict.fromkeys(_canonical_ticker(value) for value in requested_tickers))
    if not sources or not tickers:
        raise SecFilingEventError("At least one SEC source and requested ticker are required.")
    if len({source.cik for source in sources}) != len(sources):
        raise SecFilingEventError("SEC source CIKs must be unique.")
    source_tickers = {ticker for source in sources for ticker in source.tickers}
    if source_tickers != set(tickers):
        raise SecFilingEventError("SEC source ticker coverage does not match the request.")
    parsed: list[pd.DataFrame] = []
    validations: list[SecSubmissionValidation] = []
    source_manifests: list[dict[str, object]] = []
    for document in sources:
        frame, validation = parse_sec_submission(
            document,
            captured_at=capture,
            lookback_calendar_days=lookback_calendar_days,
        )
        validations.append(validation)
        source_manifests.append(_source_manifest(document, validation))
        if not validation.passed:
            raise SecFilingEventError(
                "SEC source failed validation: " + json.dumps(validation.to_dict(), sort_keys=True)
            )
        frame["source_file_sha256"] = document.sha256
        parsed.append(frame)
    nonempty = [frame for frame in parsed if not frame.empty]
    normalized = pd.concat(nonempty, ignore_index=True) if nonempty else _empty_event_frame()
    normalized = normalized.sort_values(
        ["accepted_at_utc", "ticker", "accession_number"]
    ).reset_index(drop=True)
    source_passed = sum(validation.passed for validation in validations)
    passed = source_passed == len(sources) and source_tickers == set(tickers)
    validation = SecEventSnapshotValidation(
        status="passed" if passed else "failed",
        requested_tickers=len(tickers),
        source_ciks=len(sources),
        source_gates_passed=source_passed,
        source_gates_total=len(sources),
        event_rows=len(normalized),
        lookback_calendar_days=lookback_calendar_days,
        capture_time_utc=capture.isoformat(),
    )
    if not validation.passed:
        raise SecFilingEventError(
            "SEC event snapshot failed validation: "
            + json.dumps(validation.to_dict(), sort_keys=True)
        )
    fingerprint = tabular_sha256(normalized)
    stamp = capture.strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"sec-events-{stamp}-{fingerprint[:12]}"
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
            "record_type": "candidate_bound_sec_filing_event_snapshot",
            "provider": "U.S. SEC EDGAR submissions API",
            "provider_url_template": SEC_SUBMISSIONS_URL,
            "data_cost_policy": "no_paid_sources",
            "access_classification": "official_public_keyless_free_to_access_and_reuse",
            "data_role": SEC_EVENT_ROLE,
            "availability_assumption": SEC_AVAILABILITY,
            "tracked_forms": sorted(TRACKED_FORMS),
            "content_sentiment_interpretation_authorized": False,
            "directional_interpretation_authorized": False,
            "candidate_ranking_input": False,
            "portfolio_state_input": False,
            "action_authorized": False,
            "captured_at_utc": capture.isoformat(),
            "data_file": data_path.name,
            "rows": len(normalized),
            "tabular_sha256": fingerprint,
            "data_file_sha256": file_sha256(data_path),
            "requested_tickers": list(tickers),
            "validation": validation.to_dict(),
            "event_data_gate_passed": validation.passed,
            "candidate_diagnostics": build_sec_event_diagnostics(
                normalized,
                tickers,
                captured_at=capture,
            ),
            "inputs": dict(bindings or {}),
            "sources": source_manifests,
            "limitations": [
                "Acceptance metadata establishes availability, not positive or negative meaning.",
                "Submissions metadata does not encode Form 4 transaction direction or magnitude.",
                "A filing count is not an earnings surprise and contains no analyst expectation.",
                "This snapshot is experimental context and cannot alter a portfolio target.",
            ],
        }
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
    except Exception:
        data_path.unlink(missing_ok=True)
        raise
    return SecEventSnapshot(data_path, manifest_path, len(tickers), len(normalized), validation)


def build_sec_event_diagnostics(
    frame: pd.DataFrame,
    requested_tickers: Iterable[str],
    *,
    captured_at: datetime,
) -> list[dict[str, object]]:
    """Summarize timestamped events without inferring sentiment or return direction."""
    capture = pd.Timestamp(_as_utc(captured_at))
    rows: list[dict[str, object]] = []
    for ticker in requested_tickers:
        group = frame.loc[frame["ticker"].eq(ticker)].sort_values("accepted_at_utc")
        accepted = pd.to_datetime(group["accepted_at_utc"], utc=True)
        age_days = (capture - accepted).dt.total_seconds().div(86_400.0)
        form = group["form"].astype("string")
        items = group["items"].astype("string")
        latest = group.iloc[-1] if not group.empty else None
        rows.append(
            {
                "ticker": ticker,
                "tracked_filings_1_calendar_day": int(age_days.le(1.0).sum()),
                "tracked_filings_5_calendar_days": int(age_days.le(5.0).sum()),
                "tracked_filings_21_calendar_days": int(age_days.le(21.0).sum()),
                "tracked_filings_90_calendar_days": int(age_days.le(90.0).sum()),
                "earnings_release_8k_21d": int(
                    (age_days.le(21.0) & form.str.startswith("8-K") & items.str.contains("2.02")).sum()
                ),
                "material_agreement_8k_21d": int(
                    (age_days.le(21.0) & form.str.startswith("8-K") & items.str.contains("1.01")).sum()
                ),
                "acquisition_disposition_8k_21d": int(
                    (age_days.le(21.0) & form.str.startswith("8-K") & items.str.contains("2.01")).sum()
                ),
                "management_change_8k_21d": int(
                    (age_days.le(21.0) & form.str.startswith("8-K") & items.str.contains("5.02")).sum()
                ),
                "reg_fd_8k_21d": int(
                    (age_days.le(21.0) & form.str.startswith("8-K") & items.str.contains("7.01")).sum()
                ),
                "form4_metadata_21d": int((age_days.le(21.0) & form.isin(["4", "4/A"])).sum()),
                "latest_form": None if latest is None else str(latest["form"]),
                "latest_accepted_at_utc": (
                    None
                    if latest is None
                    else pd.Timestamp(latest["accepted_at_utc"]).isoformat()
                ),
                "latest_acceptance_market_phase": (
                    None
                    if latest is None
                    else _market_phase(pd.Timestamp(latest["accepted_at_utc"]))
                ),
                "latest_filing_url": None if latest is None else str(latest["filing_url"]),
                "interpretation": "event_metadata_only_no_sentiment_or_directional_claim",
            }
        )
    return rows


def latest_sec_event_manifest(output_dir: Path) -> Path:
    paths = sorted(output_dir.glob("sec-events-*.manifest.json"))
    if not paths:
        raise FileNotFoundError(f"No SEC event snapshots found in {output_dir}.")
    return paths[-1]


def sec_events_for_candidate(output_dir: Path, candidate_path: Path) -> Path | None:
    """Return an intact SEC diagnostic already bound to the exact candidate bytes."""
    candidate_hash = file_sha256(candidate_path)
    for path in sorted(output_dir.glob("sec-events-*.manifest.json"), reverse=True):
        try:
            manifest = _read_json(path)
        except (OSError, json.JSONDecodeError, SecFilingEventError):
            continue
        inputs = manifest.get("inputs")
        if not isinstance(inputs, dict) or inputs.get("candidate_file_sha256") != candidate_hash:
            continue
        audit = audit_sec_event_snapshot(
            path,
            candidate_path=candidate_path,
            max_age_hours=float("inf"),
        )
        if audit.integrity_passed:
            return path
    return None


def audit_sec_event_snapshot(
    manifest_path: Path,
    *,
    candidate_path: Path | None = None,
    price_manifest_path: Path | None = None,
    universe_manifest_path: Path | None = None,
    max_age_hours: float = 48.0,
    now: datetime | None = None,
) -> SecEventAudit:
    errors: list[str] = []
    try:
        manifest = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError, SecFilingEventError) as exc:
        return SecEventAudit(False, False, float("inf"), "unreadable_manifest", (str(exc),))
    data_name = manifest.get("data_file")
    if not isinstance(data_name, str) or Path(data_name).name != data_name:
        errors.append("Manifest data_file must be a local filename.")
        data_path = manifest_path.parent / "__invalid__"
    else:
        data_path = manifest_path.parent / data_name
    if not data_path.exists():
        errors.append("SEC event data file is missing.")
    elif file_sha256(data_path) != manifest.get("data_file_sha256"):
        errors.append("SEC event data-file hash does not match the manifest.")
    else:
        try:
            frame = pd.read_parquet(data_path)
            if tabular_sha256(frame) != manifest.get("tabular_sha256"):
                errors.append("SEC event table fingerprint does not match the manifest.")
            if len(frame) != manifest.get("rows"):
                errors.append("SEC event row count does not match the manifest.")
            sources = manifest.get("sources")
            if not isinstance(sources, list) or not all(isinstance(row, dict) for row in sources):
                errors.append("SEC source manifests are invalid.")
            elif not frame.empty:
                manifest_hashes = {str(row.get("sha256")) for row in sources}
                table_hashes = set(frame["source_file_sha256"].astype(str))
                if not table_hashes.issubset(manifest_hashes):
                    errors.append("SEC source hashes do not match the normalized table.")
            if not frame.empty:
                capture = pd.Timestamp(str(manifest["captured_at_utc"]))
                accepted = pd.to_datetime(frame["accepted_at_utc"], utc=True)
                if accepted.gt(capture).any():
                    errors.append("SEC event table contains a filing accepted after capture.")
        except Exception as exc:
            errors.append(f"SEC event data file is unreadable: {exc}")
    if manifest.get("data_role") != SEC_EVENT_ROLE:
        errors.append("SEC event role is missing or unsafe.")
    for key, label in (
        ("content_sentiment_interpretation_authorized", "content sentiment"),
        ("directional_interpretation_authorized", "directional interpretation"),
        ("candidate_ranking_input", "candidate ranking"),
        ("portfolio_state_input", "portfolio state"),
        ("action_authorized", "action"),
    ):
        if manifest.get(key) is not False:
            errors.append(f"SEC event manifest does not explicitly prohibit {label}.")
    if manifest.get("event_data_gate_passed") is not True:
        errors.append("Original SEC event data gate did not pass.")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
        errors.append("SEC event input bindings are invalid.")
    for path, key, label in (
        (candidate_path, "candidate_file_sha256", "Candidate"),
        (price_manifest_path, "price_manifest_sha256", "Price manifest"),
        (universe_manifest_path, "universe_manifest_sha256", "Universe manifest"),
    ):
        if path is not None and (not path.exists() or file_sha256(path) != inputs.get(key)):
            errors.append(f"{label} hash does not match the SEC event snapshot.")
    try:
        captured = _as_utc(datetime.fromisoformat(str(manifest["captured_at_utc"])))
        checked_at = _as_utc(now or datetime.now(UTC))
        age_hours = (checked_at - captured).total_seconds() / 3600.0
    except (KeyError, TypeError, ValueError):
        age_hours = float("inf")
        errors.append("SEC event capture time is invalid.")
    freshness = 0.0 <= age_hours <= max_age_hours
    integrity = not errors
    status = "passed" if integrity and freshness else "failed"
    if integrity and not freshness:
        status = "stale"
    return SecEventAudit(integrity, freshness, age_hours, status, tuple(errors))


def _source_manifest(
    document: SecSourceDocument,
    validation: SecSubmissionValidation,
) -> dict[str, object]:
    return {
        "cik": document.cik,
        "tickers": list(document.tickers),
        "url": document.url,
        "fetched_at_utc": document.fetched_at_utc,
        "sha256": document.sha256,
        "content_type": document.content_type,
        "etag": document.etag,
        "last_modified": document.last_modified,
        "validation": validation.to_dict(),
    }


def _empty_event_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(dtype="string"),
            "cik": pd.Series(dtype="string"),
            "company_name": pd.Series(dtype="string"),
            "accession_number": pd.Series(dtype="string"),
            "form": pd.Series(dtype="string"),
            "filing_date": pd.Series(dtype="datetime64[ns]"),
            "report_date": pd.Series(dtype="datetime64[ns]"),
            "accepted_at_utc": pd.Series(dtype="datetime64[ns, UTC]"),
            "act": pd.Series(dtype="string"),
            "items": pd.Series(dtype="string"),
            "filing_size_bytes": pd.Series(dtype="Int64"),
            "is_xbrl": pd.Series(dtype="boolean"),
            "is_inline_xbrl": pd.Series(dtype="boolean"),
            "primary_document": pd.Series(dtype="string"),
            "primary_document_description": pd.Series(dtype="string"),
            "filing_url": pd.Series(dtype="string"),
            "availability_role": pd.Series(dtype="string"),
            "source_file_sha256": pd.Series(dtype="string"),
        }
    )


def _filing_url(cik: str, accession: str, primary_document: str) -> str:
    cik_path = str(int(_canonical_cik(cik)))
    accession_path = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_path}/"
        f"{accession_path}/{primary_document}"
    )


def _market_phase(value: pd.Timestamp) -> str:
    eastern = value.tz_convert(ZoneInfo("America/New_York"))
    minute = eastern.hour * 60 + eastern.minute
    if minute < 9 * 60 + 30:
        return "pre_market"
    if minute < 16 * 60:
        return "regular_session"
    return "after_market"


def _canonical_ticker(value: object) -> str:
    return str(value).strip().upper().replace("-", ".")


def _canonical_cik(value: object) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if not re.fullmatch(r"\d{1,10}", text):
        raise SecFilingEventError(f"Invalid SEC CIK: {value!r}")
    return text.zfill(10)


def _as_bool(value: object) -> bool | None:
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    return None


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SecFilingEventError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
