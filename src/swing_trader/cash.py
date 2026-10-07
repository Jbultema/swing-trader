from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

FRED_DGS3MO_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO"


def download_fred_cash_returns(
    output_path: Path,
    *,
    opener: Callable[..., object] = urlopen,
) -> pd.Series:
    """Download keyless official 3-month Treasury yields and cache causal returns."""
    with opener(FRED_DGS3MO_CSV_URL, timeout=30) as response:  # type: ignore[attr-defined]
        raw_bytes = response.read()  # type: ignore[attr-defined]
    raw_path = output_path.with_suffix(".source.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw_bytes)
    returns = fred_3m_cash_returns(pd.read_csv(raw_path))
    returns.to_frame().to_parquet(output_path)
    manifest = {
        "provider": "Federal Reserve Bank of St. Louis FRED",
        "series": "DGS3MO",
        "source": "Board of Governors H.15 Selected Interest Rates",
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "url": FRED_DGS3MO_CSV_URL,
        "signal_timing": "prior observation carried forward; one-session lag before accrual",
        "return_transform": "effective annual yield compounded over calendar days to next session",
        "start": str(returns.index.min().date()),
        "end": str(returns.index.max().date()),
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
    }
    output_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return returns


def load_cash_returns(path: Path | str) -> pd.Series:
    frame = pd.read_parquet(path)
    if list(frame.columns) != ["cash_return"]:
        raise ValueError("Cash-return artifact must contain only cash_return.")
    result = frame["cash_return"].astype(float)
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index)).tz_localize(None)
    return result.sort_index()


def fred_3m_cash_returns(raw: pd.DataFrame) -> pd.Series:
    """Convert DGS3MO annual percent yields into causal session returns."""
    date_column = "DATE" if "DATE" in raw.columns else "observation_date"
    if date_column not in raw or "DGS3MO" not in raw:
        raise ValueError("FRED DGS3MO CSV must contain a date column and DGS3MO.")
    dates = pd.to_datetime(raw[date_column], errors="raise")
    rate = pd.to_numeric(raw["DGS3MO"].replace(".", pd.NA), errors="coerce")
    series = pd.Series(rate.to_numpy(), index=pd.DatetimeIndex(dates), name="annual_percent")
    series = series.loc[~series.index.duplicated(keep="last")].sort_index().ffill()
    known_rate = series.shift(1)
    calendar_days = series.index.to_series().shift(-1).sub(series.index.to_series()).dt.days
    returns = (1.0 + known_rate.div(100.0)).pow(calendar_days.div(365.0)).sub(1.0)
    return returns.rename("cash_return")
