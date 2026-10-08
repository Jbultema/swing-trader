from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from swing_trader.provenance import file_sha256, tabular_sha256
from swing_trader.stock_data import StockCoverageAudit, audit_stock_coverage

REQUIRED_FIELDS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
OHLC_FIELDS = ("Open", "High", "Low", "Close")
RECOVERY_ROLE = "historical_identity_recovery_feasibility_not_accepted_backtest_data"
MAXIMUM_RELATIVE_RANGE_EXPANSION = 0.01


class HistoricalIdentityError(ValueError):
    """Raised when a historical security mapping cannot be proven or reproduced."""


@dataclass(frozen=True)
class HistoricalIdentityRule:
    canonical_ticker: str
    provider_ticker: str
    expected_member_start: str
    expected_member_end_exclusive: str
    relationship: str
    official_evidence_urls: tuple[str, ...]


YAHOO_IDENTITY_RULES = (
    HistoricalIdentityRule(
        canonical_ticker="FRC",
        provider_ticker="FRCB",
        expected_member_start="2019-01-02",
        expected_member_end_exclusive="2023-05-04",
        relationship=(
            "same First Republic common equity after exchange removal and OTC symbol change; "
            "provider backfill still requires price-level cross-source validation"
        ),
        official_evidence_urls=(
            "https://www.otcmarkets.com/stock/FRCB/overview",
            "https://www.fdic.gov/resources/resolutions/bank-failures/failed-bank-list/first-republic.html",
        ),
    ),
    HistoricalIdentityRule(
        canonical_ticker="HFC",
        provider_ticker="DINO",
        expected_member_start="2018-06-18",
        expected_member_end_exclusive="2021-06-04",
        relationship=(
            "HollyFrontier predecessor history backfilled under DINO; the later holding-company "
            "conversion exchanged HFC shares one-for-one"
        ),
        official_evidence_urls=(
            "https://www.hfsinclair.com/investor-relations/press-releases/Press-Release-Details/2022/HollyFrontier-and-Holly-Energy-Partners-Announce-Completion-of-Transactions-with-The-Sinclair-Companies-and-Establishment-of-New-Parent-Company-HF-Sinclair-Corporation/default.aspx",
        ),
    ),
)


@dataclass(frozen=True)
class HistoricalIdentityRuleAudit:
    canonical_ticker: str
    provider_ticker: str
    relationship: str
    official_evidence_urls: tuple[str, ...]
    member_start: str
    member_end_exclusive: str
    requested_start: str
    requested_end_exclusive: str
    source_first_session: str | None
    source_last_session: str | None
    complete_source_rows: int
    incomplete_source_rows: int
    expanded_range_rows: int
    maximum_relative_range_expansion: float
    range_repair_sessions: tuple[str, ...]
    expected_member_sessions: int
    covered_member_sessions: int
    member_session_coverage: float
    pre_member_sessions: int
    required_pre_member_sessions: int
    exit_session: str | None
    exit_session_covered: bool
    status: str


@dataclass(frozen=True)
class HistoricalIdentityRecovery:
    recovered: pd.DataFrame
    rules: tuple[HistoricalIdentityRuleAudit, ...]
    original_coverage: StockCoverageAudit
    supplemented_coverage: StockCoverageAudit


@dataclass(frozen=True)
class HistoricalIdentitySnapshot:
    data_path: Path
    manifest_path: Path
    recovered_tickers: tuple[str, ...]
    remaining_missing_tickers: tuple[str, ...]
    historical_backtest_ready: bool


PriceDownloader = Callable[..., pd.DataFrame]


def build_yahoo_identity_recovery(
    membership: pd.DataFrame,
    existing_prices: pd.DataFrame,
    *,
    downloader: PriceDownloader = yf.download,
    rules: tuple[HistoricalIdentityRule, ...] = YAHOO_IDENTITY_RULES,
    minimum_history_sessions: int = 252,
    audit_start: str | date | pd.Timestamp = "2014-01-02",
) -> HistoricalIdentityRecovery:
    """Recover only explicitly proven aliases and quantify their coverage effect.

    The function deliberately maps a provider series only to the dated membership
    interval declared in a rule. It never infers aliases from ticker similarity and
    never treats a successful mapping as acceptance of the surrounding historical
    panel or of Yahoo as an authoritative security master.
    """
    intervals = _normalize_membership(membership)
    price_keys = _normalize_price_keys(existing_prices)
    calendar = pd.DatetimeIndex(sorted(price_keys["date"].unique()))
    if calendar.empty:
        raise HistoricalIdentityError("Existing prices do not define a session calendar.")
    if minimum_history_sessions < 1:
        raise ValueError("minimum_history_sessions must be positive.")
    start = pd.Timestamp(audit_start)
    if start not in calendar:
        later = calendar[calendar >= start]
        if later.empty:
            raise HistoricalIdentityError("The audit start is after the price calendar.")
        start = later[0]

    recovered_frames: list[pd.DataFrame] = []
    rule_audits: list[HistoricalIdentityRuleAudit] = []
    for rule in rules:
        member_start, member_end = _validate_rule_against_membership(rule, intervals)
        prior_sessions = calendar[calendar < member_start]
        if len(prior_sessions) < minimum_history_sessions:
            raise HistoricalIdentityError(
                f"The base calendar lacks {minimum_history_sessions} sessions before "
                f"{rule.canonical_ticker} membership."
            )
        requested_start = prior_sessions[-minimum_history_sessions]
        exit_sessions = calendar[calendar >= member_end]
        if exit_sessions.empty:
            raise HistoricalIdentityError(
                f"The base calendar has no exit session for {rule.canonical_ticker}."
            )
        exit_session = exit_sessions[0]
        requested_end_exclusive = exit_session + timedelta(days=7)
        raw = _download_one(
            rule.provider_ticker,
            requested_start,
            requested_end_exclusive,
            downloader=downloader,
        )
        normalized, incomplete_rows, range_repairs = _normalize_download(raw, rule)
        bounded = normalized.loc[
            normalized["date"].between(requested_start, exit_session, inclusive="both")
        ].copy()
        if bounded.empty:
            raise HistoricalIdentityError(
                f"Provider returned no bounded history for {rule.provider_ticker}."
            )
        recovered_frames.append(bounded)

        source_dates = pd.DatetimeIndex(bounded["date"])
        expected_member_dates = calendar[
            (calendar >= member_start) & (calendar < member_end)
        ]
        covered_member_dates = expected_member_dates.intersection(source_dates)
        pre_member_count = int((source_dates < member_start).sum())
        exit_covered = exit_session in source_dates
        coverage = (
            len(covered_member_dates) / len(expected_member_dates)
            if len(expected_member_dates)
            else 0.0
        )
        passed = (
            len(covered_member_dates) == len(expected_member_dates)
            and pre_member_count >= minimum_history_sessions
            and exit_covered
        )
        rule_audits.append(
            HistoricalIdentityRuleAudit(
                canonical_ticker=rule.canonical_ticker,
                provider_ticker=rule.provider_ticker,
                relationship=rule.relationship,
                official_evidence_urls=rule.official_evidence_urls,
                member_start=member_start.date().isoformat(),
                member_end_exclusive=member_end.date().isoformat(),
                requested_start=requested_start.date().isoformat(),
                requested_end_exclusive=requested_end_exclusive.date().isoformat(),
                source_first_session=source_dates.min().date().isoformat(),
                source_last_session=source_dates.max().date().isoformat(),
                complete_source_rows=len(bounded),
                incomplete_source_rows=incomplete_rows,
                expanded_range_rows=len(range_repairs),
                maximum_relative_range_expansion=max(
                    (repair[1] for repair in range_repairs), default=0.0
                ),
                range_repair_sessions=tuple(repair[0] for repair in range_repairs),
                expected_member_sessions=len(expected_member_dates),
                covered_member_sessions=len(covered_member_dates),
                member_session_coverage=coverage,
                pre_member_sessions=pre_member_count,
                required_pre_member_sessions=minimum_history_sessions,
                exit_session=exit_session.date().isoformat(),
                exit_session_covered=exit_covered,
                status="mapping_coverage_passed" if passed else "mapping_coverage_failed",
            )
        )

    recovered = pd.concat(recovered_frames, ignore_index=True).sort_values(
        ["ticker", "date"], ignore_index=True
    )
    recovered.columns.name = None
    if recovered.duplicated(["date", "ticker"]).any():
        raise HistoricalIdentityError("Recovered identity prices contain duplicate rows.")
    original = _coverage_audit(
        intervals,
        price_keys,
        calendar,
        audit_start=start,
        minimum_history_sessions=minimum_history_sessions,
    )
    supplemented_keys = pd.concat(
        [price_keys, recovered.loc[:, ["date", "ticker"]]],
        ignore_index=True,
    ).drop_duplicates()
    supplemented = _coverage_audit(
        intervals,
        supplemented_keys,
        calendar,
        audit_start=start,
        minimum_history_sessions=minimum_history_sessions,
    )
    return HistoricalIdentityRecovery(
        recovered=recovered,
        rules=tuple(rule_audits),
        original_coverage=original,
        supplemented_coverage=supplemented,
    )


def write_yahoo_identity_recovery(
    membership_path: Path | str,
    existing_prices_path: Path | str,
    output_dir: Path | str,
    *,
    downloader: PriceDownloader = yf.download,
    now: datetime | None = None,
    minimum_history_sessions: int = 252,
    audit_start: str | date | pd.Timestamp = "2014-01-02",
) -> HistoricalIdentitySnapshot:
    """Write immutable local recovery rows and a hash-bound research-only manifest."""
    captured_at = _as_utc(now or datetime.now(UTC))
    membership_file = Path(membership_path)
    prices_file = Path(existing_prices_path)
    membership = pd.read_parquet(membership_file)
    prices = pd.read_parquet(prices_file, columns=["date", "ticker"])
    recovery = build_yahoo_identity_recovery(
        membership,
        prices,
        downloader=downloader,
        minimum_history_sessions=minimum_history_sessions,
        audit_start=audit_start,
    )
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = captured_at.strftime("%Y%m%dT%H%M%S%fZ")
    table_hash = tabular_sha256(recovery.recovered)
    stem = f"yahoo-identity-recovery-{stamp}-{table_hash[:12]}"
    data_path = output_root / f"{stem}.parquet"
    manifest_path = output_root / f"{stem}.manifest.json"
    recovery.recovered.to_parquet(data_path, index=False)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "historical_identity_recovery",
        "captured_at_utc": captured_at.isoformat(),
        "data_cost_policy": "no_paid_sources",
        "provider": "Yahoo Finance public endpoint via yfinance",
        "provider_access_classification": "public_unofficial_no_service_guarantee",
        "data_role": RECOVERY_ROLE,
        "data_file": data_path.name,
        "data_file_sha256": file_sha256(data_path),
        "tabular_sha256": table_hash,
        "rows": len(recovery.recovered),
        "recovered_tickers": sorted(recovery.recovered["ticker"].unique()),
        "membership_file_sha256": file_sha256(membership_file),
        "existing_prices_file_sha256": file_sha256(prices_file),
        "minimum_history_sessions": minimum_history_sessions,
        "audit_start": pd.Timestamp(audit_start).date().isoformat(),
        "identity_rules": [asdict(rule) for rule in recovery.rules],
        "original_coverage": recovery.original_coverage.to_dict(),
        "supplemented_coverage": recovery.supplemented_coverage.to_dict(),
        "raw_data_committable": False,
        "redistribution_authorized": False,
        "historical_backtest_ready": False,
        "action_authorized": False,
        "status": (
            "candidate_aliases_recovered_panel_still_unaccepted"
            if all(rule.status == "mapping_coverage_passed" for rule in recovery.rules)
            else "candidate_alias_recovery_incomplete"
        ),
        "limitations": [
            "A provider backfill is not an authoritative security master or price validation.",
            "The surrounding historical panel remains an unlicensed research input.",
            "BTUUQ-201704 and LIFE-201402 remain unresolved until official WIKI rows are acquired and audited.",
            "Corporate actions and terminal returns remain separate mandatory gates.",
        ],
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return HistoricalIdentitySnapshot(
        data_path=data_path,
        manifest_path=manifest_path,
        recovered_tickers=tuple(manifest["recovered_tickers"]),
        remaining_missing_tickers=recovery.supplemented_coverage.missing_tickers,
        historical_backtest_ready=False,
    )


def verify_yahoo_identity_recovery(manifest_path: Path | str) -> bool:
    manifest_file = Path(manifest_path)
    try:
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        expected = payload.pop("record_sha256")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(canonical).hexdigest() != expected:
            return False
        data_path = manifest_file.parent / str(payload["data_file"])
        frame = pd.read_parquet(data_path)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return False
    required = {
        "date",
        "ticker",
        "provider_ticker",
        "Open",
        "High",
        "Low",
        "Close",
        "Adj Close",
        "Volume",
        "price_basis",
    }
    return bool(
        required.issubset(frame.columns)
        and payload.get("data_file_sha256") == file_sha256(data_path)
        and payload.get("tabular_sha256") == tabular_sha256(frame)
        and payload.get("rows") == len(frame)
        and _verify_recovered_semantics(frame, payload)
        and payload.get("data_role") == RECOVERY_ROLE
        and payload.get("raw_data_committable") is False
        and payload.get("historical_backtest_ready") is False
        and payload.get("action_authorized") is False
    )


def _verify_recovered_semantics(frame: pd.DataFrame, manifest: dict[str, Any]) -> bool:
    try:
        rules = manifest["identity_rules"]
        if not isinstance(rules, list) or not rules:
            return False
        expected_pairs = {
            (str(rule["canonical_ticker"]), str(rule["provider_ticker"]))
            for rule in rules
        }
        actual_pairs = set(
            frame.loc[:, ["ticker", "provider_ticker"]]
            .drop_duplicates()
            .itertuples(index=False, name=None)
        )
        if actual_pairs != expected_pairs or frame.duplicated(["date", "ticker"]).any():
            return False
        numeric = frame.loc[:, list(REQUIRED_FIELDS)].apply(pd.to_numeric, errors="coerce")
        if numeric.isna().any().any() or not numeric.map(math.isfinite).all().all():
            return False
        if not numeric.loc[:, [*OHLC_FIELDS, "Adj Close"]].gt(0.0).all().all():
            return False
        if not numeric["Volume"].ge(0.0).all():
            return False
        if not (
            numeric["High"].ge(numeric.loc[:, list(OHLC_FIELDS)].max(axis=1)).all()
            and numeric["Low"].le(numeric.loc[:, list(OHLC_FIELDS)].min(axis=1)).all()
        ):
            return False
        dates = pd.to_datetime(frame["date"], errors="coerce")
        if dates.isna().any() or set(frame["price_basis"]) != {
            "yahoo_raw_ohlcv_plus_adjusted_close"
        }:
            return False
        for rule in rules:
            canonical = str(rule["canonical_ticker"])
            selected_dates = dates.loc[frame["ticker"].eq(canonical)]
            if (
                len(selected_dates) != int(rule["complete_source_rows"])
                or selected_dates.min() < pd.Timestamp(rule["requested_start"])
                or selected_dates.max() > pd.Timestamp(rule["exit_session"])
                or rule["status"] != "mapping_coverage_passed"
            ):
                return False
        return set(manifest["recovered_tickers"]) == {pair[0] for pair in expected_pairs}
    except (KeyError, TypeError, ValueError):
        return False


def latest_yahoo_identity_recovery_manifest(root: Path | str) -> Path | None:
    paths = sorted(Path(root).glob("yahoo-identity-recovery-*.manifest.json"), reverse=True)
    return paths[0] if paths else None


def _download_one(
    provider_ticker: str,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
    *,
    downloader: PriceDownloader,
) -> pd.DataFrame:
    try:
        return downloader(
            [provider_ticker],
            start=start.date().isoformat(),
            end=end_exclusive.date().isoformat(),
            auto_adjust=False,
            actions=False,
            group_by="column",
            progress=False,
            threads=False,
            timeout=30,
        )
    except Exception:
        raise HistoricalIdentityError(
            f"Yahoo identity-recovery request failed for {provider_ticker}."
        ) from None


def _normalize_download(
    raw: pd.DataFrame,
    rule: HistoricalIdentityRule,
) -> tuple[pd.DataFrame, int, tuple[tuple[str, float], ...]]:
    if raw.empty:
        raise HistoricalIdentityError(f"Yahoo returned no rows for {rule.provider_ticker}.")
    frame = raw.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)
    if isinstance(frame.columns, pd.MultiIndex):
        first = {str(value) for value in frame.columns.get_level_values(0)}
        second = {str(value) for value in frame.columns.get_level_values(1)}
        if set(REQUIRED_FIELDS).issubset(first):
            frame = frame.xs(rule.provider_ticker, axis=1, level=1)
        elif set(REQUIRED_FIELDS).issubset(second):
            frame = frame.xs(rule.provider_ticker, axis=1, level=0)
        else:
            raise HistoricalIdentityError("Yahoo returned an unrecognized column layout.")
    missing = set(REQUIRED_FIELDS) - {str(value) for value in frame.columns}
    if missing:
        raise HistoricalIdentityError(
            f"Yahoo identity history is missing fields: {sorted(missing)}"
        )
    values = frame.loc[:, list(REQUIRED_FIELDS)].apply(pd.to_numeric, errors="coerce")
    all_missing = values.isna().all(axis=1)
    partial = values.isna().any(axis=1) & ~all_missing
    incomplete_rows = int(partial.sum())
    values = values.loc[~all_missing & ~partial].copy()
    if values.empty:
        raise HistoricalIdentityError("Yahoo identity history has no complete OHLCV rows.")
    finite = values.map(math.isfinite).all(axis=1)
    positive = values.loc[:, [*OHLC_FIELDS, "Adj Close"]].gt(0.0).all(axis=1)
    volume_valid = values["Volume"].ge(0.0)
    if not (finite & positive & volume_valid).all():
        raise HistoricalIdentityError(
            f"Yahoo identity history failed strict OHLCV validation for {rule.provider_ticker}."
        )
    source_high = values["High"].copy()
    source_low = values["Low"].copy()
    enclosing_high = values.loc[:, list(OHLC_FIELDS)].max(axis=1)
    enclosing_low = values.loc[:, list(OHLC_FIELDS)].min(axis=1)
    range_bad = source_high.lt(enclosing_high) | source_low.gt(enclosing_low)
    expansion = pd.concat(
        [
            enclosing_high.sub(source_high).clip(lower=0.0),
            source_low.sub(enclosing_low).clip(lower=0.0),
        ],
        axis=1,
    ).max(axis=1)
    relative_expansion = expansion.div(values["Close"].abs())
    if range_bad.any() and (
        not relative_expansion.loc[range_bad].map(math.isfinite).all()
        or relative_expansion.loc[range_bad].max() > MAXIMUM_RELATIVE_RANGE_EXPANSION
    ):
        raise HistoricalIdentityError(
            f"Yahoo identity history requires an excessive OHLC range repair for "
            f"{rule.provider_ticker}."
        )
    values.loc[range_bad, "High"] = enclosing_high.loc[range_bad]
    values.loc[range_bad, "Low"] = enclosing_low.loc[range_bad]
    repairs = tuple(
        (pd.Timestamp(session).date().isoformat(), float(relative_expansion.loc[session]))
        for session in values.index[range_bad]
    )
    values.insert(0, "date", values.index)
    values.insert(1, "ticker", rule.canonical_ticker)
    values.insert(2, "provider_ticker", rule.provider_ticker)
    values["price_basis"] = "yahoo_raw_ohlcv_plus_adjusted_close"
    return values.reset_index(drop=True), incomplete_rows, repairs


def _coverage_audit(
    membership: pd.DataFrame,
    price_keys: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    *,
    audit_start: pd.Timestamp,
    minimum_history_sessions: int,
) -> StockCoverageAudit:
    audit_calendar = calendar[calendar >= audit_start]
    active_intervals = membership.loc[
        membership["end"].isna() | membership["end"].gt(audit_start)
    ]
    tickers = tuple(sorted(active_intervals["ticker"].unique()))
    universe = pd.DataFrame(False, index=audit_calendar, columns=tickers)
    for row in active_intervals.itertuples(index=False):
        mask = audit_calendar >= row.start
        if pd.notna(row.end):
            mask &= audit_calendar < row.end
        if mask.any():
            universe.loc[audit_calendar[mask], row.ticker] = True

    keys = price_keys.loc[price_keys["ticker"].isin(tickers)].drop_duplicates()
    keys = keys.assign(observed=1.0)
    close = keys.pivot(index="date", columns="ticker", values="observed")
    close = close.reindex(index=calendar, columns=tickers)
    return audit_stock_coverage(
        close,
        universe,
        minimum_history_sessions=minimum_history_sessions,
    )


def _validate_rule_against_membership(
    rule: HistoricalIdentityRule,
    membership: pd.DataFrame,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    expected_start = pd.Timestamp(rule.expected_member_start)
    expected_end = pd.Timestamp(rule.expected_member_end_exclusive)
    matches = membership.loc[
        membership["ticker"].eq(rule.canonical_ticker)
        & membership["start"].eq(expected_start)
        & membership["end"].eq(expected_end)
    ]
    if len(matches) != 1:
        raise HistoricalIdentityError(
            f"Membership interval for {rule.canonical_ticker} no longer matches the "
            "reviewed identity rule."
        )
    return expected_start, expected_end


def _normalize_membership(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"ticker", "start", "end"}
    if missing := required - set(frame.columns):
        raise HistoricalIdentityError(
            f"Membership intervals are missing columns: {sorted(missing)}"
        )
    result = frame.loc[:, ["ticker", "start", "end"]].copy()
    result["ticker"] = result["ticker"].astype(str).str.strip()
    result["start"] = pd.to_datetime(result["start"], errors="coerce")
    result["end"] = pd.to_datetime(result["end"], errors="coerce")
    invalid = (
        result["ticker"].eq("")
        | result["start"].isna()
        | (result["end"].notna() & result["end"].le(result["start"]))
    )
    if invalid.any():
        raise HistoricalIdentityError("Membership intervals contain invalid rows.")
    return result


def _normalize_price_keys(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "ticker"}
    if missing := required - set(frame.columns):
        raise HistoricalIdentityError(f"Prices are missing columns: {sorted(missing)}")
    result = frame.loc[:, ["date", "ticker"]].copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["ticker"] = result["ticker"].astype(str).str.strip()
    if result.empty or result["date"].isna().any() or result["ticker"].eq("").any():
        raise HistoricalIdentityError("Price keys contain invalid rows.")
    return result.drop_duplicates().sort_values(["date", "ticker"], ignore_index=True)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
