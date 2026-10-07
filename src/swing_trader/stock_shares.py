from __future__ import annotations

import io
import json
import math
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import sleep

import pandas as pd
import yfinance as yf

from swing_trader.provenance import file_sha256, tabular_sha256
from swing_trader.stock_live_data import load_locked_current_universe

SHARES_PROVIDER_ROLE = "research_only_unofficial_endpoint_prospective_snapshot"
SHARES_DATA_ROLE = "captured_now_for_future_use_only_not_historical_backfill"
REQUIRED_SHARE_COLUMNS = (
    "ticker",
    "shares_outstanding",
    "provider_observation_date",
    "captured_at_utc",
    "source_status",
    "source_observation_count",
    "discarded_historical_observations",
    "provider_request_attempts",
)
ALLOWED_SOURCE_STATUSES = frozenset({"available", "missing", "fetch_error", "invalid"})


class StockShareDataError(ValueError):
    """Raised when a prospective shares-outstanding snapshot is malformed."""


@dataclass(frozen=True)
class StockShareValidation:
    status: str
    requested_tickers: int
    usable_tickers: int
    coverage_fraction: float
    minimum_coverage_fraction: float
    maximum_observation_age_calendar_days: int
    oldest_provider_observation_date: str | None
    newest_provider_observation_date: str | None
    missing_tickers: tuple[str, ...]
    fetch_error_tickers: tuple[str, ...]
    invalid_tickers: tuple[str, ...]
    stale_tickers: tuple[str, ...]
    future_observation_tickers: tuple[str, ...]
    inconsistent_source_tickers: tuple[str, ...]
    duplicate_tickers: tuple[str, ...]
    unexpected_tickers: tuple[str, ...]
    absent_roster_tickers: tuple[str, ...]
    source_observations_received: int
    discarded_historical_observations: int

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StockShareSnapshot:
    data_path: Path
    manifest_path: Path
    rows: int
    validation: StockShareValidation


@dataclass(frozen=True)
class StockShareSnapshotAudit:
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


ShareFetcher = Callable[[str, date, date], object]


def download_current_stock_shares(
    universe_manifest_path: Path,
    output_dir: Path,
    *,
    share_fetcher: ShareFetcher | None = None,
    now: datetime | None = None,
    minimum_coverage_fraction: float = 0.99,
    maximum_observation_age_calendar_days: int = 130,
    request_lookback_calendar_days: int = 400,
    max_workers: int = 4,
    retry_attempts: int = 2,
    retry_delay_seconds: float = 1.0,
    sleeper: Callable[[float], None] = sleep,
) -> StockShareSnapshot:
    """Capture one currently known shares-outstanding value per current constituent.

    Yahoo may return a dated history. Those dates are retained only as provider metadata:
    every value becomes usable no earlier than ``captured_at_utc``. Historical rows are
    deliberately discarded rather than treated as point-in-time observations.
    """
    requested_at = _as_utc(now or datetime.now(UTC))
    if request_lookback_calendar_days <= maximum_observation_age_calendar_days:
        raise ValueError("Request lookback must exceed the maximum allowed observation age.")
    if max_workers < 1:
        raise ValueError("max_workers must be positive.")
    if retry_attempts < 1:
        raise ValueError("retry_attempts must be positive.")
    universe, universe_manifest = load_locked_current_universe(
        universe_manifest_path,
        now=requested_at,
    )
    tickers = tuple(str(value) for value in universe["ticker"])
    if len(tickers) != len(set(tickers)):
        raise StockShareDataError("Current universe contains duplicate tickers.")
    provider_symbols = (
        tuple(str(value) for value in universe["provider_ticker_yahoo"])
        if "provider_ticker_yahoo" in universe.columns
        else tuple(ticker.replace(".", "-") for ticker in tickers)
    )
    fetcher = share_fetcher or _fetch_yahoo_shares
    request_start = requested_at.date() - timedelta(days=request_lookback_calendar_days)
    request_end = requested_at.date() + timedelta(days=1)
    rows: dict[str, dict[str, object]] = {}

    def collect(ticker: str, provider_symbol: str) -> tuple[str, dict[str, object]]:
        return ticker, _collect_one_share_value(
            provider_symbol,
            fetcher,
            request_start=request_start,
            request_end=request_end,
            retry_attempts=retry_attempts,
            retry_delay_seconds=retry_delay_seconds,
            sleeper=sleeper,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(collect, ticker, provider): ticker
            for ticker, provider in zip(tickers, provider_symbols, strict=True)
        }
        for future in as_completed(futures):
            ticker, result = future.result()
            rows[ticker] = result

    captured_at = requested_at if now is not None else datetime.now(UTC)
    frame = pd.DataFrame(
        [
            {
                "ticker": ticker,
                **rows[ticker],
                "captured_at_utc": captured_at.isoformat(),
            }
            for ticker in tickers
        ]
    )
    frame = _normalize_share_frame(frame)
    return write_current_stock_share_snapshot(
        frame,
        tickers,
        output_dir,
        universe_manifest_path=universe_manifest_path,
        universe_manifest=universe_manifest,
        captured_at=captured_at,
        request_start=request_start,
        request_end_exclusive=request_end,
        minimum_coverage_fraction=minimum_coverage_fraction,
        maximum_observation_age_calendar_days=maximum_observation_age_calendar_days,
    )


def validate_current_stock_shares(
    frame: pd.DataFrame,
    requested_tickers: Iterable[str],
    *,
    captured_at: datetime,
    minimum_coverage_fraction: float = 0.99,
    maximum_observation_age_calendar_days: int = 130,
) -> StockShareValidation:
    requested = tuple(str(value) for value in requested_tickers)
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("Requested tickers must be non-empty and unique.")
    if not 0.0 <= minimum_coverage_fraction <= 1.0:
        raise ValueError("Minimum coverage fraction must be between zero and one.")
    if maximum_observation_age_calendar_days < 0:
        raise ValueError("Maximum observation age cannot be negative.")
    if tuple(frame.columns) != REQUIRED_SHARE_COLUMNS:
        raise StockShareDataError(
            f"Share snapshot columns must exactly match {REQUIRED_SHARE_COLUMNS}."
        )
    captured = _as_utc(captured_at)
    tickers = frame["ticker"].astype(str)
    duplicate_tickers = tuple(sorted(tickers[tickers.duplicated(keep=False)].unique()))
    expected = set(requested)
    observed = set(tickers)
    unexpected = tuple(sorted(observed - expected))
    absent = tuple(sorted(expected - observed))
    source_status = frame["source_status"].astype(str)
    unknown_status = tuple(sorted(set(source_status) - ALLOWED_SOURCE_STATUSES))
    if unknown_status:
        raise StockShareDataError(f"Unknown share source status: {unknown_status}")

    shares = pd.to_numeric(frame["shares_outstanding"], errors="coerce")
    dates = pd.to_datetime(frame["provider_observation_date"], errors="coerce")
    observation_counts = frame["source_observation_count"]
    discarded_counts = frame["discarded_historical_observations"]
    request_attempts = frame["provider_request_attempts"]
    finite_positive = shares.notna() & shares.map(math.isfinite) & shares.gt(0.0)
    dated = dates.notna()
    ages = pd.Series(pd.NA, index=frame.index, dtype="Int64")
    ages.loc[dated] = [
        (captured.date() - value.date()).days for value in dates.loc[dated]
    ]
    available = source_status.eq("available")
    future = available & dated & ages.lt(0).fillna(False)
    stale = available & dated & ages.gt(maximum_observation_age_calendar_days).fillna(False)
    invalid = source_status.eq("invalid") | (available & (~finite_positive | ~dated))
    usable = available & finite_positive & dated & ~future & ~stale
    missing = source_status.eq("missing")
    fetch_error = source_status.eq("fetch_error")
    available_consistent = (
        ~available
        | (
            observation_counts.ge(1)
            & discarded_counts.eq(observation_counts - 1)
            & finite_positive
            & dated
        )
    )
    unavailable = missing | fetch_error
    unavailable_consistent = (
        ~unavailable
        | (
            observation_counts.eq(0)
            & discarded_counts.eq(0)
            & shares.isna()
            & dates.isna()
        )
    )
    nonnegative_counts = (
        observation_counts.ge(0)
        & discarded_counts.ge(0)
        & request_attempts.ge(1)
    )
    inconsistent = ~(available_consistent & unavailable_consistent & nonnegative_counts)
    coverage = float(usable.sum()) / len(requested)
    observation_dates = dates.loc[dated]
    no_roster_errors = not duplicate_tickers and not unexpected and not absent
    passed = (
        no_roster_errors
        and coverage >= minimum_coverage_fraction
        and not bool(invalid.any())
        and not bool(future.any())
        and not bool(inconsistent.any())
        and frame["captured_at_utc"].eq(captured.isoformat()).all()
    )
    return StockShareValidation(
        status="passed" if passed else "failed",
        requested_tickers=len(requested),
        usable_tickers=int(usable.sum()),
        coverage_fraction=coverage,
        minimum_coverage_fraction=minimum_coverage_fraction,
        maximum_observation_age_calendar_days=maximum_observation_age_calendar_days,
        oldest_provider_observation_date=(
            None if observation_dates.empty else observation_dates.min().date().isoformat()
        ),
        newest_provider_observation_date=(
            None if observation_dates.empty else observation_dates.max().date().isoformat()
        ),
        missing_tickers=tuple(sorted(tickers.loc[missing])),
        fetch_error_tickers=tuple(sorted(tickers.loc[fetch_error])),
        invalid_tickers=tuple(sorted(tickers.loc[invalid])),
        stale_tickers=tuple(sorted(tickers.loc[stale])),
        future_observation_tickers=tuple(sorted(tickers.loc[future])),
        inconsistent_source_tickers=tuple(sorted(tickers.loc[inconsistent])),
        duplicate_tickers=duplicate_tickers,
        unexpected_tickers=unexpected,
        absent_roster_tickers=absent,
        source_observations_received=int(observation_counts.sum()),
        discarded_historical_observations=int(discarded_counts.sum()),
    )


def write_current_stock_share_snapshot(
    frame: pd.DataFrame,
    requested_tickers: Iterable[str],
    output_dir: Path,
    *,
    universe_manifest_path: Path,
    universe_manifest: dict[str, object],
    captured_at: datetime,
    request_start: date,
    request_end_exclusive: date,
    minimum_coverage_fraction: float = 0.99,
    maximum_observation_age_calendar_days: int = 130,
) -> StockShareSnapshot:
    normalized = _normalize_share_frame(frame)
    requested = tuple(str(value) for value in requested_tickers)
    validation = validate_current_stock_shares(
        normalized,
        requested,
        captured_at=captured_at,
        minimum_coverage_fraction=minimum_coverage_fraction,
        maximum_observation_age_calendar_days=maximum_observation_age_calendar_days,
    )
    fingerprint = tabular_sha256(normalized)
    captured = _as_utc(captured_at)
    stamp = captured.strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"shares-outstanding-{stamp}-{fingerprint[:12]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / f"{stem}.parquet"
    manifest_path = output_dir / f"{stem}.manifest.json"
    buffer = io.BytesIO()
    normalized.to_parquet(buffer)
    with data_path.open("xb") as handle:
        handle.write(buffer.getvalue())
    try:
        manifest = {
            "schema_version": 2,
            "provider": "Yahoo Finance via yfinance",
            "endpoint": "Ticker.get_shares_full",
            "data_cost_policy": "no_paid_sources",
            "provider_role": SHARES_PROVIDER_ROLE,
            "data_role": SHARES_DATA_ROLE,
            "historical_backfill_authorized": False,
            "action_authorized": False,
            "captured_at_utc": captured.isoformat(),
            "request_start": request_start.isoformat(),
            "request_end_exclusive": request_end_exclusive.isoformat(),
            "provider_history_policy": (
                "only the latest provider value is retained; provider dates are metadata and "
                "the value becomes usable no earlier than captured_at_utc"
            ),
            "data_file": data_path.name,
            "rows": len(normalized),
            "requested_tickers": len(requested),
            "tabular_sha256": fingerprint,
            "data_file_sha256": file_sha256(data_path),
            "validation": validation.to_dict(),
            "shares_data_gate_passed": validation.passed,
            "universe_manifest": universe_manifest_path.name,
            "universe_manifest_sha256": file_sha256(universe_manifest_path),
            "universe_tabular_sha256": universe_manifest.get("tabular_sha256"),
            "universe_role": universe_manifest.get("universe_role"),
        }
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
    except Exception:
        data_path.unlink(missing_ok=True)
        raise
    return StockShareSnapshot(data_path, manifest_path, len(normalized), validation)


def latest_stock_share_manifest(output_dir: Path) -> Path:
    paths = sorted(output_dir.glob("shares-outstanding-*.manifest.json"))
    if not paths:
        raise FileNotFoundError(f"No prospective stock-share snapshots found in {output_dir}.")
    return paths[-1]


def audit_current_stock_share_snapshot(
    manifest_path: Path,
    *,
    universe_manifest_path: Path | None = None,
    max_age_hours: float = 48.0,
    now: datetime | None = None,
) -> StockShareSnapshotAudit:
    errors: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return StockShareSnapshotAudit(False, False, float("inf"), "unreadable_manifest", (str(exc),))
    if not isinstance(manifest, dict):
        return StockShareSnapshotAudit(
            False, False, float("inf"), "invalid_manifest", ("Manifest must be an object.",)
        )
    data_name = manifest.get("data_file")
    if not isinstance(data_name, str) or Path(data_name).name != data_name:
        errors.append("Manifest data_file must be a local filename.")
        data_path = manifest_path.parent / "__invalid__"
    else:
        data_path = manifest_path.parent / data_name
    frame: pd.DataFrame | None = None
    if not data_path.exists():
        errors.append("Stock-share data file is missing.")
    elif file_sha256(data_path) != manifest.get("data_file_sha256"):
        errors.append("Stock-share data-file hash does not match the manifest.")
    else:
        try:
            frame = _normalize_share_frame(pd.read_parquet(data_path))
            if tabular_sha256(frame) != manifest.get("tabular_sha256"):
                errors.append("Stock-share table fingerprint does not match the manifest.")
            if len(frame) != manifest.get("rows"):
                errors.append("Stock-share row count does not match the manifest.")
        except Exception as exc:
            errors.append(f"Stock-share data file is unreadable: {exc}")
    if manifest.get("provider_role") != SHARES_PROVIDER_ROLE:
        errors.append("Stock-share provider role is not prospective research-only.")
    if manifest.get("data_role") != SHARES_DATA_ROLE:
        errors.append("Stock-share data role permits unsafe historical use.")
    if manifest.get("historical_backfill_authorized") is not False:
        errors.append("Historical backfill must be explicitly prohibited.")
    if manifest.get("action_authorized") is not False:
        errors.append("Stock-share snapshot must not authorize trading.")
    if manifest.get("shares_data_gate_passed") is not True:
        errors.append("Original stock-share data gate did not pass.")
    if universe_manifest_path is not None:
        if not universe_manifest_path.exists():
            errors.append("Bound universe manifest is missing.")
        elif file_sha256(universe_manifest_path) != manifest.get("universe_manifest_sha256"):
            errors.append("Universe manifest hash does not match the share snapshot.")
        elif frame is not None:
            try:
                universe, _ = load_locked_current_universe(
                    universe_manifest_path,
                    max_age_hours=max_age_hours,
                    now=now,
                )
                captured = _as_utc(datetime.fromisoformat(str(manifest["captured_at_utc"])))
                stored_validation = manifest.get("validation")
                if not isinstance(stored_validation, dict):
                    errors.append("Stock-share validation report is missing.")
                else:
                    recalculated = validate_current_stock_shares(
                        frame,
                        tuple(str(value) for value in universe["ticker"]),
                        captured_at=captured,
                        minimum_coverage_fraction=float(
                            stored_validation["minimum_coverage_fraction"]
                        ),
                        maximum_observation_age_calendar_days=int(
                            stored_validation["maximum_observation_age_calendar_days"]
                        ),
                    )
                    normalized_validation = json.loads(json.dumps(recalculated.to_dict()))
                    if int(manifest.get("schema_version", 1)) < 2:
                        normalized_validation.pop("inconsistent_source_tickers", None)
                    if recalculated.inconsistent_source_tickers:
                        errors.append("Stock-share source accounting is inconsistent.")
                    if normalized_validation != stored_validation:
                        errors.append("Stock-share validation report does not match the data.")
            except Exception as exc:
                errors.append(f"Bound stock-share evidence is invalid: {exc}")
    try:
        captured = _as_utc(datetime.fromisoformat(str(manifest["captured_at_utc"])))
        checked_at = _as_utc(now or datetime.now(UTC))
        age_hours = (checked_at - captured).total_seconds() / 3600.0
    except (KeyError, TypeError, ValueError):
        age_hours = float("inf")
        errors.append("Stock-share capture time is invalid.")
    freshness = 0.0 <= age_hours <= max_age_hours
    integrity = not errors
    status = "passed" if integrity and freshness else "failed"
    if integrity and not freshness:
        status = "stale"
    return StockShareSnapshotAudit(integrity, freshness, age_hours, status, tuple(errors))


def _collect_one_share_value(
    provider_symbol: str,
    fetcher: ShareFetcher,
    *,
    request_start: date,
    request_end: date,
    retry_attempts: int,
    retry_delay_seconds: float,
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    payload: object | None = None
    attempts = 0
    for attempts in range(1, retry_attempts + 1):
        try:
            payload = fetcher(provider_symbol, request_start, request_end)
            break
        except Exception:
            if attempts < retry_attempts and retry_delay_seconds > 0.0:
                sleeper(retry_delay_seconds * attempts)
    if payload is None:
        return _empty_share_row("fetch_error" if attempts == retry_attempts else "missing", attempts)
    try:
        series = _coerce_share_series(payload)
    except (TypeError, ValueError):
        return _empty_share_row("invalid", attempts)
    if series.empty:
        return _empty_share_row("missing", attempts)
    latest_date = pd.Timestamp(series.index[-1]).tz_localize(None)
    latest_value = float(series.iloc[-1])
    if not math.isfinite(latest_value) or latest_value <= 0.0:
        return {
            **_empty_share_row("invalid", attempts),
            "provider_observation_date": latest_date,
            "source_observation_count": len(series),
            "discarded_historical_observations": max(len(series) - 1, 0),
        }
    rounded = round(latest_value)
    if not math.isclose(latest_value, rounded, rel_tol=0.0, abs_tol=0.5):
        return {
            **_empty_share_row("invalid", attempts),
            "provider_observation_date": latest_date,
            "source_observation_count": len(series),
            "discarded_historical_observations": max(len(series) - 1, 0),
        }
    return {
        "shares_outstanding": rounded,
        "provider_observation_date": latest_date,
        "source_status": "available",
        "source_observation_count": len(series),
        "discarded_historical_observations": max(len(series) - 1, 0),
        "provider_request_attempts": attempts,
    }


def _empty_share_row(status: str, attempts: int) -> dict[str, object]:
    return {
        "shares_outstanding": pd.NA,
        "provider_observation_date": pd.NaT,
        "source_status": status,
        "source_observation_count": 0,
        "discarded_historical_observations": 0,
        "provider_request_attempts": max(attempts, 1),
    }


def _coerce_share_series(payload: object) -> pd.Series:
    if isinstance(payload, pd.DataFrame):
        if payload.shape[1] != 1:
            raise ValueError("Share history data frame must contain one value column.")
        series = payload.iloc[:, 0]
    elif isinstance(payload, pd.Series):
        series = payload
    else:
        raise TypeError("Share history must be a pandas Series or one-column DataFrame.")
    values = pd.to_numeric(series, errors="coerce")
    index = pd.DatetimeIndex(pd.to_datetime(series.index, errors="coerce", utc=True))
    normalized = pd.Series(values.to_numpy(), index=index)
    normalized = normalized.loc[~normalized.index.isna()].dropna().sort_index()
    normalized = normalized.loc[~normalized.index.duplicated(keep="last")]
    normalized.index = normalized.index.tz_convert(None)
    return normalized


def _normalize_share_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(REQUIRED_SHARE_COLUMNS) - set(frame.columns)
    if missing:
        raise StockShareDataError(f"Stock-share data is missing columns: {sorted(missing)}")
    normalized = frame.loc[:, REQUIRED_SHARE_COLUMNS].copy()
    normalized["ticker"] = normalized["ticker"].astype(str)
    normalized["shares_outstanding"] = pd.to_numeric(
        normalized["shares_outstanding"], errors="coerce"
    ).round().astype("Int64")
    normalized["provider_observation_date"] = pd.to_datetime(
        normalized["provider_observation_date"], errors="coerce"
    ).dt.tz_localize(None)
    normalized["captured_at_utc"] = normalized["captured_at_utc"].astype(str)
    normalized["source_status"] = normalized["source_status"].astype(str)
    for column in (
        "source_observation_count",
        "discarded_historical_observations",
        "provider_request_attempts",
    ):
        normalized[column] = pd.to_numeric(normalized[column], errors="raise").astype("int64")
    return normalized.reset_index(drop=True)


def _fetch_yahoo_shares(provider_symbol: str, start: date, end: date) -> object:
    return yf.Ticker(provider_symbol).get_shares_full(
        start=start.isoformat(),
        end=end.isoformat(),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
