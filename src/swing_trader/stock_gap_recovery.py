from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from swing_trader.provenance import (
    file_sha256,
    implementation_sha256,
    stock_policy_sha256,
)
from swing_trader.stock_live_data import audit_current_stock_price_snapshot
from swing_trader.stock_shadow_state import (
    StockShadowConfig,
    _advance_arm,
    load_stock_shadow_config,
    stock_shadow_lineage_id,
    verify_stock_shadow_state,
)
from swing_trader.stock_signals import ExitPolicy

GAP_RECOVERY_REASON = "missed_session_fail_closed_exit"


class StockGapRecoveryError(ValueError):
    """Raised when a missed-session gap cannot be bridged without invented signals."""


def record_fail_closed_gap_states(
    previous_state_path: Path,
    price_manifest_path: Path,
    config_path: Path,
    state_dir: Path,
    *,
    stop_before_session: str,
    now: datetime | None = None,
) -> tuple[Path, ...]:
    """Bridge missed closes using observed prices while generating no historical signals.

    The prior close's already-frozen target is allowed to execute at the first missed open.
    Every bridge close then targets cash for the following open. The bridge states are
    explicitly ineligible for prospective evidence and cannot authorize an action.
    """
    recorded_at = _as_utc(now or datetime.now(UTC))
    if not verify_stock_shadow_state(previous_state_path):
        raise StockGapRecoveryError("Previous stock state failed its content-hash check.")
    previous = _read_json(previous_state_path)
    config, _ = load_stock_shadow_config(config_path)
    expected_lineage = stock_shadow_lineage_id(config_path)
    if previous.get("lineage_id") != expected_lineage:
        raise StockGapRecoveryError("Gap recovery cannot cross a stock-policy lineage.")
    if previous.get("stock_policy_sha256") != stock_policy_sha256():
        raise StockGapRecoveryError("Gap recovery cannot cross a stock-policy implementation.")

    price_audit = audit_current_stock_price_snapshot(
        price_manifest_path,
        now=recorded_at,
    )
    if not price_audit.passed:
        raise StockGapRecoveryError(
            "Gap-recovery price snapshot failed its gate: "
            + json.dumps(price_audit.to_dict(), sort_keys=True)
        )
    price_manifest = _read_json(price_manifest_path)
    data_path = price_manifest_path.parent / str(price_manifest["data_file"])
    prices = pd.read_parquet(data_path)
    prices.columns = pd.MultiIndex.from_tuples(prices.columns, names=["field", "ticker"])
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index)).tz_localize(None)

    prior_session = pd.Timestamp(str(previous["as_of_session"]))
    stop_before = pd.Timestamp(stop_before_session)
    if prior_session not in prices.index or stop_before not in prices.index:
        raise StockGapRecoveryError(
            "Gap-recovery boundary sessions are absent from the locked price snapshot."
        )
    sessions = prices.index[(prices.index > prior_session) & (prices.index < stop_before)]
    written: list[Path] = []
    previous_path = previous_state_path
    for session in sessions:
        path = _write_gap_state(
            previous_path,
            previous,
            prices,
            session,
            price_manifest_path,
            price_manifest,
            config_path,
            config,
            state_dir,
            recorded_at,
        )
        written.append(path)
        previous_path = path
        previous = _read_json(path)
    return tuple(written)


def tracked_tickers_from_state(
    path: Path | None,
    *,
    arm_names: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Return held names and pending targets, optionally scoped to selected arms."""
    if path is None:
        return ()
    if not verify_stock_shadow_state(path):
        raise StockGapRecoveryError("Latest stock state failed its content-hash check.")
    payload = _read_json(path)
    arms = payload.get("arms")
    if not isinstance(arms, dict):
        return ()
    tickers: set[str] = set()
    selected_arms = arms.values() if arm_names is None else (arms.get(name) for name in arm_names)
    for arm in selected_arms:
        if not isinstance(arm, dict):
            continue
        for field in ("positions_at_close", "target_for_next_open"):
            values = arm.get(field)
            if isinstance(values, dict):
                tickers.update(str(ticker) for ticker in values)
    return tuple(sorted(tickers))


def _write_gap_state(
    previous_path: Path,
    previous: dict[str, object],
    prices: pd.DataFrame,
    session: pd.Timestamp,
    price_manifest_path: Path,
    price_manifest: dict[str, object],
    config_path: Path,
    config: StockShadowConfig,
    state_dir: Path,
    recorded_at: datetime,
) -> Path:
    previous_arms = previous.get("arms")
    if not isinstance(previous_arms, dict) or not previous_arms:
        raise StockGapRecoveryError("Previous stock state has no registered arms.")
    day = pd.DataFrame(index=prices["Close"].columns)
    rank = pd.Series(dtype=float)
    available_tickers = {str(value) for value in prices["Close"].columns}
    arms: dict[str, object] = {}
    for arm_name, raw_prior_arm in previous_arms.items():
        if not isinstance(raw_prior_arm, dict):
            raise StockGapRecoveryError(f"Previous arm is malformed: {arm_name}")
        policy = _exit_policy(raw_prior_arm, config)
        recovered = _advance_arm(
            str(arm_name),
            raw_prior_arm,
            session,
            prices,
            day,
            rank,
            [],
            available_tickers,
            set(),
            guarded=bool(raw_prior_arm.get("market_guard_enabled")),
            market_risk_on=False,
            config=config,
            policy=policy,
            sector_by_ticker=None,
            signal_available=False,
            signal_unavailable_reason=GAP_RECOVERY_REASON,
            guard_reason="missed_session_market_state_unavailable",
        )
        for field in (
            "prospective_role",
            "signal_family",
            "maximum_holding_sessions",
            "entry_rank_threshold",
            "rank_exit_enabled",
            "hold_rank_threshold",
            "rank_exit_multiple",
            "independent_price_validation_applies",
        ):
            if field in raw_prior_arm:
                recovered[field] = raw_prior_arm[field]
        recovered["eligible_for_diagnostic_prospective_performance"] = False
        recovered["eligible_for_primary_prospective_performance"] = False
        arms[str(arm_name)] = recovered

    benchmark = _benchmark(previous, config)
    benchmark_open = _positive_price(prices["Open"].loc[session], benchmark, "benchmark open")
    benchmark_close = _positive_price(prices["Close"].loc[session], benchmark, "benchmark close")
    inputs = dict(previous.get("inputs")) if isinstance(previous.get("inputs"), dict) else {}
    inputs.update(
        {
            "price_manifest": price_manifest_path.name,
            "price_manifest_sha256": file_sha256(price_manifest_path),
            "price_tabular_sha256": price_manifest.get("tabular_sha256"),
            "config": config_path.name,
            "config_sha256": file_sha256(config_path),
            "gap_recovery_price_role": "observed_valuation_only_no_historical_signals",
        }
    )
    payload: dict[str, object] = {
        "schema_version": 7,
        "record_type": "prospective_stock_shadow_state",
        "research_status": "paper_only_human_execution_required",
        "data_cost_policy": "no_paid_sources",
        "action_authorized": False,
        "initialization": False,
        "as_of_session": session.date().isoformat(),
        "recorded_at_utc": recorded_at.isoformat(),
        "previous_record": previous_path.name,
        "previous_record_sha256": previous.get("record_sha256"),
        "lineage_id": previous.get("lineage_id"),
        "specification": previous.get("specification"),
        "specification_sha256": previous.get("specification_sha256"),
        "stock_policy_sha256": previous.get("stock_policy_sha256"),
        "implementation_sha256": implementation_sha256(),
        "gap_recovery": {
            "status": "missed_session_recovered_fail_closed",
            "reason": "scheduled_refresh_was_not_recorded_for_this_completed_session",
            "recorded_after_session": True,
            "historical_signal_generation_authorized": False,
            "historical_entry_generation_authorized": False,
            "accounting_policy": (
                "execute_only_the_prior_frozen_target_then_target_cash_at_the_next_open"
            ),
            "price_role": "observed_ohlcv_for_valuation_and_prior_target_execution_only",
        },
        "market_state": {
            "status": "unavailable_missed_session_recovery",
            "benchmark": benchmark,
            "open": benchmark_open,
            "close": benchmark_close,
            "risk_on": False,
        },
        "market_breadth": {
            "status": "unavailable_missed_session_recovery",
            "data_gate_passed": False,
            "breadth_risk_on": False,
            "benchmark_risk_on": False,
            "combined_risk_on": False,
        },
        "independent_price_validation": {
            "status": "not_run_missed_session_recovery",
            "passed": False,
        },
        "earnings_risk_validation": {
            "status": "not_run_missed_session_recovery",
            "passed": False,
            "blocked_entries": [],
        },
        "share_turnover_data_validation": {
            "status": "not_run_missed_session_recovery",
            "passed": False,
            "historical_backfill_authorized": False,
            "primary_consensus_gate_affected": False,
        },
        "eligible_for_primary_prospective_performance": False,
        "operational_action_gate_passed": False,
        "arms": arms,
        "inputs": inputs,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    record_hash = hashlib.sha256(canonical).hexdigest()
    payload["record_sha256"] = record_hash
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = recorded_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = state_dir / f"state-{session.strftime('%Y%m%d')}-{stamp}-{record_hash[:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def _exit_policy(arm: dict[str, object], config: StockShadowConfig) -> ExitPolicy:
    return ExitPolicy(
        hard_stop_fraction=config.hard_stop_fraction,
        atr_multiple=config.atr_multiple,
        maximum_holding_sessions=int(
            arm.get("maximum_holding_sessions", config.maximum_holding_sessions)
        ),
        rank_exit_multiple=float(arm.get("rank_exit_multiple", config.rank_exit_multiple)),
        use_rank_decay=bool(arm.get("rank_exit_enabled", True)),
    )


def _benchmark(previous: dict[str, object], config: StockShadowConfig) -> str:
    market = previous.get("market_state")
    if isinstance(market, dict) and isinstance(market.get("benchmark"), str):
        return str(market["benchmark"])
    return config.benchmark


def _positive_price(series: pd.Series, ticker: str, label: str) -> float:
    value = series.get(ticker)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise StockGapRecoveryError(f"Missing or invalid {label} for {ticker}.") from exc
    if not pd.notna(numeric) or numeric <= 0.0:
        raise StockGapRecoveryError(f"Missing or invalid {label} for {ticker}.")
    return numeric


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise StockGapRecoveryError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
