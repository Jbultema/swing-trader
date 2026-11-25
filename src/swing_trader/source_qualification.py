from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from swing_trader.provenance import file_sha256

QualificationStatus = Literal[
    "passed",
    "blocked",
    "failed",
    "prospective_only",
    "context_only",
]

REQUIRED_CRITERIA = (
    "free_to_use",
    "reliably_accessible",
    "historically_backtestable",
    "operationally_timely",
    "point_in_time_reproducible",
    "open_data_policy_compatible",
)


@dataclass(frozen=True)
class SourceAssessment:
    source_id: str
    provider: str
    intended_role: str
    status: QualificationStatus
    criteria: dict[str, bool]
    evidence: dict[str, object]
    limitations: tuple[str, ...]
    approved_uses: tuple[str, ...]
    prohibited_uses: tuple[str, ...]
    references: tuple[str, ...]

    @property
    def passes_all_required_criteria(self) -> bool:
        return all(self.criteria.get(name) is True for name in REQUIRED_CRITERIA)

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"passes_all_required_criteria": self.passes_all_required_criteria}


@dataclass(frozen=True)
class SourceQualificationReport:
    path: Path
    record_sha256: str
    passed_source_ids: tuple[str, ...]
    eligible_new_stock_selection_source_ids: tuple[str, ...]
    prospective_feature_work_authorized: bool
    authorized_experiment_families: tuple[str, ...]
    broader_strategy_work_authorized: bool


def write_source_qualification_report(
    output_dir: Path,
    *,
    sec_probe_path: Path | None = None,
    latest_price_manifest_path: Path | None = None,
    alpha_capability_path: Path | None = None,
    alpha_key_configured: bool = False,
    alpaca_key_configured: bool = False,
    now: datetime | None = None,
) -> SourceQualificationReport:
    """Write a redacted, immutable gate over proposed free data sources.

    A provider is not promoted because its documentation looks promising. It must
    also pass the exact access, historical, timing, point-in-time, and open-data
    requirements for the role in which Swing Trader would use it.
    """
    checked_at = _as_utc(now or datetime.now(UTC))
    sources = _assess_sources(
        checked_at=checked_at,
        sec_probe_path=sec_probe_path,
        latest_price_manifest_path=latest_price_manifest_path,
        alpha_capability_path=alpha_capability_path,
        alpha_key_configured=alpha_key_configured,
        alpaca_key_configured=alpaca_key_configured,
    )
    passed = tuple(source.source_id for source in sources if source.passes_all_required_criteria)
    stock_selection = tuple(
        source.source_id
        for source in sources
        if source.passes_all_required_criteria
        and source.intended_role in {"stock_prices_and_identity", "stock_fundamentals"}
    )
    price_sources = tuple(
        source.source_id
        for source in sources
        if source.passes_all_required_criteria
        and source.intended_role == "stock_prices_and_identity"
    )
    fundamental_sources = tuple(
        source.source_id
        for source in sources
        if source.passes_all_required_criteria and source.intended_role == "stock_fundamentals"
    )
    experiment_families: tuple[str, ...] = ()
    if price_sources:
        experiment_families += ("residual_momentum",)
    if price_sources and fundamental_sources:
        experiment_families += ("fundamental_momentum", "residual_fundamental_intersection")
    prospective_feature_work_authorized = bool(stock_selection)
    broader_strategy_work_authorized = bool(experiment_families)
    payload: dict[str, object] = {
        "schema_version": 1,
        "record_type": "data_source_qualification",
        "checked_at_utc": checked_at.isoformat(),
        "data_cost_policy": "no_paid_sources",
        "open_data_policy": (
            "public_or_open_data_required_for_strategy_inputs; proprietary_free_tiers_are_not_"
            "automatically_acceptable"
        ),
        "required_criteria": list(REQUIRED_CRITERIA),
        "sources": [source.to_dict() for source in sources],
        "passed_source_ids": list(passed),
        "eligible_new_stock_selection_source_ids": list(stock_selection),
        "qualified_stock_price_source_ids": list(price_sources),
        "qualified_stock_fundamental_source_ids": list(fundamental_sources),
        "prospective_feature_work_authorized": prospective_feature_work_authorized,
        "authorized_experiment_families": list(experiment_families),
        "broader_strategy_work_authorized": broader_strategy_work_authorized,
        "promotion_effect": "none",
        "existing_lineage_changed": False,
        "action_authorized": False,
        "interpretation": (
            "A qualified fundamental source may unlock only its point-in-time collector. A "
            "broader strategy experiment additionally requires a qualified stock-price and "
            "identity source. Passing an ancillary source does not authorize a strategy, trade, "
            "historical performance claim, or lineage reset."
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    record_hash = hashlib.sha256(canonical).hexdigest()
    payload["record_sha256"] = record_hash
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = checked_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"source-qualification-{stamp}-{record_hash[:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return SourceQualificationReport(
        path=path,
        record_sha256=record_hash,
        passed_source_ids=passed,
        eligible_new_stock_selection_source_ids=stock_selection,
        prospective_feature_work_authorized=prospective_feature_work_authorized,
        authorized_experiment_families=experiment_families,
        broader_strategy_work_authorized=broader_strategy_work_authorized,
    )


def verify_source_qualification_report(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def latest_matching_file(directory: Path, pattern: str) -> Path | None:
    paths = sorted(directory.glob(pattern)) if directory.exists() else []
    return paths[-1] if paths else None


def _assess_sources(
    *,
    checked_at: datetime,
    sec_probe_path: Path | None,
    latest_price_manifest_path: Path | None,
    alpha_capability_path: Path | None,
    alpha_key_configured: bool,
    alpaca_key_configured: bool,
) -> tuple[SourceAssessment, ...]:
    sec_probe = _load_optional_json(sec_probe_path)
    sec_probe_fresh = _artifact_is_fresh(sec_probe, checked_at, maximum_age_hours=24.0)
    sec_runtime_passed = sec_probe.get("status") == "passed" and sec_probe_fresh
    sec_status: QualificationStatus = "passed" if sec_runtime_passed else "blocked"
    sec_limitations = (
        ()
        if sec_runtime_passed
        else (
            "The current runtime did not pass a live EDGAR submissions probe.",
            "Historical research must use each as-filed accession and acceptance time; a current consolidated view may not be projected backward.",
        )
    )
    sec = SourceAssessment(
        source_id="sec_edgar_as_filed_xbrl",
        provider="U.S. SEC EDGAR",
        intended_role="stock_fundamentals",
        status=sec_status,
        criteria={
            "free_to_use": True,
            "reliably_accessible": sec_runtime_passed,
            "historically_backtestable": True,
            "operationally_timely": True,
            "point_in_time_reproducible": True,
            "open_data_policy_compatible": True,
        },
        evidence=_artifact_evidence(sec_probe_path, sec_probe)
        | {
            "documented_history_start": "2009-04-15",
            "documented_submissions_update_lag": "typically_under_one_second",
            "documented_xbrl_update_lag": "typically_under_one_minute",
            "historical_mode": "quarterly_as_filed_financial_statement_data_sets",
            "operational_mode": "submissions_and_xbrl_apis",
            "runtime_probe_fresh": sec_probe_fresh,
        },
        limitations=sec_limitations,
        approved_uses=(
            ("as_filed_fundamental_research", "prospective_filing_features")
            if sec_runtime_passed
            else ()
        ),
        prohibited_uses=(
            "current_companyfacts_projected_backward",
            "filing_date_without_acceptance_time",
            "trading_authority",
        ),
        references=(
            "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
            "https://www.sec.gov/dera/data/financial-statement-data-sets.html",
        ),
    )

    price_manifest = _load_optional_json(latest_price_manifest_path)
    price_validation = price_manifest.get("validation")
    price_manifest_fresh = _artifact_is_fresh(
        price_manifest,
        checked_at,
        maximum_age_hours=96.0,
    )
    prospective_price_passed = (
        isinstance(price_validation, dict)
        and price_manifest.get("price_data_gate_passed") is True
        and price_validation.get("status") == "passed"
        and price_manifest_fresh
    )
    yahoo = SourceAssessment(
        source_id="yahoo_via_yfinance",
        provider="Yahoo Finance via yfinance",
        intended_role="stock_prices_and_identity",
        status="prospective_only" if prospective_price_passed else "failed",
        criteria={
            "free_to_use": True,
            "reliably_accessible": prospective_price_passed,
            "historically_backtestable": False,
            "operationally_timely": prospective_price_passed,
            "point_in_time_reproducible": False,
            "open_data_policy_compatible": False,
        },
        evidence=_artifact_evidence(latest_price_manifest_path, price_manifest)
        | {
            "prospective_price_gate_passed": prospective_price_passed,
            "price_manifest_fresh": price_manifest_fresh,
            "latest_session": (
                price_validation.get("latest_session")
                if isinstance(price_validation, dict)
                else None
            ),
        },
        limitations=(
            "Unofficial public endpoint with no service guarantee or open-data license.",
            "Current adjusted history does not prove survivor-free delisted identity, terminal returns, or historical universe coverage.",
        ),
        approved_uses=("immutable_capture_forward_price_snapshot",)
        if prospective_price_passed
        else (),
        prohibited_uses=(
            "sole_historical_backtest_source",
            "current_roster_projected_backward",
            "unreviewed_ticker_aliases",
        ),
        references=("https://github.com/ranaroussi/yfinance",),
    )

    alpha_capability = _load_optional_json(alpha_capability_path)
    alpha = SourceAssessment(
        source_id="alpha_vantage_free",
        provider="Alpha Vantage",
        intended_role="independent_current_price_check",
        status="context_only" if alpha_key_configured else "blocked",
        criteria={
            "free_to_use": True,
            "reliably_accessible": alpha_key_configured,
            "historically_backtestable": False,
            "operationally_timely": alpha_key_configured,
            "point_in_time_reproducible": False,
            "open_data_policy_compatible": False,
        },
        evidence=_artifact_evidence(alpha_capability_path, alpha_capability)
        | {
            "credential_configured": alpha_key_configured,
            "documented_free_daily_points": 100,
            "documented_full_daily_history_entitlement": "premium",
            "daily_adjusted_entitlement": "premium",
        },
        limitations=(
            "The free daily endpoint exposes only the latest 100 observations; full output is premium.",
            "Adjusted daily history is premium and the documented free allowance is 25 requests per day.",
        ),
        approved_uses=("candidate_only_current_close_reconciliation",)
        if alpha_key_configured
        else (),
        prohibited_uses=(
            "full_universe_history",
            "adjusted_historical_backtest",
            "shares_outstanding_when_premium_only",
        ),
        references=("https://www.alphavantage.co/documentation/",),
    )

    alpaca = SourceAssessment(
        source_id="alpaca_basic_sip",
        provider="Alpaca Market Data",
        intended_role="stock_prices_and_identity",
        status="failed",
        criteria={
            "free_to_use": True,
            "reliably_accessible": alpaca_key_configured,
            "historically_backtestable": True,
            "operationally_timely": True,
            "point_in_time_reproducible": True,
            "open_data_policy_compatible": False,
        },
        evidence={
            "credential_configured": alpaca_key_configured,
            "documented_history_start": "2016",
            "free_sip_operational_delay_minutes": 15,
            "supports_inactive_assets": True,
            "supports_symbol_as_of_mapping": True,
            "supports_corporate_actions": True,
            "account_required": True,
        },
        limitations=(
            "The feed is a proprietary account service, not an open dataset.",
            "No credential is configured, so inactive-symbol, ticker-reuse, corporate-action, and coverage probes cannot run."
            if not alpaca_key_configured
            else "Live hard-case coverage still requires a separate evidence run.",
        ),
        approved_uses=(),
        prohibited_uses=(
            "strategy_dependency_under_current_open_data_policy",
            "broker_or_order_api",
            "historical_performance_before_hard_case_coverage_audit",
        ),
        references=(
            "https://docs.alpaca.markets/us/docs/about-market-data-api",
            "https://docs.alpaca.markets/us/docs/market-data-faq",
            "https://docs.alpaca.markets/us/reference/stockbars",
            "https://docs.alpaca.markets/us/reference/corporateactions-1",
        ),
    )

    finra = SourceAssessment(
        source_id="finra_reg_sho_daily_volume",
        provider="FINRA",
        intended_role="market_activity_context",
        status="context_only",
        criteria={
            "free_to_use": True,
            "reliably_accessible": True,
            "historically_backtestable": False,
            "operationally_timely": True,
            "point_in_time_reproducible": True,
            "open_data_policy_compatible": True,
        },
        evidence={
            "documented_api_history": "rolling_twelve_months",
            "market_scope": "finra_reported_off_exchange_activity_only",
        },
        limitations=(
            "The API history is too short for the preregistered multi-regime backtest.",
            "Daily short-sale volume is neither consolidated exchange volume nor short interest.",
        ),
        approved_uses=("prospective_neutral_activity_context",),
        prohibited_uses=("directional_short_signal", "core_historical_selector"),
        references=("https://developer.finra.org/docs",),
    )

    fred = SourceAssessment(
        source_id="fred_dgs3mo",
        provider="Federal Reserve Bank of St. Louis FRED",
        intended_role="cash_and_macro_context",
        status="passed",
        criteria=dict.fromkeys(REQUIRED_CRITERIA, True),
        evidence={
            "series": "DGS3MO",
            "availability_policy": "one_observation_lag",
            "stock_specific": False,
        },
        limitations=("A macro/cash series cannot create stock-specific short-horizon alpha.",),
        approved_uses=("cash_return_proxy", "macro_regime_context"),
        prohibited_uses=("stock_selection_unlock",),
        references=("https://fred.stlouisfed.org/series/DGS3MO",),
    )
    return sec, yahoo, alpha, alpaca, finra, fred


def _artifact_evidence(path: Path | None, payload: dict[str, object]) -> dict[str, object]:
    return {
        "artifact": None if path is None else str(path),
        "artifact_sha256": file_sha256(path) if path is not None and path.is_file() else None,
        "artifact_status": payload.get("status"),
        "artifact_checked_at_utc": payload.get("checked_at_utc") or payload.get("captured_at_utc"),
    }


def _load_optional_json(path: Path | None) -> dict[str, object]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_is_fresh(
    payload: dict[str, object],
    now: datetime,
    *,
    maximum_age_hours: float,
) -> bool:
    raw = payload.get("checked_at_utc") or payload.get("captured_at_utc")
    if not isinstance(raw, str):
        return False
    try:
        captured = _as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return False
    age_hours = (now - captured).total_seconds() / 3600.0
    return 0.0 <= age_hours <= maximum_age_hours


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
