from __future__ import annotations

import io
import json
import math
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from swing_trader.provenance import file_sha256, tabular_sha256
from swing_trader.stock_universe import (
    PROSPECTIVE_UNIVERSE_ROLE,
    audit_current_sp500_snapshot,
)

REQUIRED_PRICE_FIELDS = ("Open", "High", "Low", "Close", "Volume")
PRICE_PROVIDER_ROLE = "research_only_unofficial_endpoint_prospective_snapshot"
SOURCE_QUALITY_POLICY = "conservative-yahoo-ohlcv-repair-v1"
MAXIMUM_RELATIVE_RANGE_EXPANSION = 0.01
MAXIMUM_LATEST_AFFECTED_FRACTION = 0.05
MAXIMUM_QUARANTINED_FRACTION = 0.0001


class StockPriceDataError(ValueError):
    """Raised when a prospective stock-price snapshot is incomplete or unsafe."""


@dataclass(frozen=True)
class StockPriceValidation:
    status: str
    requested_tickers: int
    available_tickers: int
    latest_session: str | None
    latest_age_calendar_days: int | None
    maximum_age_calendar_days: int
    latest_close_tickers: int
    latest_close_fraction: float
    minimum_latest_close_fraction: float
    history_252_tickers: int
    history_252_fraction: float
    minimum_history_252_fraction: float
    missing_tickers: tuple[str, ...]
    stale_tickers: tuple[str, ...]
    invalid_ohlc_rows: int
    invalid_volume_rows: int
    benchmark_current: bool
    partial_session_risk: bool

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StockPriceRepairEvent:
    session: str
    ticker: str
    action: str
    source_open: float | None
    source_high: float | None
    source_low: float | None
    source_close: float | None
    source_volume: float | None
    normalized_open: float | None
    normalized_high: float | None
    normalized_low: float | None
    normalized_close: float | None
    normalized_volume: float | None
    relative_range_expansion: float | None


@dataclass(frozen=True)
class StockPriceRepairReport:
    status: str
    policy: str
    source_invalid_rows: int
    expanded_range_rows: int
    quarantined_rows: int
    latest_affected_rows: int
    latest_affected_fraction: float
    maximum_latest_affected_fraction: float
    quarantined_fraction: float
    maximum_quarantined_fraction: float
    maximum_relative_range_expansion: float
    allowed_maximum_relative_range_expansion: float
    affected_tickers: tuple[str, ...]
    quarantined_tickers: tuple[str, ...]
    events: tuple[StockPriceRepairEvent, ...]

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StockPriceSnapshot:
    data_path: Path
    manifest_path: Path
    rows: int
    tickers: int
    validation: StockPriceValidation
    source_quality: StockPriceRepairReport


@dataclass(frozen=True)
class StockPriceSnapshotAudit:
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


PriceDownloader = Callable[..., pd.DataFrame]


def load_locked_current_universe(
    manifest_path: Path,
    *,
    max_age_hours: float = 48.0,
    now: datetime | None = None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    audit = audit_current_sp500_snapshot(
        manifest_path,
        max_age_hours=max_age_hours,
        now=now,
    )
    if not audit.passed:
        raise StockPriceDataError(
            f"Current-universe snapshot failed its gate: {json.dumps(audit.to_dict())}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise StockPriceDataError("Current-universe manifest must be an object.")
    data_path = manifest_path.parent / str(manifest["data_file"])
    universe = pd.read_parquet(data_path)
    if universe["universe_role"].nunique() != 1 or (
        universe["universe_role"].iloc[0] != PROSPECTIVE_UNIVERSE_ROLE
    ):
        raise StockPriceDataError("Universe data is not marked prospective-only.")
    return universe, manifest


def download_current_stock_prices(
    universe_manifest_path: Path,
    output_dir: Path,
    *,
    extra_tickers: Iterable[str] = (),
    benchmark: str = "SPY",
    lookback_calendar_days: int = 800,
    batch_size: int = 600,
    downloader: PriceDownloader = yf.download,
    now: datetime | None = None,
) -> StockPriceSnapshot:
    """Lock adjusted daily OHLCV for the current roster without a partial daily bar."""
    captured_at = _as_utc(now or datetime.now(UTC))
    universe, universe_manifest = load_locked_current_universe(
        universe_manifest_path,
        now=captured_at,
    )
    tickers = tuple(
        dict.fromkeys(
            [
                *(str(value) for value in universe["ticker"]),
                *(str(value).strip().upper() for value in extra_tickers),
                benchmark,
            ]
        )
    )
    if lookback_calendar_days < 400:
        raise ValueError("Stock signal history requires at least 400 calendar days.")
    if batch_size < 1:
        raise ValueError("batch_size must be positive.")
    end_exclusive = _safe_yahoo_end_date(captured_at)
    start = end_exclusive - timedelta(days=lookback_calendar_days)
    frame = _download_batches(
        tickers,
        start=start,
        end=end_exclusive,
        batch_size=batch_size,
        downloader=downloader,
    )
    frame, source_quality = normalize_yahoo_ohlcv(frame, tickers)
    return write_current_stock_price_snapshot(
        frame,
        tickers,
        output_dir,
        universe_manifest_path=universe_manifest_path,
        universe_manifest=universe_manifest,
        benchmark=benchmark,
        captured_at=captured_at,
        requested_start=start,
        requested_end_exclusive=end_exclusive,
        source_quality=source_quality,
    )


def normalize_yahoo_ohlcv(
    frame: pd.DataFrame,
    requested_tickers: Iterable[str],
    *,
    maximum_relative_range_expansion: float = MAXIMUM_RELATIVE_RANGE_EXPANSION,
    maximum_latest_affected_fraction: float = MAXIMUM_LATEST_AFFECTED_FRACTION,
    maximum_quarantined_fraction: float = MAXIMUM_QUARANTINED_FRACTION,
) -> tuple[pd.DataFrame, StockPriceRepairReport]:
    """Apply only conservative, fully enumerated repairs to provider OHLCV rows."""
    requested = tuple(dict.fromkeys(str(value) for value in requested_tickers))
    if not requested:
        raise ValueError("At least one ticker is required.")
    if maximum_relative_range_expansion < 0.0:
        raise ValueError("Maximum relative range expansion cannot be negative.")
    if not 0.0 <= maximum_latest_affected_fraction <= 1.0:
        raise ValueError("Maximum latest affected fraction must be between zero and one.")
    if not 0.0 <= maximum_quarantined_fraction <= 1.0:
        raise ValueError("Maximum quarantined fraction must be between zero and one.")
    normalized = frame.sort_index().sort_index(axis=1).copy()
    if normalized.empty:
        return normalized, _stock_price_repair_report(
            events=(),
            requested_tickers=requested,
            source_rows=0,
            latest_session=None,
            maximum_relative_range_expansion=maximum_relative_range_expansion,
            maximum_latest_affected_fraction=maximum_latest_affected_fraction,
            maximum_quarantined_fraction=maximum_quarantined_fraction,
        )
    if not isinstance(normalized.columns, pd.MultiIndex):
        raise StockPriceDataError("Stock prices must use field/ticker MultiIndex columns.")
    fields = set(normalized.columns.get_level_values(0))
    if missing_fields := set(REQUIRED_PRICE_FIELDS) - fields:
        raise StockPriceDataError(f"Stock prices are missing fields: {sorted(missing_fields)}")

    open_ = normalized["Open"].reindex(columns=requested)
    high = normalized["High"].reindex(columns=requested)
    low = normalized["Low"].reindex(columns=requested)
    close = normalized["Close"].reindex(columns=requested)
    volume = normalized["Volume"].reindex(columns=requested)
    complete = open_.notna() & high.notna() & low.notna() & close.notna()
    finite_ohlc = (
        open_.map(math.isfinite)
        & high.map(math.isfinite)
        & low.map(math.isfinite)
        & close.map(math.isfinite)
    )
    invalid_volume = volume.notna() & (~volume.map(math.isfinite) | volume.lt(0.0))
    quarantine = (
        complete
        & (~finite_ohlc | open_.le(0.0) | high.le(0.0) | low.le(0.0) | close.le(0.0))
    ) | invalid_volume
    upper = pd.concat([open_, high, low, close], axis=0).groupby(level=0).max()
    lower = pd.concat([open_, high, low, close], axis=0).groupby(level=0).min()
    expand = complete & ~quarantine & (high.lt(upper) | low.gt(lower))
    events: list[StockPriceRepairEvent] = []

    for session, ticker in quarantine.stack()[lambda values: values].index:
        source_values = _ohlcv_values(normalized, session, ticker)
        for field in REQUIRED_PRICE_FIELDS:
            normalized.loc[session, (field, ticker)] = float("nan")
        events.append(
            _repair_event(
                session,
                ticker,
                "quarantined_nonpositive_nonfinite_or_negative_volume",
                source_values,
                _ohlcv_values(normalized, session, ticker),
                None,
            )
        )

    for session, ticker in expand.stack()[lambda values: values].index:
        source_values = _ohlcv_values(normalized, session, ticker)
        new_high = max(source_values[0], source_values[1], source_values[2], source_values[3])
        new_low = min(source_values[0], source_values[1], source_values[2], source_values[3])
        expansion = max(new_high - source_values[1], source_values[2] - new_low)
        relative_expansion = expansion / abs(source_values[3])
        normalized.loc[session, ("High", ticker)] = new_high
        normalized.loc[session, ("Low", ticker)] = new_low
        events.append(
            _repair_event(
                session,
                ticker,
                "expanded_high_low_to_include_positive_open_close",
                source_values,
                _ohlcv_values(normalized, session, ticker),
                relative_expansion,
            )
        )

    report = _stock_price_repair_report(
        events=tuple(events),
        requested_tickers=requested,
        source_rows=len(frame),
        latest_session=pd.Timestamp(frame.index.max()),
        maximum_relative_range_expansion=maximum_relative_range_expansion,
        maximum_latest_affected_fraction=maximum_latest_affected_fraction,
        maximum_quarantined_fraction=maximum_quarantined_fraction,
    )
    return normalized.sort_index(axis=1), report


def validate_current_stock_prices(
    frame: pd.DataFrame,
    requested_tickers: Iterable[str],
    *,
    benchmark: str = "SPY",
    now: datetime | None = None,
    minimum_latest_close_fraction: float = 0.99,
    minimum_history_252_fraction: float = 0.95,
    maximum_age_calendar_days: int = 4,
) -> StockPriceValidation:
    requested = tuple(dict.fromkeys(str(value) for value in requested_tickers))
    if not requested:
        raise ValueError("At least one ticker is required.")
    if not isinstance(frame.columns, pd.MultiIndex):
        raise StockPriceDataError("Stock prices must use field/ticker MultiIndex columns.")
    if frame.columns.names != ["field", "ticker"]:
        raise StockPriceDataError("Stock price columns must be named field/ticker.")
    fields = set(frame.columns.get_level_values("field"))
    if missing_fields := set(REQUIRED_PRICE_FIELDS) - fields:
        raise StockPriceDataError(f"Stock prices are missing fields: {sorted(missing_fields)}")
    if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise StockPriceDataError("Stock price dates must be non-empty, unique, and sorted.")

    close = frame["Close"].reindex(columns=requested)
    available = close.notna().any()
    missing_tickers = tuple(sorted(available.index[~available]))
    latest = pd.Timestamp(frame.index.max()).tz_localize(None)
    checked_at = _as_utc(now or datetime.now(UTC))
    age_days = (checked_at.date() - latest.date()).days
    latest_mask = close.loc[latest].notna()
    latest_count = int(latest_mask.sum())
    latest_fraction = latest_count / len(requested)
    history_counts = close.notna().sum()
    history_count = int(history_counts.ge(252).sum())
    history_fraction = history_count / len(requested)
    last_valid = close.apply(lambda series: series.last_valid_index())
    stale = tuple(
        sorted(
            ticker
            for ticker, value in last_valid.items()
            if value is not None and pd.Timestamp(value) != latest
        )
    )

    open_ = frame["Open"].reindex(columns=requested)
    high = frame["High"].reindex(columns=requested)
    low = frame["Low"].reindex(columns=requested)
    paired = open_.notna() & high.notna() & low.notna() & close.notna()
    invalid_ohlc = paired & (
        high.lt(pd.concat([open_, close, low], axis=0).groupby(level=0).max())
        | low.gt(pd.concat([open_, close, high], axis=0).groupby(level=0).min())
        | open_.le(0.0)
        | high.le(0.0)
        | low.le(0.0)
        | close.le(0.0)
    )
    volume = frame["Volume"].reindex(columns=requested)
    invalid_volume = volume.notna() & volume.lt(0.0)
    eastern = checked_at.astimezone(ZoneInfo("America/New_York"))
    partial_risk = (
        latest.date() == eastern.date()
        and eastern.weekday() < 5
        and eastern.time() < time(16, 15)
    )
    benchmark_current = bool(benchmark in latest_mask.index and latest_mask.loc[benchmark])
    passed = (
        0 <= age_days <= maximum_age_calendar_days
        and latest_fraction >= minimum_latest_close_fraction
        and history_fraction >= minimum_history_252_fraction
        and int(invalid_ohlc.to_numpy().sum()) == 0
        and int(invalid_volume.to_numpy().sum()) == 0
        and benchmark_current
        and not partial_risk
    )
    return StockPriceValidation(
        status="passed" if passed else "failed",
        requested_tickers=len(requested),
        available_tickers=int(available.sum()),
        latest_session=latest.date().isoformat(),
        latest_age_calendar_days=age_days,
        maximum_age_calendar_days=maximum_age_calendar_days,
        latest_close_tickers=latest_count,
        latest_close_fraction=latest_fraction,
        minimum_latest_close_fraction=minimum_latest_close_fraction,
        history_252_tickers=history_count,
        history_252_fraction=history_fraction,
        minimum_history_252_fraction=minimum_history_252_fraction,
        missing_tickers=missing_tickers,
        stale_tickers=stale,
        invalid_ohlc_rows=int(invalid_ohlc.to_numpy().sum()),
        invalid_volume_rows=int(invalid_volume.to_numpy().sum()),
        benchmark_current=benchmark_current,
        partial_session_risk=partial_risk,
    )


def write_current_stock_price_snapshot(
    frame: pd.DataFrame,
    requested_tickers: Iterable[str],
    output_dir: Path,
    *,
    universe_manifest_path: Path,
    universe_manifest: dict[str, object],
    benchmark: str,
    captured_at: datetime,
    requested_start: date,
    requested_end_exclusive: date,
    source_quality: StockPriceRepairReport | None = None,
) -> StockPriceSnapshot:
    normalized = frame.sort_index().sort_index(axis=1)
    if source_quality is None:
        source_quality = _stock_price_repair_report(
            events=(),
            requested_tickers=tuple(requested_tickers),
            source_rows=len(normalized),
            latest_session=None if normalized.empty else pd.Timestamp(normalized.index.max()),
            maximum_relative_range_expansion=MAXIMUM_RELATIVE_RANGE_EXPANSION,
            maximum_latest_affected_fraction=MAXIMUM_LATEST_AFFECTED_FRACTION,
            maximum_quarantined_fraction=MAXIMUM_QUARANTINED_FRACTION,
        )
    if not source_quality.passed:
        raise StockPriceDataError(
            "Stock-price source anomalies exceeded the conservative repair gate: "
            + json.dumps(source_quality.to_dict(), sort_keys=True)
        )
    validation = validate_current_stock_prices(
        normalized,
        requested_tickers,
        benchmark=benchmark,
        now=captured_at,
    )
    if not validation.passed:
        raise StockPriceDataError(
            "Current stock prices failed validation: "
            + json.dumps(validation.to_dict(), sort_keys=True)
        )
    fingerprint = tabular_sha256(normalized)
    stamp = captured_at.strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"adjusted-daily-{stamp}-{fingerprint[:12]}"
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
            "data_cost_policy": "no_paid_sources",
            "provider_role": PRICE_PROVIDER_ROLE,
            "action_authorized": False,
            "captured_at_utc": captured_at.isoformat(),
            "requested_start": requested_start.isoformat(),
            "requested_end_exclusive": requested_end_exclusive.isoformat(),
            "adjusted_ohlcv": True,
            "data_file": data_path.name,
            "rows": len(normalized),
            "tickers": len(tuple(requested_tickers)),
            "tabular_sha256": fingerprint,
            "data_file_sha256": file_sha256(data_path),
            "validation": validation.to_dict(),
            "source_quality": source_quality.to_dict(),
            "price_data_gate_passed": validation.passed,
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
    return StockPriceSnapshot(
        data_path,
        manifest_path,
        len(normalized),
        validation.available_tickers,
        validation,
        source_quality,
    )


def latest_stock_price_manifest(output_dir: Path) -> Path:
    paths = sorted(output_dir.glob("adjusted-daily-*.manifest.json"))
    if not paths:
        raise FileNotFoundError(f"No prospective stock-price snapshots found in {output_dir}.")
    return paths[-1]


def audit_current_stock_price_snapshot(
    manifest_path: Path,
    *,
    universe_manifest_path: Path | None = None,
    max_age_hours: float = 48.0,
    now: datetime | None = None,
) -> StockPriceSnapshotAudit:
    errors: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return StockPriceSnapshotAudit(False, False, float("inf"), "unreadable_manifest", (str(exc),))
    if not isinstance(manifest, dict):
        return StockPriceSnapshotAudit(
            False, False, float("inf"), "invalid_manifest", ("Manifest must be an object.",)
        )
    data_name = manifest.get("data_file")
    if not isinstance(data_name, str) or Path(data_name).name != data_name:
        errors.append("Manifest data_file must be a local filename.")
        data_path = manifest_path.parent / "__invalid__"
    else:
        data_path = manifest_path.parent / data_name
    if not data_path.exists():
        errors.append("Stock-price data file is missing.")
    elif file_sha256(data_path) != manifest.get("data_file_sha256"):
        errors.append("Stock-price data-file hash does not match the manifest.")
    else:
        try:
            frame = pd.read_parquet(data_path)
            frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["field", "ticker"])
            if tabular_sha256(frame) != manifest.get("tabular_sha256"):
                errors.append("Stock-price table fingerprint does not match the manifest.")
            if len(frame) != manifest.get("rows"):
                errors.append("Stock-price row count does not match the manifest.")
        except Exception as exc:
            errors.append(f"Stock-price data file is unreadable: {exc}")
    if manifest.get("provider_role") != PRICE_PROVIDER_ROLE:
        errors.append("Stock-price provider role is not prospective research-only.")
    if manifest.get("price_data_gate_passed") is not True:
        errors.append("Original stock-price data gate did not pass.")
    if int(manifest.get("schema_version", 1)) >= 2:
        source_quality = manifest.get("source_quality")
        if not isinstance(source_quality, dict):
            errors.append("Stock-price source-quality report is missing.")
        else:
            if source_quality.get("policy") != SOURCE_QUALITY_POLICY:
                errors.append("Stock-price source-quality policy is invalid.")
            if source_quality.get("status") != "passed":
                errors.append("Stock-price source-quality gate did not pass.")
    if universe_manifest_path is not None:
        if not universe_manifest_path.exists():
            errors.append("Bound universe manifest is missing.")
        elif file_sha256(universe_manifest_path) != manifest.get("universe_manifest_sha256"):
            errors.append("Universe manifest hash does not match the price snapshot.")
    try:
        captured = _as_utc(datetime.fromisoformat(str(manifest["captured_at_utc"])))
        checked_at = _as_utc(now or datetime.now(UTC))
        age_hours = (checked_at - captured).total_seconds() / 3600.0
    except (KeyError, TypeError, ValueError):
        age_hours = float("inf")
        errors.append("Stock-price capture time is invalid.")
    freshness = 0.0 <= age_hours <= max_age_hours
    integrity = not errors
    status = "passed" if integrity and freshness else "failed"
    if integrity and not freshness:
        status = "stale"
    return StockPriceSnapshotAudit(integrity, freshness, age_hours, status, tuple(errors))


def _download_batches(
    tickers: tuple[str, ...],
    *,
    start: date,
    end: date,
    batch_size: int,
    downloader: PriceDownloader,
    retry_attempts: int = 2,
    retry_delay_seconds: float = 2.0,
    individual_retry_limit: int = 10,
    request_timeout_seconds: float = 20.0,
    sleeper: Callable[[float], None] = sleep,
) -> pd.DataFrame:
    if retry_attempts < 0:
        raise ValueError("retry_attempts cannot be negative.")
    if individual_retry_limit < 0:
        raise ValueError("individual_retry_limit cannot be negative.")
    if request_timeout_seconds <= 0.0:
        raise ValueError("request_timeout_seconds must be positive.")

    frames: list[pd.DataFrame] = []
    for offset in range(0, len(tickers), batch_size):
        canonical = tickers[offset : offset + batch_size]
        frames.append(
            _download_ticker_group(
                canonical,
                start=start,
                end=end,
                downloader=downloader,
                request_timeout_seconds=request_timeout_seconds,
            )
        )
    combined = pd.concat(frames, axis=1).sort_index()
    combined = combined.loc[:, ~combined.columns.duplicated()]
    combined.columns = pd.MultiIndex.from_tuples(combined.columns, names=["field", "ticker"])

    # Yahoo can return complete history but omit the newest row for an entire request shard. Retry
    # the unresolved set collectively before considering single-symbol recovery. This avoids
    # turning a transient 150-name partial response into hundreds of serial HTTP calls.
    for attempt in range(retry_attempts):
        pending = _incomplete_tickers(combined, tickers)
        if not pending:
            break
        if retry_delay_seconds > 0:
            sleeper(retry_delay_seconds * (attempt + 1))
        for offset in range(0, len(pending), batch_size):
            canonical = pending[offset : offset + batch_size]
            retry = _download_ticker_group(
                canonical,
                start=start,
                end=end,
                downloader=downloader,
                request_timeout_seconds=request_timeout_seconds,
            )
            combined = combined.combine_first(retry)

    pending = _incomplete_tickers(combined, tickers)
    if 0 < len(pending) <= individual_retry_limit:
        for ticker in pending:
            retry = _download_ticker_group(
                (ticker,),
                start=start,
                end=end,
                downloader=downloader,
                request_timeout_seconds=request_timeout_seconds,
            )
            combined = combined.combine_first(retry)
    return combined.sort_index(axis=1)


def _download_ticker_group(
    canonical_tickers: tuple[str, ...],
    *,
    start: date,
    end: date,
    downloader: PriceDownloader,
    request_timeout_seconds: float,
) -> pd.DataFrame:
    provider_tickers = [ticker.replace(".", "-") for ticker in canonical_tickers]
    try:
        raw = downloader(
            provider_tickers,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            actions=False,
            group_by="column",
            progress=False,
            threads=len(provider_tickers) > 1,
            timeout=request_timeout_seconds,
        )
    except Exception:
        raw = pd.DataFrame()
    return _normalize_yahoo_batch(raw, canonical_tickers, provider_tickers)


def _incomplete_tickers(frame: pd.DataFrame, tickers: tuple[str, ...]) -> tuple[str, ...]:
    if frame.empty:
        return tickers
    close = frame["Close"].reindex(columns=tickers)
    latest = frame.index.max()
    return tuple(
        ticker
        for ticker in tickers
        if close[ticker].last_valid_index() is None
        or close[ticker].last_valid_index() != latest
    )


def _normalize_yahoo_batch(
    raw: pd.DataFrame,
    canonical_tickers: tuple[str, ...],
    provider_tickers: list[str],
) -> pd.DataFrame:
    if raw.empty:
        columns = pd.MultiIndex.from_product(
            [REQUIRED_PRICE_FIELDS, canonical_tickers], names=["field", "ticker"]
        )
        return pd.DataFrame(columns=columns, dtype=float)
    frame = raw.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)
    provider_to_canonical = dict(zip(provider_tickers, canonical_tickers, strict=True))
    if not isinstance(frame.columns, pd.MultiIndex):
        if len(canonical_tickers) != 1:
            raise StockPriceDataError("Provider returned flat columns for a multi-ticker request.")
        frame.columns = pd.MultiIndex.from_product(
            [frame.columns, canonical_tickers], names=["field", "ticker"]
        )
    elif set(frame.columns.get_level_values(0)) >= set(REQUIRED_PRICE_FIELDS):
        frame.columns = pd.MultiIndex.from_tuples(
            [
                (str(field), provider_to_canonical.get(str(ticker), str(ticker)))
                for field, ticker in frame.columns
            ],
            names=["field", "ticker"],
        )
    elif set(frame.columns.get_level_values(1)) >= set(REQUIRED_PRICE_FIELDS):
        frame = frame.swaplevel(0, 1, axis=1)
        frame.columns = pd.MultiIndex.from_tuples(
            [
                (str(field), provider_to_canonical.get(str(ticker), str(ticker)))
                for field, ticker in frame.columns
            ],
            names=["field", "ticker"],
        )
    else:
        raise StockPriceDataError("Provider returned an unrecognized column layout.")
    expected = pd.MultiIndex.from_product(
        [REQUIRED_PRICE_FIELDS, canonical_tickers], names=["field", "ticker"]
    )
    return frame.reindex(columns=expected).sort_index()


def _ohlcv_values(
    frame: pd.DataFrame,
    session: object,
    ticker: str,
) -> tuple[float, float, float, float, float]:
    return tuple(float(frame.loc[session, (field, ticker)]) for field in REQUIRED_PRICE_FIELDS)  # type: ignore[return-value]


def _finite_or_none(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _repair_event(
    session: object,
    ticker: str,
    action: str,
    source: tuple[float, float, float, float, float],
    normalized: tuple[float, float, float, float, float],
    relative_range_expansion: float | None,
) -> StockPriceRepairEvent:
    return StockPriceRepairEvent(
        session=pd.Timestamp(session).date().isoformat(),
        ticker=ticker,
        action=action,
        source_open=_finite_or_none(source[0]),
        source_high=_finite_or_none(source[1]),
        source_low=_finite_or_none(source[2]),
        source_close=_finite_or_none(source[3]),
        source_volume=_finite_or_none(source[4]),
        normalized_open=_finite_or_none(normalized[0]),
        normalized_high=_finite_or_none(normalized[1]),
        normalized_low=_finite_or_none(normalized[2]),
        normalized_close=_finite_or_none(normalized[3]),
        normalized_volume=_finite_or_none(normalized[4]),
        relative_range_expansion=relative_range_expansion,
    )


def _stock_price_repair_report(
    *,
    events: tuple[StockPriceRepairEvent, ...],
    requested_tickers: tuple[str, ...],
    source_rows: int,
    latest_session: pd.Timestamp | None,
    maximum_relative_range_expansion: float,
    maximum_latest_affected_fraction: float,
    maximum_quarantined_fraction: float,
) -> StockPriceRepairReport:
    expanded = tuple(event for event in events if event.relative_range_expansion is not None)
    quarantined = tuple(event for event in events if event.relative_range_expansion is None)
    latest_date = None if latest_session is None else latest_session.date().isoformat()
    latest_affected = sum(event.session == latest_date for event in events)
    latest_fraction = latest_affected / len(requested_tickers)
    possible_rows = source_rows * len(requested_tickers)
    quarantined_fraction = len(quarantined) / possible_rows if possible_rows else 0.0
    observed_maximum_expansion = max(
        (float(event.relative_range_expansion) for event in expanded),
        default=0.0,
    )
    passed = (
        latest_fraction <= maximum_latest_affected_fraction
        and quarantined_fraction <= maximum_quarantined_fraction
        and observed_maximum_expansion <= maximum_relative_range_expansion
    )
    return StockPriceRepairReport(
        status="passed" if passed else "failed",
        policy=SOURCE_QUALITY_POLICY,
        source_invalid_rows=len(events),
        expanded_range_rows=len(expanded),
        quarantined_rows=len(quarantined),
        latest_affected_rows=latest_affected,
        latest_affected_fraction=latest_fraction,
        maximum_latest_affected_fraction=maximum_latest_affected_fraction,
        quarantined_fraction=quarantined_fraction,
        maximum_quarantined_fraction=maximum_quarantined_fraction,
        maximum_relative_range_expansion=observed_maximum_expansion,
        allowed_maximum_relative_range_expansion=maximum_relative_range_expansion,
        affected_tickers=tuple(sorted({event.ticker for event in events})),
        quarantined_tickers=tuple(sorted({event.ticker for event in quarantined})),
        events=events,
    )


def _safe_yahoo_end_date(captured_at: datetime) -> date:
    eastern = captured_at.astimezone(ZoneInfo("America/New_York"))
    if eastern.weekday() < 5 and eastern.time() < time(16, 15):
        return eastern.date()
    return eastern.date() + timedelta(days=1)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
