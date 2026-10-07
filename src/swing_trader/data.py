from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from time import sleep
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd
import yfinance as yf

REQUIRED_FIELDS = ("Open", "High", "Low", "Close", "Volume")
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"


class MarketDataError(ValueError):
    """Raised when price history is incomplete or internally inconsistent."""


def download_prices(
    tickers: tuple[str, ...],
    start: str,
    output_path: Path,
    *,
    end: str | None = None,
) -> pd.DataFrame:
    """Download split/dividend-adjusted OHLCV and persist an immutable snapshot.

    The provider is suitable for research, not a broker-grade live market feed. Raw
    snapshots and hashes make later revisions detectable.
    """
    raw = yf.download(
        list(tickers),
        start=start,
        end=end,
        auto_adjust=True,
        actions=False,
        group_by="column",
        progress=False,
        threads=True,
    )
    if raw.empty:
        raise MarketDataError("Provider returned no rows.")
    frame = _normalize_download(raw, tickers)
    validate_prices(frame, tickers)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(output_path)
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    manifest = {
        "provider": "Yahoo Finance via yfinance",
        "provider_role": "research_only_unofficial_endpoint",
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "start": str(frame.index.min().date()),
        "end": str(frame.index.max().date()),
        "rows": len(frame),
        "tickers": list(tickers),
        "sha256": digest,
        "adjusted_ohlc": True,
    }
    output_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return frame


def load_prices(path: Path | str) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["field", "ticker"])
    return frame.sort_index()


def download_alpha_vantage_monthly(
    tickers: tuple[str, ...],
    api_key: str,
    output_path: Path,
    *,
    opener: Callable[..., object] = urlopen,
    request_interval_seconds: float = 1.1,
    sleeper: Callable[[float], None] = sleep,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Fetch adjusted monthly closes while respecting the free-tier burst limit."""
    if not api_key.strip():
        raise MarketDataError("Alpha Vantage API key is empty.")
    series: dict[str, pd.Series] = {}
    for position, ticker in enumerate(tickers):
        if position and request_interval_seconds > 0:
            sleeper(request_interval_seconds)
        query = urlencode(
            {
                "function": "TIME_SERIES_MONTHLY_ADJUSTED",
                "symbol": ticker,
                "apikey": api_key,
            }
        )
        with opener(f"{ALPHA_VANTAGE_URL}?{query}", timeout=30) as response:  # type: ignore[attr-defined]
            payload = json.load(response)
        series[ticker] = _parse_alpha_vantage_monthly(payload, ticker)
    frame = pd.DataFrame(series).sort_index()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(output_path)
    captured_at = (now or datetime.now(UTC)).astimezone(UTC)
    manifest = {
        "schema_version": 1,
        "provider": "Alpha Vantage",
        "endpoint": "TIME_SERIES_MONTHLY_ADJUSTED",
        "provider_tier": "free_25_calls_per_day",
        "role": "weekly_etf_adjusted_return_reconciliation_cache",
        "data_cost_policy": "no_paid_sources",
        "captured_at_utc": captured_at.isoformat(),
        "tickers": list(tickers),
        "parquet_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
    }
    output_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return frame


def load_cached_alpha_vantage_monthly(
    path: Path,
    tickers: tuple[str, ...],
    *,
    max_age_hours: float = 192.0,
    now: datetime | None = None,
) -> pd.DataFrame | None:
    """Reuse a complete, recent snapshot so retries do not consume daily API quota."""
    manifest_path = path.with_suffix(".manifest.json")
    if not path.exists() or not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        captured_at = datetime.fromisoformat(str(manifest["captured_at_utc"])).astimezone(UTC)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if manifest.get("parquet_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
        return None
    if manifest.get("tickers") != list(tickers):
        return None
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    age_hours = (checked_at - captured_at).total_seconds() / 3600
    if not 0 <= age_hours <= max_age_hours:
        return None
    frame = pd.read_parquet(path)
    if list(frame.columns) != list(tickers):
        return None
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)
    return frame.sort_index()


def reconcile_monthly_adjusted(
    primary: pd.DataFrame,
    secondary: pd.DataFrame | None,
    tickers: tuple[str, ...],
    *,
    months: int,
    return_tolerance: float,
    max_primary_age_calendar_days: int = 4,
    as_of: date | None = None,
    secondary_error: str | None = None,
) -> dict[str, object]:
    """Compare recent completed-month total returns across independent providers."""
    checked_at = datetime.now(UTC).isoformat()
    primary_close = primary["Close"].reindex(columns=tickers)
    primary_latest_date = pd.Timestamp(primary_close.index.max())
    effective_as_of = as_of or datetime.now(UTC).date()
    age_days = (effective_as_of - primary_latest_date.date()).days
    last_valid_dates = {ticker: primary_close[ticker].last_valid_index() for ticker in tickers}
    all_tickers_current = all(
        pd.Timestamp(last_valid) == primary_latest_date
        for last_valid in last_valid_dates.values()
        if last_valid is not None
    ) and all(last_valid is not None for last_valid in last_valid_dates.values())
    primary_fresh = 0 <= age_days <= max_primary_age_calendar_days and all_tickers_current
    primary_status = {
        "primary_latest_date": str(primary_latest_date.date()),
        "primary_age_calendar_days": age_days,
        "max_primary_age_calendar_days": max_primary_age_calendar_days,
        "all_primary_tickers_current": all_tickers_current,
        "primary_fresh": primary_fresh,
    }
    if secondary is None:
        return {
            "status": "secondary_source_unavailable",
            "decision_data_gate_passed": False,
            "checked_at_utc": checked_at,
            "secondary_error": secondary_error or "Alpha Vantage data not supplied.",
            "primary_provider": "Yahoo Finance via yfinance",
            "secondary_provider": "Alpha Vantage monthly adjusted",
            **primary_status,
        }
    if months < 2:
        raise ValueError("Reconciliation requires at least two monthly returns.")

    complete_mask = _completed_month_mask(primary_close.index)
    primary_monthly = primary_close.loc[complete_mask].copy()
    primary_monthly.index = primary_monthly.index.to_period("M")
    secondary_monthly = secondary.reindex(columns=tickers).copy()
    secondary_monthly.index = pd.DatetimeIndex(secondary_monthly.index).to_period("M")
    secondary_monthly = secondary_monthly.loc[~secondary_monthly.index.duplicated(keep="last")]

    rows: list[dict[str, object]] = []
    passed = primary_fresh
    expected_period = primary_monthly.index.max()
    for ticker in tickers:
        joined = pd.concat(
            {
                "primary": primary_monthly[ticker],
                "secondary": secondary_monthly[ticker],
            },
            axis=1,
            join="inner",
        ).sort_index()
        returns = joined.pct_change(fill_method=None).dropna().tail(months)
        latest_period = returns.index.max() if not returns.empty else None
        differences = (returns["primary"] - returns["secondary"]).abs()
        max_difference = float(differences.max()) if not differences.empty else None
        enough = len(returns) >= months
        current = latest_period == expected_period
        within_tolerance = max_difference is not None and max_difference <= return_tolerance
        ticker_passed = enough and current and within_tolerance
        passed &= ticker_passed
        rows.append(
            {
                "ticker": ticker,
                "months_compared": len(returns),
                "latest_completed_period": str(latest_period) if latest_period else None,
                "expected_completed_period": str(expected_period),
                "max_absolute_monthly_return_difference": max_difference,
                "tolerance": return_tolerance,
                "passed": ticker_passed,
            }
        )
    return {
        "status": "passed" if passed else "failed_reconciliation",
        "decision_data_gate_passed": passed,
        "checked_at_utc": checked_at,
        "primary_provider": "Yahoo Finance via yfinance",
        "secondary_provider": "Alpha Vantage monthly adjusted",
        "months_required": months,
        "return_tolerance": return_tolerance,
        **primary_status,
        "ticker_checks": rows,
    }


def validate_prices(frame: pd.DataFrame, tickers: tuple[str, ...]) -> None:
    if not isinstance(frame.columns, pd.MultiIndex):
        raise MarketDataError("Expected field/ticker MultiIndex columns.")
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise MarketDataError("Dates must be unique and sorted.")
    available = set(frame.columns.get_level_values("ticker"))
    missing = set(tickers) - available
    if missing:
        raise MarketDataError(f"Missing tickers: {sorted(missing)}")
    for field in REQUIRED_FIELDS:
        if field not in frame.columns.get_level_values("field"):
            raise MarketDataError(f"Missing field: {field}")
    close = frame["Close"].reindex(columns=tickers)
    if close.notna().sum().min() < 252:
        short = close.notna().sum().loc[lambda values: values < 252].to_dict()
        raise MarketDataError(f"Insufficient history: {short}")
    if (frame["High"] < frame["Low"]).any(axis=None):
        raise MarketDataError("Found High below Low.")
    if (close.dropna(how="all") <= 0).any(axis=None):
        raise MarketDataError("Prices must be positive.")


def _normalize_download(raw: pd.DataFrame, tickers: tuple[str, ...]) -> pd.DataFrame:
    if not isinstance(raw.columns, pd.MultiIndex):
        raw.columns = pd.MultiIndex.from_product([raw.columns, [tickers[0]]])
    if raw.columns.names[0] == "Ticker":
        raw = raw.swaplevel(0, 1, axis=1)
    wanted = pd.MultiIndex.from_product([REQUIRED_FIELDS, tickers], names=["field", "ticker"])
    frame = raw.reindex(columns=wanted).copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)
    return frame.sort_index().dropna(how="all")


def _parse_alpha_vantage_monthly(payload: object, ticker: str) -> pd.Series:
    if not isinstance(payload, dict):
        raise MarketDataError(f"Alpha Vantage returned invalid payload for {ticker}.")
    error = payload.get("Error Message") or payload.get("Information") or payload.get("Note")
    if error:
        raise MarketDataError(f"Alpha Vantage error for {ticker}: {error}")
    raw = payload.get("Monthly Adjusted Time Series")
    if not isinstance(raw, dict) or not raw:
        raise MarketDataError(f"Alpha Vantage monthly adjusted series missing for {ticker}.")
    values: dict[pd.Timestamp, float] = {}
    for date_text, fields in raw.items():
        if not isinstance(fields, dict) or "5. adjusted close" not in fields:
            raise MarketDataError(
                f"Alpha Vantage adjusted close missing for {ticker} on {date_text}."
            )
        values[pd.Timestamp(date_text)] = float(fields["5. adjusted close"])
    return pd.Series(values, name=ticker, dtype=float).sort_index()


def _completed_month_mask(index: pd.DatetimeIndex) -> pd.Series:
    periods = index.to_period("M")
    next_period = pd.Series(periods, index=index).shift(-1)
    mask = pd.Series(periods != next_period.array, index=index)
    if len(index) and (index[-1] + pd.offsets.BDay(1)).to_period("M") == periods[-1]:
        mask.iloc[-1] = False
    return mask
