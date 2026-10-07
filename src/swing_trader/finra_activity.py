from __future__ import annotations

import io
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from time import sleep

import numpy as np
import pandas as pd
import requests

from swing_trader.provenance import file_sha256, tabular_sha256
from swing_trader.stock_candidates import verify_candidate_snapshot
from swing_trader.stock_live_data import audit_current_stock_price_snapshot

FINRA_DAILY_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{session}.txt"
FINRA_ACTIVITY_ROLE = (
    "experimental_off_exchange_short_sale_activity_not_short_interest_or_directional_signal"
)
FINRA_AVAILABILITY = "published_after_close_use_no_earlier_than_next_regular_session_open"
FINRA_HEADER = (
    "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"
)
USER_AGENT = "swing-trader/0.1 research github.com/Jbultema/swing-trader"


class FinraActivityError(ValueError):
    """Raised when FINRA activity data cannot support a causal research snapshot."""


@dataclass(frozen=True)
class FinraSourceDocument:
    session: date
    url: str
    content: bytes
    fetched_at_utc: str
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def sha256(self) -> str:
        import hashlib

        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class FinraFileValidation:
    status: str
    session: str
    rows: int
    declared_rows: int
    minimum_rows: int
    duplicate_symbols: tuple[str, ...]
    wrong_session_rows: int
    invalid_symbol_rows: int
    nonfinite_volume_rows: int
    negative_volume_rows: int
    zero_total_volume_rows: int
    short_volume_above_total_rows: int
    short_exempt_above_short_rows: int

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FinraSnapshotValidation:
    status: str
    sessions: int
    minimum_sessions: int
    rows: int
    requested_tickers: int
    latest_session: str
    latest_matched_tickers: int
    latest_coverage_fraction: float
    minimum_latest_coverage_fraction: float
    latest_missing_tickers: tuple[str, ...]
    source_file_gates_passed: int
    source_file_gates_total: int

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FinraActivitySnapshot:
    data_path: Path
    manifest_path: Path
    sessions: int
    rows: int
    validation: FinraSnapshotValidation


@dataclass(frozen=True)
class FinraActivityAudit:
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


FinraFetcher = Callable[[date], FinraSourceDocument]


def fetch_finra_source_document(
    session: date,
    *,
    attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> FinraSourceDocument:
    """Fetch one completed-session consolidated FINRA daily file without credentials."""
    if attempts < 1:
        raise ValueError("attempts must be positive.")
    url = FINRA_DAILY_URL.format(session=session.strftime("%Y%m%d"))
    last_error: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
                timeout=30,
            )
            response.raise_for_status()
            return FinraSourceDocument(
                session=session,
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
    raise FinraActivityError(f"FINRA daily file is unavailable for {session}: {last_error}")


def parse_finra_short_volume_file(
    content: bytes,
    expected_session: date,
    *,
    minimum_rows: int = 1_000,
) -> tuple[pd.DataFrame, FinraFileValidation]:
    """Parse and validate one consolidated file, including its declared trailer count."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FinraActivityError("FINRA daily file is not UTF-8 text.") from exc
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or lines[0] != FINRA_HEADER:
        raise FinraActivityError("FINRA daily file header or row structure is invalid.")
    if not lines[-1].isdigit():
        raise FinraActivityError("FINRA daily file is missing its integer trailer count.")
    declared_rows = int(lines[-1])
    try:
        raw = pd.read_csv(
            io.StringIO("\n".join(lines[:-1])),
            sep="|",
            dtype="string",
            keep_default_na=False,
        )
    except (pd.errors.ParserError, ValueError) as exc:
        raise FinraActivityError("FINRA daily file could not be parsed.") from exc
    expected_columns = [
        "Date",
        "Symbol",
        "ShortVolume",
        "ShortExemptVolume",
        "TotalVolume",
        "Market",
    ]
    if list(raw.columns) != expected_columns:
        raise FinraActivityError("FINRA daily file columns do not match the documented schema.")

    normalized = pd.DataFrame(
        {
            "session": pd.to_datetime(raw["Date"], format="%Y%m%d", errors="coerce"),
            "finra_symbol": raw["Symbol"].str.strip(),
            "market": raw["Market"].str.strip(),
        }
    )
    for output, source in (
        ("short_volume", "ShortVolume"),
        ("short_exempt_volume", "ShortExemptVolume"),
        ("reported_total_volume", "TotalVolume"),
    ):
        normalized[output] = pd.to_numeric(raw[source], errors="coerce").astype(float)
    normalized["ticker"] = normalized["finra_symbol"].str.replace("/", ".", regex=False)
    normalized = normalized[
        [
            "session",
            "ticker",
            "finra_symbol",
            "short_volume",
            "short_exempt_volume",
            "reported_total_volume",
            "market",
        ]
    ]
    validation = validate_finra_short_volume_frame(
        normalized,
        expected_session,
        declared_rows=declared_rows,
        minimum_rows=minimum_rows,
    )
    return normalized, validation


def validate_finra_short_volume_frame(
    frame: pd.DataFrame,
    expected_session: date,
    *,
    declared_rows: int,
    minimum_rows: int = 1_000,
) -> FinraFileValidation:
    required = {
        "session",
        "ticker",
        "finra_symbol",
        "short_volume",
        "short_exempt_volume",
        "reported_total_volume",
        "market",
    }
    if missing := required - set(frame.columns):
        raise FinraActivityError(f"FINRA normalized data is missing columns: {sorted(missing)}")
    symbols = frame["finra_symbol"].astype("string")
    duplicates = tuple(sorted(symbols[symbols.duplicated(keep=False)].dropna().unique()))
    invalid_symbol = symbols.isna() | symbols.str.strip().eq("") | symbols.str.contains(
        r"[|\r\n\s]", regex=True, na=True
    )
    dates = pd.to_datetime(frame["session"], errors="coerce")
    wrong_session = dates.isna() | dates.dt.date.ne(expected_session)
    volumes = frame[["short_volume", "short_exempt_volume", "reported_total_volume"]]
    numeric = volumes.apply(pd.to_numeric, errors="coerce")
    finite = np.isfinite(numeric.to_numpy(dtype=float)).all(axis=1)
    negative = numeric.lt(0.0).any(axis=1)
    zero_total = numeric["reported_total_volume"].le(0.0)
    short_above_total = numeric["short_volume"].gt(numeric["reported_total_volume"] + 1e-9)
    exempt_above_short = numeric["short_exempt_volume"].gt(numeric["short_volume"] + 1e-9)
    passed = (
        len(frame) >= minimum_rows
        and len(frame) == declared_rows
        and not duplicates
        and not bool(invalid_symbol.any())
        and not bool(wrong_session.any())
        and bool(finite.all())
        and not bool(negative.any())
        and not bool(zero_total.any())
        and not bool(short_above_total.any())
        and not bool(exempt_above_short.any())
    )
    return FinraFileValidation(
        status="passed" if passed else "failed",
        session=expected_session.isoformat(),
        rows=len(frame),
        declared_rows=declared_rows,
        minimum_rows=minimum_rows,
        duplicate_symbols=duplicates,
        wrong_session_rows=int(wrong_session.sum()),
        invalid_symbol_rows=int(invalid_symbol.sum()),
        nonfinite_volume_rows=int((~finite).sum()),
        negative_volume_rows=int(negative.sum()),
        zero_total_volume_rows=int(zero_total.sum()),
        short_volume_above_total_rows=int(short_above_total.sum()),
        short_exempt_above_short_rows=int(exempt_above_short.sum()),
    )


def download_candidate_finra_activity(
    candidate_path: Path,
    universe_manifest_path: Path,
    price_manifest_path: Path,
    output_dir: Path,
    *,
    lookback_sessions: int = 21,
    fetcher: FinraFetcher = fetch_finra_source_document,
    now: datetime | None = None,
) -> FinraActivitySnapshot:
    """Lock candidate-bound FINRA context without allowing it to alter portfolio targets."""
    recorded_at = _as_utc(now or datetime.now(UTC))
    if lookback_sessions < 5:
        raise ValueError("FINRA activity needs at least five completed sessions.")
    if not verify_candidate_snapshot(candidate_path):
        raise FinraActivityError("Candidate snapshot failed its content-hash check.")
    candidate = _read_json(candidate_path)
    candidate_inputs = candidate.get("inputs")
    if not isinstance(candidate_inputs, dict):
        raise FinraActivityError("Candidate snapshot has no input bindings.")
    if file_sha256(universe_manifest_path) != candidate_inputs.get("universe_manifest_sha256"):
        raise FinraActivityError("Candidate snapshot is not bound to the supplied universe.")
    if file_sha256(price_manifest_path) != candidate_inputs.get("price_manifest_sha256"):
        raise FinraActivityError("Candidate snapshot is not bound to the supplied prices.")
    symbols = candidate.get("validation_symbols")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(x, str) for x in symbols):
        raise FinraActivityError("Candidate snapshot has no valid validation-symbol list.")
    price_audit = audit_current_stock_price_snapshot(
        price_manifest_path,
        universe_manifest_path=universe_manifest_path,
        now=recorded_at,
    )
    if not price_audit.passed:
        raise FinraActivityError(
            f"Bound stock-price snapshot failed its gate: {json.dumps(price_audit.to_dict())}"
        )
    price_manifest = _read_json(price_manifest_path)
    data_path = price_manifest_path.parent / str(price_manifest["data_file"])
    sessions = pd.DatetimeIndex(pd.read_parquet(data_path, columns=[]).index).tz_localize(None)
    as_of = pd.Timestamp(str(candidate.get("as_of_session")))
    eligible = sessions[sessions <= as_of]
    if eligible.empty or eligible[-1] != as_of:
        raise FinraActivityError("Candidate session is not the latest bound price session.")
    selected = tuple(timestamp.date() for timestamp in eligible[-lookback_sessions:])
    if len(selected) < lookback_sessions:
        raise FinraActivityError("Bound prices do not contain the requested FINRA lookback.")
    documents = tuple(fetcher(session) for session in selected)
    bindings = {
        "candidate_file": candidate_path.name,
        "candidate_file_sha256": file_sha256(candidate_path),
        "candidate_record_sha256": candidate.get("record_sha256"),
        "price_manifest": price_manifest_path.name,
        "price_manifest_sha256": file_sha256(price_manifest_path),
        "price_tabular_sha256": price_manifest.get("tabular_sha256"),
        "universe_manifest": universe_manifest_path.name,
        "universe_manifest_sha256": file_sha256(universe_manifest_path),
    }
    return write_finra_activity_snapshot(
        documents,
        tuple(symbols),
        output_dir,
        captured_at=recorded_at,
        bindings=bindings,
        minimum_sessions=lookback_sessions,
    )


def write_finra_activity_snapshot(
    documents: Iterable[FinraSourceDocument],
    requested_tickers: Iterable[str],
    output_dir: Path,
    *,
    captured_at: datetime,
    bindings: Mapping[str, object] | None = None,
    minimum_sessions: int = 5,
    minimum_latest_coverage_fraction: float = 1.0,
) -> FinraActivitySnapshot:
    observed_at = _as_utc(captured_at)
    sources = tuple(sorted(documents, key=lambda value: value.session))
    tickers = tuple(dict.fromkeys(str(value).strip().upper() for value in requested_tickers))
    if not sources or not tickers:
        raise FinraActivityError("At least one FINRA source and requested ticker are required.")
    if len({source.session for source in sources}) != len(sources):
        raise FinraActivityError("FINRA source sessions must be unique.")
    parsed: list[pd.DataFrame] = []
    file_validations: list[FinraFileValidation] = []
    source_manifests: list[dict[str, object]] = []
    for document in sources:
        frame, validation = parse_finra_short_volume_file(document.content, document.session)
        file_validations.append(validation)
        source_manifests.append(_source_manifest(document, validation))
        if not validation.passed:
            raise FinraActivityError(
                "FINRA source failed validation: " + json.dumps(validation.to_dict(), sort_keys=True)
            )
        frame["source_file_sha256"] = document.sha256
        parsed.append(frame)
    combined = pd.concat(parsed, ignore_index=True)
    normalized = combined.loc[combined["ticker"].isin(tickers)].copy()
    normalized["short_sale_volume_fraction"] = normalized["short_volume"].div(
        normalized["reported_total_volume"]
    )
    normalized["short_exempt_volume_fraction"] = normalized["short_exempt_volume"].div(
        normalized["reported_total_volume"]
    )
    normalized["data_role"] = FINRA_ACTIVITY_ROLE
    normalized = normalized.sort_values(["session", "ticker"]).reset_index(drop=True)
    latest = sources[-1].session
    latest_tickers = set(
        normalized.loc[normalized["session"].dt.date == latest, "ticker"].astype(str)
    )
    missing_latest = tuple(sorted(set(tickers) - latest_tickers))
    latest_coverage = len(latest_tickers & set(tickers)) / len(tickers)
    source_passed = sum(validation.passed for validation in file_validations)
    valid_fractions = bool(
        normalized["short_sale_volume_fraction"].between(0.0, 1.0).all()
        and normalized["short_exempt_volume_fraction"].between(0.0, 1.0).all()
    )
    passed = (
        len(sources) >= minimum_sessions
        and not normalized.empty
        and normalized["session"].nunique() == len(sources)
        and source_passed == len(sources)
        and latest_coverage >= minimum_latest_coverage_fraction
        and valid_fractions
    )
    validation = FinraSnapshotValidation(
        status="passed" if passed else "failed",
        sessions=len(sources),
        minimum_sessions=minimum_sessions,
        rows=len(normalized),
        requested_tickers=len(tickers),
        latest_session=latest.isoformat(),
        latest_matched_tickers=len(latest_tickers & set(tickers)),
        latest_coverage_fraction=latest_coverage,
        minimum_latest_coverage_fraction=minimum_latest_coverage_fraction,
        latest_missing_tickers=missing_latest,
        source_file_gates_passed=source_passed,
        source_file_gates_total=len(sources),
    )
    if not validation.passed:
        raise FinraActivityError(
            "FINRA activity snapshot failed validation: "
            + json.dumps(validation.to_dict(), sort_keys=True)
        )

    fingerprint = tabular_sha256(normalized)
    stamp = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"finra-activity-{latest.strftime('%Y%m%d')}-{stamp}-{fingerprint[:12]}"
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
            "record_type": "candidate_bound_finra_activity_snapshot",
            "provider": "FINRA Reg SHO consolidated daily short sale volume file",
            "provider_url_template": FINRA_DAILY_URL,
            "data_cost_policy": "no_paid_sources",
            "access_classification": "public_keyless_not_asserted_open_license",
            "data_role": FINRA_ACTIVITY_ROLE,
            "availability_assumption": FINRA_AVAILABILITY,
            "directional_interpretation_authorized": False,
            "short_interest_interpretation_authorized": False,
            "candidate_ranking_input": False,
            "portfolio_state_input": False,
            "action_authorized": False,
            "captured_at_utc": observed_at.isoformat(),
            "first_session": sources[0].session.isoformat(),
            "latest_session": latest.isoformat(),
            "data_file": data_path.name,
            "rows": len(normalized),
            "tabular_sha256": fingerprint,
            "data_file_sha256": file_sha256(data_path),
            "requested_tickers": list(tickers),
            "validation": validation.to_dict(),
            "activity_data_gate_passed": validation.passed,
            "candidate_diagnostics": build_finra_activity_diagnostics(normalized),
            "inputs": dict(bindings or {}),
            "sources": source_manifests,
            "limitations": [
                "FINRA-reported off-exchange trades only; exchange volume is not consolidated here.",
                "Daily short sale volume is not short interest and is not a bearish-position measure.",
                "Market-making, hedging, and same-day covering can produce short-sale volume.",
                "This snapshot is experimental context and cannot alter a portfolio target.",
            ],
        }
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
    except Exception:
        data_path.unlink(missing_ok=True)
        raise
    return FinraActivitySnapshot(data_path, manifest_path, len(sources), len(normalized), validation)


def build_finra_activity_diagnostics(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Summarize unusual participation without assigning bullish or bearish meaning."""
    rows: list[dict[str, object]] = []
    for ticker, group in frame.sort_values("session").groupby("ticker", sort=True):
        ratio = group["short_sale_volume_fraction"].astype(float)
        latest = float(ratio.iloc[-1])
        mean_5 = float(ratio.tail(5).mean())
        history = ratio.tail(20)
        mean_20 = float(history.mean())
        standard_deviation = float(history.std(ddof=1)) if len(history) > 1 else math.nan
        z_score = (
            (latest - mean_20) / standard_deviation
            if math.isfinite(standard_deviation) and standard_deviation > 0.0
            else None
        )
        rows.append(
            {
                "ticker": str(ticker),
                "sessions": len(group),
                "latest_session": pd.Timestamp(group["session"].iloc[-1]).date().isoformat(),
                "latest_short_sale_volume_fraction": latest,
                "mean_5_session_fraction": mean_5,
                "mean_20_session_fraction": mean_20,
                "latest_vs_20_session_z_score": z_score,
                "interpretation": "activity_context_only_no_directional_claim",
            }
        )
    return rows


def latest_finra_activity_manifest(output_dir: Path) -> Path:
    paths = sorted(output_dir.glob("finra-activity-*.manifest.json"))
    if not paths:
        raise FileNotFoundError(f"No FINRA activity snapshots found in {output_dir}.")
    return paths[-1]


def finra_activity_for_candidate(
    output_dir: Path,
    candidate_path: Path,
) -> Path | None:
    """Return an intact diagnostic already bound to the exact candidate bytes."""
    candidate_hash = file_sha256(candidate_path)
    for path in sorted(output_dir.glob("finra-activity-*.manifest.json"), reverse=True):
        try:
            manifest = _read_json(path)
        except (OSError, json.JSONDecodeError, FinraActivityError):
            continue
        inputs = manifest.get("inputs")
        if not isinstance(inputs, dict) or inputs.get("candidate_file_sha256") != candidate_hash:
            continue
        audit = audit_finra_activity_snapshot(
            path,
            candidate_path=candidate_path,
            max_age_hours=float("inf"),
        )
        if audit.integrity_passed:
            return path
    return None


def audit_finra_activity_snapshot(
    manifest_path: Path,
    *,
    candidate_path: Path | None = None,
    price_manifest_path: Path | None = None,
    universe_manifest_path: Path | None = None,
    max_age_hours: float = 48.0,
    now: datetime | None = None,
) -> FinraActivityAudit:
    errors: list[str] = []
    try:
        manifest = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError, FinraActivityError) as exc:
        return FinraActivityAudit(False, False, float("inf"), "unreadable_manifest", (str(exc),))
    data_name = manifest.get("data_file")
    if not isinstance(data_name, str) or Path(data_name).name != data_name:
        errors.append("Manifest data_file must be a local filename.")
        data_path = manifest_path.parent / "__invalid__"
    else:
        data_path = manifest_path.parent / data_name
    if not data_path.exists():
        errors.append("FINRA activity data file is missing.")
    elif file_sha256(data_path) != manifest.get("data_file_sha256"):
        errors.append("FINRA activity data-file hash does not match the manifest.")
    else:
        try:
            frame = pd.read_parquet(data_path)
            if tabular_sha256(frame) != manifest.get("tabular_sha256"):
                errors.append("FINRA activity table fingerprint does not match the manifest.")
            if len(frame) != manifest.get("rows"):
                errors.append("FINRA activity row count does not match the manifest.")
            sources = manifest.get("sources")
            if not isinstance(sources, list) or not all(isinstance(row, dict) for row in sources):
                errors.append("FINRA source manifests are invalid.")
            else:
                manifest_hashes = {str(row.get("sha256")) for row in sources}
                table_hashes = set(frame["source_file_sha256"].astype(str))
                if table_hashes != manifest_hashes:
                    errors.append("FINRA source hashes do not match the normalized table.")
                manifest_sessions = {str(row.get("session")) for row in sources}
                table_sessions = {
                    pd.Timestamp(value).date().isoformat() for value in frame["session"].unique()
                }
                if table_sessions != manifest_sessions:
                    errors.append("FINRA source sessions do not match the normalized table.")
        except Exception as exc:
            errors.append(f"FINRA activity data file is unreadable: {exc}")
    if manifest.get("data_role") != FINRA_ACTIVITY_ROLE:
        errors.append("FINRA activity role is missing or unsafe.")
    if manifest.get("candidate_ranking_input") is not False:
        errors.append("FINRA activity is not explicitly excluded from candidate ranking.")
    if manifest.get("portfolio_state_input") is not False:
        errors.append("FINRA activity is not explicitly excluded from portfolio state.")
    if manifest.get("action_authorized") is not False:
        errors.append("FINRA activity does not explicitly prohibit action.")
    if manifest.get("activity_data_gate_passed") is not True:
        errors.append("Original FINRA activity data gate did not pass.")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
        errors.append("FINRA activity input bindings are invalid.")
    for path, key, label in (
        (candidate_path, "candidate_file_sha256", "Candidate"),
        (price_manifest_path, "price_manifest_sha256", "Price manifest"),
        (universe_manifest_path, "universe_manifest_sha256", "Universe manifest"),
    ):
        if path is not None and (not path.exists() or file_sha256(path) != inputs.get(key)):
            errors.append(f"{label} hash does not match the FINRA activity snapshot.")
    try:
        captured = _as_utc(datetime.fromisoformat(str(manifest["captured_at_utc"])))
        checked_at = _as_utc(now or datetime.now(UTC))
        age_hours = (checked_at - captured).total_seconds() / 3600.0
    except (KeyError, TypeError, ValueError):
        age_hours = float("inf")
        errors.append("FINRA activity capture time is invalid.")
    freshness = 0.0 <= age_hours <= max_age_hours
    integrity = not errors
    status = "passed" if integrity and freshness else "failed"
    if integrity and not freshness:
        status = "stale"
    return FinraActivityAudit(integrity, freshness, age_hours, status, tuple(errors))


def _source_manifest(
    document: FinraSourceDocument,
    validation: FinraFileValidation,
) -> dict[str, object]:
    return {
        "session": document.session.isoformat(),
        "url": document.url,
        "fetched_at_utc": document.fetched_at_utc,
        "sha256": document.sha256,
        "content_type": document.content_type,
        "etag": document.etag,
        "last_modified": document.last_modified,
        "validation": validation.to_dict(),
    }


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FinraActivityError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
