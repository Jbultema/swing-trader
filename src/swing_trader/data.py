from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

REQUIRED_FIELDS = ("Open", "High", "Low", "Close", "Volume")


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
