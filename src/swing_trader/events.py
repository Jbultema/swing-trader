from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

from swing_trader.data import ALPHA_VANTAGE_URL, MarketDataError

EXPECTED_EARNINGS_FIELDS = (
    "symbol",
    "name",
    "reportDate",
    "fiscalDateEnding",
    "estimate",
    "currency",
    "timeOfTheDay",
)


def download_alpha_earnings_calendar(
    api_key: str,
    output_dir: Path,
    *,
    horizon: str = "3month",
    opener: Callable[..., object] = urlopen,
    now: datetime | None = None,
) -> Path:
    """Write an immutable prospective earnings-calendar snapshot."""
    if horizon not in {"3month", "6month", "12month"}:
        raise ValueError("Earnings-calendar horizon must be 3month, 6month, or 12month.")
    if not api_key.strip():
        raise MarketDataError("ALPHA_VANTAGE_API_KEY is empty.")
    query = urlencode(
        {
            "function": "EARNINGS_CALENDAR",
            "horizon": horizon,
            "apikey": api_key,
        }
    )
    with opener(f"{ALPHA_VANTAGE_URL}?{query}", timeout=30) as response:  # type: ignore[attr-defined]
        payload = response.read()  # type: ignore[attr-defined]
    calendar = parse_alpha_earnings_calendar(payload.decode("utf-8"))
    checked_at = now or datetime.now(UTC)
    digest = hashlib.sha256(payload).hexdigest()
    stem = f"{checked_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{horizon}-{digest[:12]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{stem}.parquet"
    if output.exists():
        raise FileExistsError(f"Prospective earnings snapshot already exists: {output}")
    calendar.to_parquet(output, index=False)
    manifest = {
        "provider": "Alpha Vantage",
        "endpoint": "EARNINGS_CALENDAR",
        "role": "prospective_event_risk_only_not_historical_backfill",
        "captured_at_utc": checked_at.isoformat(),
        "horizon": horizon,
        "rows": len(calendar),
        "first_report_date": str(calendar["report_date"].min().date()),
        "last_report_date": str(calendar["report_date"].max().date()),
        "raw_sha256": digest,
        "parquet_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return output


def latest_earnings_snapshot(output_dir: Path) -> Path | None:
    """Return the newest immutable earnings parquet, if one has been captured."""
    paths = sorted(output_dir.glob("*.parquet"))
    return paths[-1] if paths else None


def parse_alpha_earnings_calendar(payload: str) -> pd.DataFrame:
    reader = csv.DictReader(io.StringIO(payload))
    if tuple(reader.fieldnames or ()) != EXPECTED_EARNINGS_FIELDS:
        raise MarketDataError("Alpha Vantage earnings calendar returned an unexpected schema.")
    raw = pd.DataFrame(reader)
    if raw.empty:
        raise MarketDataError("Alpha Vantage earnings calendar returned no rows.")
    raw["report_date"] = pd.to_datetime(raw["reportDate"], errors="coerce")
    raw["fiscal_date_ending"] = pd.to_datetime(raw["fiscalDateEnding"], errors="coerce")
    symbol_valid = raw["symbol"].astype(str).str.fullmatch(re.compile(r"[A-Z0-9.\-]+"))
    valid = raw["report_date"].notna() & raw["fiscal_date_ending"].notna() & symbol_valid
    if not valid.all():
        raise MarketDataError(
            "Alpha Vantage earnings calendar contains malformed rows; probable quota or provider error."
        )
    result = pd.DataFrame(
        {
            "symbol": raw["symbol"].str.upper(),
            "name": raw["name"],
            "report_date": raw["report_date"],
            "fiscal_date_ending": raw["fiscal_date_ending"],
            "estimate": pd.to_numeric(raw["estimate"], errors="coerce"),
            "currency": raw["currency"],
            "time_of_day": raw["timeOfTheDay"].str.strip().str.lower(),
        }
    )
    return result.sort_values(["report_date", "symbol"]).reset_index(drop=True)


def build_earnings_event_flags(
    calendar: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    *,
    lead_sessions: int = 2,
    cooling_sessions: int = 1,
) -> pd.DataFrame:
    """Mark close dates where a next-open entry faces scheduled earnings risk."""
    required = {"symbol", "report_date", "time_of_day"}
    if missing := required - set(calendar.columns):
        raise ValueError(f"Earnings calendar is missing columns: {sorted(missing)}")
    if lead_sessions < 0 or cooling_sessions < 0:
        raise ValueError("Earnings-event windows cannot be negative.")
    dates = pd.DatetimeIndex(pd.to_datetime(sessions)).tz_localize(None).sort_values().unique()
    rows: list[dict[str, object]] = []
    for event in calendar.itertuples(index=False):
        report_date = pd.Timestamp(event.report_date).tz_localize(None)
        candidates = dates[dates >= report_date]
        if not len(candidates):
            continue
        event_session = candidates[0]
        position = int(dates.get_loc(event_session))
        after_close = str(event.time_of_day).lower() in {
            "after close",
            "after_close",
            "post-market",
        }
        blocked_end = position if after_close else position - 1
        blocked_start = max(0, position - lead_sessions)
        for signal_position in range(blocked_start, blocked_end + 1):
            rows.append(
                {
                    "date": dates[signal_position],
                    "ticker": str(event.symbol),
                    "event_report_date": report_date,
                    "event_session": event_session,
                    "time_of_day": event.time_of_day,
                    "entry_blocked": True,
                    "post_earnings_window": False,
                }
            )
        for signal_position in range(position, min(len(dates), position + cooling_sessions + 1)):
            rows.append(
                {
                    "date": dates[signal_position],
                    "ticker": str(event.symbol),
                    "event_report_date": report_date,
                    "event_session": event_session,
                    "time_of_day": event.time_of_day,
                    "entry_blocked": False,
                    "post_earnings_window": True,
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "ticker",
                "event_report_date",
                "event_session",
                "time_of_day",
                "entry_blocked",
                "post_earnings_window",
            ]
        )
    result = pd.DataFrame(rows)
    return result.sort_values(["date", "ticker", "entry_blocked"]).reset_index(drop=True)
