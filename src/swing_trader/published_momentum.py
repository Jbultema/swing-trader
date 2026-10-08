from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

from swing_trader.provenance import file_sha256, implementation_sha256, tabular_sha256
from swing_trader.stock_validation import newey_west_mean_standard_error

MOMENTUM_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "10_Portfolios_Prior_12_2_Daily_CSV.zip"
)
SHORT_REVERSAL_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "10_Portfolios_Prior_1_0_Daily_CSV.zip"
)
SIZE_MOMENTUM_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "6_Portfolios_ME_Prior_12_2_Daily_CSV.zip"
)
SIZE_SHORT_REVERSAL_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "6_Portfolios_ME_Prior_1_0_Daily_CSV.zip"
)
SIZE_QUINTILE_MOMENTUM_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "25_Portfolios_ME_Prior_12_2_Daily_CSV.zip"
)
SIZE_QUINTILE_SHORT_REVERSAL_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "25_Portfolios_ME_Prior_1_0_Daily_CSV.zip"
)
FACTORS_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_daily_CSV.zip"
)
SOURCE_ROLE = "published_crsp_portfolio_comparator_gross_not_strategy_backtest"
MAXIMUM_SOURCE_AGE_DAYS = 75


class PublishedMomentumError(ValueError):
    """Raised when an official published comparator cannot be verified."""


@dataclass(frozen=True)
class PublishedDocument:
    url: str
    content: bytes
    fetched_at_utc: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class PublishedMomentumResult:
    report_dir: Path
    manifest_path: Path
    metrics_path: Path
    spreads_path: Path
    returns_path: Path
    source_status: str
    latest_session: str


PublishedFetcher = Callable[[str], PublishedDocument]


def fetch_published_document(url: str) -> PublishedDocument:
    response = requests.get(
        url,
        headers={"User-Agent": "swing-trader/0.1 research github.com/Jbultema/swing-trader"},
        timeout=30,
    )
    response.raise_for_status()
    return PublishedDocument(url, response.content, datetime.now(UTC).isoformat())


def parse_daily_portfolios(content: bytes) -> pd.DataFrame:
    lines = _zip_csv_lines(content)
    return _parse_daily_section(
        lines,
        marker="Average Value Weighted Returns -- Daily",
        required_columns=("Lo PRIOR", "Hi PRIOR"),
    )


def parse_daily_size_portfolios(content: bytes) -> pd.DataFrame:
    lines = _zip_csv_lines(content)
    return _parse_daily_section(
        lines,
        marker="Average Value Weighted Returns -- Daily",
        required_columns=("BIG LoPRIOR", "BIG HiPRIOR"),
    )


def parse_daily_factors(content: bytes) -> pd.DataFrame:
    lines = _zip_csv_lines(content)
    header_position = next(
        (position for position, line in enumerate(lines) if line.strip().startswith(",Mkt-RF")),
        None,
    )
    if header_position is None:
        raise PublishedMomentumError("Daily factor source is missing its Mkt-RF header.")
    return _parse_csv_block(
        lines,
        header_position,
        required_columns=("Mkt-RF", "RF"),
    )


def build_published_momentum_returns(
    momentum: pd.DataFrame,
    short_reversal: pd.DataFrame,
    size_momentum: pd.DataFrame,
    size_short_reversal: pd.DataFrame,
    size_quintile_momentum: pd.DataFrame,
    size_quintile_short_reversal: pd.DataFrame,
    factors: pd.DataFrame,
) -> pd.DataFrame:
    aligned = pd.concat(
        {
            "momentum_loser_12_2": momentum["Lo PRIOR"],
            "momentum_winner_12_2": momentum["Hi PRIOR"],
            "short_term_loser_1_0": short_reversal["Lo PRIOR"],
            "short_term_winner_1_0": short_reversal["Hi PRIOR"],
            "large_momentum_loser_12_2": size_momentum["BIG LoPRIOR"],
            "large_momentum_winner_12_2": size_momentum["BIG HiPRIOR"],
            "large_short_term_loser_1_0": size_short_reversal["BIG LoPRIOR"],
            "large_short_term_winner_1_0": size_short_reversal["BIG HiPRIOR"],
            "largest_momentum_loser_12_2": size_quintile_momentum["BIG LoPRIOR"],
            "largest_momentum_winner_12_2": size_quintile_momentum["BIG HiPRIOR"],
            "largest_short_term_loser_1_0": size_quintile_short_reversal[
                "BIG LoPRIOR"
            ],
            "largest_short_term_winner_1_0": size_quintile_short_reversal[
                "BIG HiPRIOR"
            ],
            "market": factors["Mkt-RF"] + factors["RF"],
            "risk_free": factors["RF"],
        },
        axis=1,
        join="inner",
    ).dropna()
    if aligned.empty:
        raise PublishedMomentumError("Published momentum and factor sources do not overlap.")
    if aligned.index.has_duplicates or not aligned.index.is_monotonic_increasing:
        raise PublishedMomentumError("Published daily returns must be unique and sorted.")
    if aligned.abs().max().max() >= 1.0:
        raise PublishedMomentumError("Published daily returns were not normalized to fractions.")
    return aligned


def write_published_momentum_report(
    source_dir: Path,
    report_root: Path,
    *,
    fetcher: PublishedFetcher = fetch_published_document,
    now: datetime | None = None,
) -> PublishedMomentumResult:
    captured_at = _as_utc(now or datetime.now(UTC))
    documents = {
        "momentum_12_2": fetcher(MOMENTUM_URL),
        "short_term_1_0": fetcher(SHORT_REVERSAL_URL),
        "size_momentum_12_2": fetcher(SIZE_MOMENTUM_URL),
        "size_short_term_1_0": fetcher(SIZE_SHORT_REVERSAL_URL),
        "size_quintile_momentum_12_2": fetcher(SIZE_QUINTILE_MOMENTUM_URL),
        "size_quintile_short_term_1_0": fetcher(SIZE_QUINTILE_SHORT_REVERSAL_URL),
        "daily_factors": fetcher(FACTORS_URL),
    }
    for name, document in documents.items():
        if document.url != _source_url(name):
            raise PublishedMomentumError(f"Published source URL mismatch for {name}.")
        _as_utc(datetime.fromisoformat(document.fetched_at_utc))

    momentum = parse_daily_portfolios(documents["momentum_12_2"].content)
    short_reversal = parse_daily_portfolios(documents["short_term_1_0"].content)
    size_momentum = parse_daily_size_portfolios(documents["size_momentum_12_2"].content)
    size_short_reversal = parse_daily_size_portfolios(
        documents["size_short_term_1_0"].content
    )
    size_quintile_momentum = parse_daily_size_portfolios(
        documents["size_quintile_momentum_12_2"].content
    )
    size_quintile_short_reversal = parse_daily_size_portfolios(
        documents["size_quintile_short_term_1_0"].content
    )
    factors = parse_daily_factors(documents["daily_factors"].content)
    returns = build_published_momentum_returns(
        momentum,
        short_reversal,
        size_momentum,
        size_short_reversal,
        size_quintile_momentum,
        size_quintile_short_reversal,
        factors,
    )
    latest_session = pd.Timestamp(returns.index.max())
    source_age_days = (captured_at.date() - latest_session.date()).days
    source_status = (
        "passed" if 0 <= source_age_days <= MAXIMUM_SOURCE_AGE_DAYS else "stale"
    )

    source_dir.mkdir(parents=True, exist_ok=True)
    source_manifest: dict[str, dict[str, object]] = {}
    for name, document in documents.items():
        source_path = source_dir / f"{name}-{document.sha256[:12]}.zip"
        if source_path.exists():
            if file_sha256(source_path) != document.sha256:
                raise PublishedMomentumError(f"Existing published source is modified: {source_path}")
        else:
            with source_path.open("xb") as handle:
                handle.write(document.content)
        source_manifest[name] = {
            "url": document.url,
            "fetched_at_utc": document.fetched_at_utc,
            "file": source_path.name,
            "file_sha256": document.sha256,
            "bytes": len(document.content),
        }

    combined_hash = hashlib.sha256(
        "".join(document.sha256 for document in documents.values()).encode()
    ).hexdigest()
    stamp = captured_at.strftime("%Y%m%dT%H%M%S%fZ")
    report_dir = report_root / f"published-{stamp}-{combined_hash[:12]}"
    report_dir.mkdir(parents=True, exist_ok=False)
    returns_path = report_dir / "daily_returns.parquet"
    metrics_path = report_dir / "gross_metrics.csv"
    spreads_path = report_dir / "gross_characteristic_spreads.csv"
    regime_path = report_dir / "gross_regime_metrics.csv"
    interpretation_path = report_dir / "interpretation.json"
    returns.to_parquet(returns_path)
    metrics = published_momentum_metrics(returns)
    metrics.to_csv(metrics_path, index=False)
    spreads = published_characteristic_spreads(returns)
    spreads.to_csv(spreads_path, index=False)
    regimes = published_momentum_regimes(returns)
    regimes.to_csv(regime_path, index=False)
    interpretation = _interpretation(metrics, spreads, source_status)
    interpretation_path.write_text(
        json.dumps(interpretation, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_paths = (
        returns_path,
        metrics_path,
        spreads_path,
        regime_path,
        interpretation_path,
    )
    manifest = {
        "schema_version": 1,
        "record_type": "published_daily_momentum_comparator",
        "source_role": SOURCE_ROLE,
        "research_status": "published_gross_comparator_not_trading_authority",
        "data_cost_policy": "no_paid_sources",
        "action_authorized": False,
        "captured_at_utc": captured_at.isoformat(),
        "latest_session": latest_session.date().isoformat(),
        "source_age_days": source_age_days,
        "maximum_source_age_days": MAXIMUM_SOURCE_AGE_DAYS,
        "source_status": source_status,
        "method": (
            "Official Fama-French daily value-weighted prior-return portfolios, including "
            "2x3 and 5x5 size intersections. PRIOR 12-2 and PRIOR 1-0 portfolios are "
            "constructed daily; market is Mkt-RF plus RF. Returns are gross because "
            "constituent turnover and implementation costs are unavailable."
        ),
        "limitations": [
            "These are broad published CRSP deciles, not the swing-trader ten-stock portfolio.",
            "The largest-size quintile is closer to large caps but is not an S&P 500 portfolio.",
            "No transaction cost or slippage deduction is possible from portfolio returns alone.",
            "The data cannot validate swing-trader exits, capacity, or account-specific execution.",
            "Short-term winners and losers are unconditional on share turnover.",
        ],
        "daily_return_rows": len(returns),
        "daily_returns_sha256": tabular_sha256(returns),
        "sources": source_manifest,
        "artifacts": {path.name: file_sha256(path) for path in artifact_paths},
        "implementation_sha256": implementation_sha256(),
    }
    manifest_path = report_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return PublishedMomentumResult(
        report_dir,
        manifest_path,
        metrics_path,
        spreads_path,
        returns_path,
        source_status,
        latest_session.date().isoformat(),
    )


def published_momentum_metrics(returns: pd.DataFrame) -> pd.DataFrame:
    periods = {
        "full_history": None,
        "since_2000": "2000-01-01",
        "since_2010": "2010-01-01",
        "since_2020": "2020-01-01",
        "since_2022": "2022-01-01",
    }
    strategies = tuple(column for column in returns.columns if column != "risk_free")
    rows: list[dict[str, object]] = []
    for period, start in periods.items():
        sample = returns if start is None else returns.loc[start:]
        for strategy in strategies:
            row = _metrics(sample[strategy], sample["risk_free"])
            rows.append({"period": period, "strategy": strategy, **row})
    return pd.DataFrame(rows)


def published_momentum_regimes(returns: pd.DataFrame) -> pd.DataFrame:
    regimes = {
        "global_financial_crisis": ("2007-10-01", "2009-06-30"),
        "momentum_crash_rebound": ("2009-03-01", "2009-05-31"),
        "covid_crash_and_rebound": ("2020-02-01", "2020-12-31"),
        "inflation_rate_shock": ("2022-01-01", "2022-12-31"),
        "post_ai_release_cycle": ("2023-01-01", None),
    }
    strategies = tuple(column for column in returns.columns if column != "risk_free")
    rows: list[dict[str, object]] = []
    for regime, (start, end) in regimes.items():
        sample = returns.loc[start:end]
        if sample.empty:
            continue
        for strategy in strategies:
            row = _metrics(sample[strategy], sample["risk_free"])
            rows.append({"regime": regime, "strategy": strategy, **row})
    return pd.DataFrame(rows)


def published_characteristic_spreads(returns: pd.DataFrame) -> pd.DataFrame:
    """Measure gross winner-minus-loser spreads without claiming implementability."""
    periods = {
        "full_history": None,
        "since_2000": "2000-01-01",
        "since_2010": "2010-01-01",
        "since_2020": "2020-01-01",
        "since_2022": "2022-01-01",
    }
    pairs = {
        "all_stocks_momentum_12_2": (
            "momentum_winner_12_2",
            "momentum_loser_12_2",
        ),
        "all_stocks_short_term_1_0": (
            "short_term_winner_1_0",
            "short_term_loser_1_0",
        ),
        "large_half_momentum_12_2": (
            "large_momentum_winner_12_2",
            "large_momentum_loser_12_2",
        ),
        "large_half_short_term_1_0": (
            "large_short_term_winner_1_0",
            "large_short_term_loser_1_0",
        ),
        "largest_quintile_momentum_12_2": (
            "largest_momentum_winner_12_2",
            "largest_momentum_loser_12_2",
        ),
        "largest_quintile_short_term_1_0": (
            "largest_short_term_winner_1_0",
            "largest_short_term_loser_1_0",
        ),
    }
    rows: list[dict[str, object]] = []
    for period, start in periods.items():
        sample = returns if start is None else returns.loc[start:]
        for characteristic, (winner, loser) in pairs.items():
            spread = sample[winner].sub(sample[loser]).dropna()
            values = spread.to_numpy(dtype=float)
            standard_error = newey_west_mean_standard_error(values, maximum_lag=21)
            observed_mean = float(values.mean())
            rows.append(
                {
                    "period": period,
                    "characteristic": characteristic,
                    "winner_portfolio": winner,
                    "loser_portfolio": loser,
                    "start": spread.index[0].date().isoformat(),
                    "end": spread.index[-1].date().isoformat(),
                    "sessions": len(spread),
                    "annualized_arithmetic_winner_minus_loser": observed_mean * 252.0,
                    "annualized_spread_volatility": float(spread.std(ddof=1))
                    * math.sqrt(252.0),
                    "newey_west_t_statistic": (
                        0.0 if standard_error <= 0.0 else observed_mean / standard_error
                    ),
                    "gross_direction": "momentum" if observed_mean > 0.0 else "reversal",
                    "performance_basis": (
                        "gross_published_winner_minus_loser_no_cost_deduction"
                    ),
                }
            )
    return pd.DataFrame(rows)


def _metrics(returns: pd.Series, risk_free: pd.Series) -> dict[str, object]:
    aligned = pd.concat([returns.rename("return"), risk_free.rename("risk_free")], axis=1).dropna()
    if len(aligned) < 2:
        raise PublishedMomentumError("At least two published return observations are required.")
    daily = aligned["return"]
    excess = daily - aligned["risk_free"]
    equity = (1.0 + daily).cumprod()
    years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1.0 / 365.25)
    cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0)
    volatility = float(daily.std(ddof=1) * math.sqrt(252.0))
    excess_volatility = float(excess.std(ddof=1) * math.sqrt(252.0))
    drawdown = equity.div(equity.cummax()).sub(1.0)
    return {
        "start": daily.index[0].date().isoformat(),
        "end": daily.index[-1].date().isoformat(),
        "sessions": len(daily),
        "cumulative_return": float(equity.iloc[-1] - 1.0),
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe_excess_rf": (
            0.0
            if excess_volatility < 1e-12
            else float(excess.mean() * 252.0 / excess_volatility)
        ),
        "maximum_drawdown": float(drawdown.min()),
        "calmar": 0.0 if abs(float(drawdown.min())) < 1e-12 else cagr / abs(float(drawdown.min())),
        "worst_day": float(daily.min()),
        "positive_day_fraction": float(daily.gt(0.0).mean()),
        "performance_basis": "gross_published_portfolio_return_no_cost_deduction",
    }


def _interpretation(
    metrics: pd.DataFrame,
    spreads: pd.DataFrame,
    source_status: str,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for period in metrics["period"].unique():
        sample = metrics.loc[metrics["period"] == period].set_index("strategy")
        market_cagr = float(sample.loc["market", "cagr"])
        row: dict[str, object] = {"period": str(period), "market_cagr": market_cagr}
        for strategy in (
            "momentum_winner_12_2",
            "short_term_winner_1_0",
            "short_term_loser_1_0",
            "large_momentum_winner_12_2",
            "large_short_term_winner_1_0",
            "large_short_term_loser_1_0",
            "largest_momentum_winner_12_2",
            "largest_short_term_winner_1_0",
            "largest_short_term_loser_1_0",
        ):
            row[f"{strategy}_cagr"] = float(sample.loc[strategy, "cagr"])
            row[f"{strategy}_cagr_minus_market"] = float(sample.loc[strategy, "cagr"]) - market_cagr
        period_spreads = spreads.loc[spreads["period"].eq(period)].set_index(
            "characteristic"
        )
        for label, characteristic in (
            ("prior_month", "all_stocks_short_term_1_0"),
            ("large_prior_month", "large_half_short_term_1_0"),
            ("largest_prior_month", "largest_quintile_short_term_1_0"),
        ):
            spread_row = period_spreads.loc[characteristic]
            row[f"{label}_gross_direction"] = str(spread_row["gross_direction"])
            row[f"{label}_annualized_arithmetic_spread"] = float(
                spread_row["annualized_arithmetic_winner_minus_loser"]
            )
            row[f"{label}_newey_west_t_statistic"] = float(
                spread_row["newey_west_t_statistic"]
            )
        rows.append(row)
    return {
        "source_status": source_status,
        "performance_basis": "gross_published_portfolios_without_costs",
        "periods": rows,
        "action_authorized": False,
        "warning": (
            "Direction is descriptive. Daily reconstitution, unknown turnover, broad deciles, "
            "and no swing-trader exits prevent an implementable return claim."
        ),
    }


def _parse_daily_section(
    lines: list[str],
    *,
    marker: str,
    required_columns: tuple[str, ...],
) -> pd.DataFrame:
    marker_position = next(
        (position for position, line in enumerate(lines) if marker in line),
        None,
    )
    if marker_position is None:
        raise PublishedMomentumError(f"Published portfolio source is missing section: {marker}")
    header_position = next(
        (
            position
            for position in range(marker_position + 1, len(lines))
            if lines[position].strip()
        ),
        None,
    )
    if header_position is None:
        raise PublishedMomentumError("Published portfolio section has no CSV header.")
    return _parse_csv_block(lines, header_position, required_columns=required_columns)


def _parse_csv_block(
    lines: list[str],
    header_position: int,
    *,
    required_columns: tuple[str, ...],
) -> pd.DataFrame:
    header = next(csv.reader([lines[header_position]], skipinitialspace=True))
    header = ["date" if position == 0 else value.strip() for position, value in enumerate(header)]
    if missing := set(required_columns) - set(header):
        raise PublishedMomentumError(f"Published CSV is missing columns: {sorted(missing)}")
    rows: list[list[str]] = []
    for line in lines[header_position + 1 :]:
        values = next(csv.reader([line], skipinitialspace=True))
        date_value = values[0].strip() if values else ""
        if len(date_value) != 8 or not date_value.isdigit():
            break
        if len(values) != len(header):
            raise PublishedMomentumError("Published CSV row width changed.")
        rows.append([value.strip() for value in values])
    if not rows:
        raise PublishedMomentumError("Published daily CSV section contains no rows.")
    frame = pd.DataFrame(rows, columns=header)
    frame["date"] = pd.to_datetime(frame["date"], format="%Y%m%d", errors="raise")
    frame = frame.set_index("date")
    frame = frame.apply(pd.to_numeric, errors="raise").div(100.0)
    frame = frame.mask(frame.le(-0.99))
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise PublishedMomentumError("Published daily source dates must be unique and sorted.")
    return frame


def _zip_csv_lines(content: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise PublishedMomentumError(
                    f"Expected one CSV in published ZIP; found {len(names)}."
                )
            return archive.read(names[0]).decode("utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise PublishedMomentumError("Published source is not a readable CSV ZIP.") from exc


def _source_url(name: str) -> str:
    return {
        "momentum_12_2": MOMENTUM_URL,
        "short_term_1_0": SHORT_REVERSAL_URL,
        "size_momentum_12_2": SIZE_MOMENTUM_URL,
        "size_short_term_1_0": SIZE_SHORT_REVERSAL_URL,
        "size_quintile_momentum_12_2": SIZE_QUINTILE_MOMENTUM_URL,
        "size_quintile_short_term_1_0": SIZE_QUINTILE_SHORT_REVERSAL_URL,
        "daily_factors": FACTORS_URL,
    }[name]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
