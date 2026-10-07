from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
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
class StockPriceSnapshot:
    data_path: Path
    manifest_path: Path
    rows: int
    tickers: int
    validation: StockPriceValidation


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
    batch_size: int = 100,
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
    )


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
) -> StockPriceSnapshot:
    normalized = frame.sort_index().sort_index(axis=1)
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
            "schema_version": 1,
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
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(tickers), batch_size):
        canonical = tickers[offset : offset + batch_size]
        provider = [ticker.replace(".", "-") for ticker in canonical]
        raw = downloader(
            provider,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            actions=False,
            group_by="column",
            progress=False,
            threads=True,
        )
        frames.append(_normalize_yahoo_batch(raw, canonical, provider))
    combined = pd.concat(frames, axis=1).sort_index()
    combined = combined.loc[:, ~combined.columns.duplicated()]
    combined.columns = pd.MultiIndex.from_tuples(combined.columns, names=["field", "ticker"])
    return combined.sort_index(axis=1)


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


def _safe_yahoo_end_date(captured_at: datetime) -> date:
    eastern = captured_at.astimezone(ZoneInfo("America/New_York"))
    if eastern.weekday() < 5 and eastern.time() < time(16, 15):
        return eastern.date()
    return eastern.date() + timedelta(days=1)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
