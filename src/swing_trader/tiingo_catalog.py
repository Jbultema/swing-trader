from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

from swing_trader.provenance import file_sha256

TIINGO_SUPPORTED_TICKERS_URL = (
    "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
)
DEFAULT_PROBE_DATES = (
    "1996-01-02",
    "2000-01-03",
    "2005-01-03",
    "2008-09-15",
    "2010-01-04",
    "2015-01-05",
    "2019-01-11",
    "2024-09-23",
)
_ANTI_RECYCLING_SUFFIX = re.compile(r"-\d{6}$")
_CATALOG_COLUMNS = {
    "ticker",
    "exchange",
    "assetType",
    "priceCurrency",
    "startDate",
    "endDate",
}


@dataclass(frozen=True)
class TiingoCatalogDateAudit:
    date: str
    point_in_time_members: int
    existing_price_ranges: int
    existing_price_range_coverage: float
    missing_price_ranges: int
    exact_symbol_catalog_hints: int
    suffixed_symbol_catalog_hints: int
    total_catalog_hints: int
    catalog_upper_bound_count: int
    catalog_upper_bound_coverage: float
    unresolved_count: int
    exact_symbol_candidates: tuple[str, ...]
    suffixed_symbol_candidates: tuple[str, ...]
    unresolved_tickers: tuple[str, ...]


@dataclass(frozen=True)
class TiingoCatalogAudit:
    generated_at: str
    source_url: str
    membership_sha256: str
    prices_sha256: str
    catalog_sha256: str
    usd_stock_catalog_rows: int
    quarantined_recycled_tickers: tuple[str, ...]
    dates: tuple[TiingoCatalogDateAudit, ...]
    access_classification: str
    redistribution_authorized: bool
    authenticated_api_verified: bool
    historical_backtest_ready: bool
    action_authorized: bool
    status: str
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def download_tiingo_supported_catalog(
    output: Path | str,
    *,
    url: str = TIINGO_SUPPORTED_TICKERS_URL,
    timeout_seconds: float = 60.0,
) -> Path:
    """Download Tiingo's public symbol catalog without an API token."""
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.content
    _read_catalog(payload)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_tiingo_catalog_audit(
    membership_path: Path | str,
    prices_path: Path | str,
    catalog_path: Path | str,
    output: Path | str,
    *,
    source_url: str = TIINGO_SUPPORTED_TICKERS_URL,
    probe_dates: tuple[str, ...] | None = None,
) -> TiingoCatalogAudit:
    membership_file = Path(membership_path)
    prices_file = Path(prices_path)
    catalog_file = Path(catalog_path)
    audit = audit_tiingo_catalog_coverage(
        pd.read_parquet(membership_file),
        pd.read_parquet(prices_file, columns=["date", "ticker"]),
        catalog_file.read_bytes(),
        membership_sha256=file_sha256(membership_file),
        prices_sha256=file_sha256(prices_file),
        source_url=source_url,
        probe_dates=probe_dates,
    )
    output_file = Path(output)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(audit.to_dict(), indent=2) + "\n", encoding="utf-8")
    return audit


def audit_tiingo_catalog_coverage(
    membership: pd.DataFrame,
    prices: pd.DataFrame,
    catalog_archive: bytes,
    *,
    membership_sha256: str = "in-memory",
    prices_sha256: str = "in-memory",
    source_url: str = TIINGO_SUPPORTED_TICKERS_URL,
    probe_dates: tuple[str, ...] | None = None,
) -> TiingoCatalogAudit:
    """Measure only a catalog-based upper bound on missing historical prices.

    A catalog row is not proof that the authenticated API will return the security,
    and a reused base ticker is not proof of security identity. The result therefore
    remains research-only even when every missing label has a catalog hint.
    """
    intervals = _normalize_membership(membership)
    price_rows = _normalize_prices(prices)
    catalog = _read_catalog(catalog_archive)
    catalog = catalog.loc[
        catalog["assetType"].eq("Stock") & catalog["priceCurrency"].eq("USD")
    ].copy()
    catalog["startDate"] = pd.to_datetime(catalog["startDate"], errors="coerce")
    catalog["endDate"] = pd.to_datetime(catalog["endDate"], errors="coerce")
    # Tiingo documents null bounds as unavailable data, so reservations with null
    # dates are intentionally excluded from even the loose catalog upper bound.
    catalog = catalog.dropna(subset=["ticker", "startDate", "endDate"])

    bounds = price_rows.groupby("ticker")["date"].agg(["min", "max"])
    recycled = _recycled_tickers(intervals, price_rows)
    dates = _probe_timestamps(price_rows, probe_dates)
    date_audits = tuple(
        _audit_date(date, intervals, bounds, catalog, recycled) for date in dates
    )

    return TiingoCatalogAudit(
        generated_at=datetime.now(UTC).isoformat(),
        source_url=source_url,
        membership_sha256=membership_sha256,
        prices_sha256=prices_sha256,
        catalog_sha256=hashlib.sha256(catalog_archive).hexdigest(),
        usd_stock_catalog_rows=len(catalog),
        quarantined_recycled_tickers=tuple(sorted(recycled)),
        dates=date_audits,
        access_classification="zero-dollar account; proprietary internal-use data",
        redistribution_authorized=False,
        authenticated_api_verified=False,
        historical_backtest_ready=False,
        action_authorized=False,
        status="research_only_catalog_upper_bound",
        limitations=(
            "The public catalog includes reserved symbols and does not prove immediate API access.",
            "No authenticated historical-price request was made by this audit.",
            "Ticker strings are not permanent security identifiers; every recovery needs an identity test.",
            "A catalog date span does not prove complete daily bars, corporate actions, or terminal returns.",
            "Provider data are proprietary and must not be committed or redistributed.",
        ),
    )


def _normalize_membership(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"ticker", "start", "end"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Membership intervals are missing columns: {sorted(missing)}")
    result = frame.loc[:, ["ticker", "start", "end"]].copy()
    result["ticker"] = result["ticker"].astype(str).str.strip()
    result["start"] = pd.to_datetime(result["start"], errors="coerce")
    result["end"] = pd.to_datetime(result["end"], errors="coerce")
    if result["ticker"].eq("").any() or result["start"].isna().any():
        raise ValueError("Membership intervals contain blank tickers or invalid start dates.")
    if (result["end"].notna() & result["end"].le(result["start"])).any():
        raise ValueError("Membership interval ends must be later than starts.")
    return result


def _normalize_prices(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"ticker", "date"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Prices are missing columns: {sorted(missing)}")
    result = frame.loc[:, ["ticker", "date"]].copy()
    result["ticker"] = result["ticker"].astype(str).str.strip()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    if result.empty or result["ticker"].eq("").any() or result["date"].isna().any():
        raise ValueError("Prices must contain valid ticker/date rows.")
    return result.drop_duplicates()


def _read_catalog(payload: bytes) -> pd.DataFrame:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(csv_names) != 1:
                raise ValueError("Tiingo catalog archive must contain exactly one CSV file.")
            frame = pd.read_csv(
                io.BytesIO(archive.read(csv_names[0])),
                dtype={"ticker": "string", "assetType": "string", "priceCurrency": "string"},
                keep_default_na=False,
            )
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("Tiingo catalog is not a valid ZIP/CSV archive.") from exc
    missing = _CATALOG_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Tiingo catalog is missing columns: {sorted(missing)}")
    frame["ticker"] = frame["ticker"].astype(str).str.strip()
    return frame


def _recycled_tickers(intervals: pd.DataFrame, prices: pd.DataFrame) -> set[str]:
    # Match the upstream diagnostic: if the only stored price history starts after
    # the historical constituent left, the symbol now belongs to another security.
    last_end = intervals.groupby("ticker")["end"].max()
    first_price = prices.groupby("ticker")["date"].min()
    joined = pd.concat(
        [last_end.rename("left"), first_price.rename("price_start")], axis=1
    ).dropna()
    return set(joined.index[joined["price_start"] > joined["left"]].astype(str))


def _probe_timestamps(
    prices: pd.DataFrame, probe_dates: tuple[str, ...] | None
) -> tuple[pd.Timestamp, ...]:
    if probe_dates is None:
        per_day = prices.groupby("date")["ticker"].nunique()
        broad = per_day[per_day >= 0.5 * per_day.max()]
        latest = broad.index.max() if not broad.empty else prices["date"].max()
        values = (*DEFAULT_PROBE_DATES, str(pd.Timestamp(latest).date()))
    else:
        values = probe_dates
    timestamps = tuple(dict.fromkeys(pd.Timestamp(value) for value in values))
    if not timestamps:
        raise ValueError("At least one probe date is required.")
    return timestamps


def _audit_date(
    date: pd.Timestamp,
    intervals: pd.DataFrame,
    price_bounds: pd.DataFrame,
    catalog: pd.DataFrame,
    recycled: set[str],
) -> TiingoCatalogDateAudit:
    active = intervals.loc[
        intervals["start"].le(date)
        & (intervals["end"].isna() | intervals["end"].gt(date)),
        "ticker",
    ]
    members = set(active.astype(str)) - recycled
    existing = {
        ticker
        for ticker in members
        if ticker in price_bounds.index
        and price_bounds.at[ticker, "min"] <= date <= price_bounds.at[ticker, "max"]
    }
    missing = members - existing
    dated_catalog = set(
        catalog.loc[
            catalog["startDate"].le(date) & catalog["endDate"].ge(date), "ticker"
        ].astype(str)
    )
    exact = {ticker for ticker in missing if ticker in dated_catalog}
    suffixed = {
        ticker
        for ticker in missing - exact
        if _ANTI_RECYCLING_SUFFIX.search(ticker)
        and _ANTI_RECYCLING_SUFFIX.sub("", ticker) in dated_catalog
    }
    hinted = exact | suffixed
    upper = existing | hinted
    unresolved = missing - hinted
    denominator = len(members)

    return TiingoCatalogDateAudit(
        date=str(date.date()),
        point_in_time_members=denominator,
        existing_price_ranges=len(existing),
        existing_price_range_coverage=len(existing) / denominator if denominator else 0.0,
        missing_price_ranges=len(missing),
        exact_symbol_catalog_hints=len(exact),
        suffixed_symbol_catalog_hints=len(suffixed),
        total_catalog_hints=len(hinted),
        catalog_upper_bound_count=len(upper),
        catalog_upper_bound_coverage=len(upper) / denominator if denominator else 0.0,
        unresolved_count=len(unresolved),
        exact_symbol_candidates=tuple(sorted(exact)),
        suffixed_symbol_candidates=tuple(sorted(suffixed)),
        unresolved_tickers=tuple(sorted(unresolved)),
    )
