from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from swing_trader.alpha_validation import verify_alpha_candidate_validation
from swing_trader.events import build_earnings_event_flags
from swing_trader.provenance import file_sha256, implementation_sha256, stock_policy_sha256
from swing_trader.stock_candidates import verify_candidate_snapshot
from swing_trader.stock_live_data import (
    audit_current_stock_price_snapshot,
    load_locked_current_universe,
)
from swing_trader.stock_shares import audit_current_stock_share_snapshot
from swing_trader.stock_signals import SCORE_COLUMNS, ExitPolicy, build_stock_features, exit_reasons
from swing_trader.stock_turnover import (
    SHARE_TURNOVER_METHOD,
    SHARE_TURNOVER_SIGNAL_FAMILY,
    ShareTurnoverSignalConfig,
    build_share_turnover_features,
)


class StockShadowStateError(ValueError):
    """Raised when a prospective stock state cannot advance without invented history."""


@dataclass(frozen=True)
class StockShadowConfig:
    maximum_positions: int
    maximum_position_weight: float
    hard_stop_fraction: float
    atr_multiple: float
    rank_exit_multiple: float
    maximum_holding_sessions: int
    benchmark: str
    moving_average_sessions: int
    volatility_sessions: int
    maximum_annualized_volatility: float
    arms: tuple[str, ...]
    short_volume_holding_sessions: tuple[int, ...]
    share_turnover_holding_sessions: tuple[int, ...]
    earnings_lead_sessions: int
    earnings_cooling_sessions: int
    round_trip_cost_bps: float
    evaluation_horizons: tuple[int, ...]


@dataclass(frozen=True)
class StockShadowRecord:
    path: Path
    as_of_session: str
    initialization: bool
    targets: dict[str, int]
    record_sha256: str


def load_stock_shadow_config(path: Path) -> tuple[StockShadowConfig, dict[str, object]]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    config = StockShadowConfig(
        maximum_positions=int(raw["portfolio"]["maximum_positions"]),
        maximum_position_weight=float(raw["portfolio"]["maximum_position_weight"]),
        hard_stop_fraction=float(raw["exit"]["hard_stop_fraction"]),
        atr_multiple=float(raw["exit"]["atr_multiple"]),
        rank_exit_multiple=float(raw["exit"]["rank_exit_multiple"]),
        maximum_holding_sessions=int(raw["exit"]["maximum_holding_sessions"]),
        benchmark=str(raw["market_guard"]["benchmark"]),
        moving_average_sessions=int(raw["market_guard"]["moving_average_sessions"]),
        volatility_sessions=int(raw["market_guard"]["volatility_sessions"]),
        maximum_annualized_volatility=float(
            raw["market_guard"]["maximum_annualized_volatility"]
        ),
        arms=tuple(str(value) for value in raw["market_guard"]["arms"]),
        short_volume_holding_sessions=tuple(
            int(value) for value in raw["experiments"]["short_volume_holding_sessions"]
        ),
        share_turnover_holding_sessions=tuple(
            int(value) for value in raw["experiments"]["share_turnover_holding_sessions"]
        ),
        earnings_lead_sessions=int(raw["events"]["earnings_lead_sessions"]),
        earnings_cooling_sessions=int(raw["events"]["earnings_cooling_sessions"]),
        round_trip_cost_bps=float(raw["evaluation"]["round_trip_cost_bps"]),
        evaluation_horizons=tuple(int(value) for value in raw["evaluation"]["horizons"]),
    )
    _validate_config(config)
    return config, raw


def record_stock_shadow_state(
    candidate_path: Path,
    universe_manifest_path: Path,
    price_manifest_path: Path,
    share_manifest_path: Path,
    config_path: Path,
    state_dir: Path,
    *,
    alpha_validation_path: Path | None = None,
    earnings_path: Path | None = None,
    now: datetime | None = None,
) -> StockShadowRecord:
    """Advance one close-to-next-open paper state, or initialize it from cash."""
    recorded_at = _as_utc(now or datetime.now(UTC))
    package_implementation_hash = implementation_sha256()
    policy_hash = stock_policy_sha256()
    if not verify_candidate_snapshot(candidate_path):
        raise StockShadowStateError("Candidate snapshot failed its content-hash check.")
    candidate = _read_json(candidate_path)
    current_session = pd.Timestamp(candidate["as_of_session"])
    candidate_inputs = candidate.get("inputs")
    if not isinstance(candidate_inputs, dict):
        raise StockShadowStateError("Candidate snapshot is missing its bound inputs.")
    if candidate_inputs.get("stock_policy_sha256") != policy_hash:
        raise StockShadowStateError(
            "Candidate snapshot was built by a different stock decision policy; rerun the screen."
        )
    _require_bound_file(
        universe_manifest_path,
        candidate_inputs.get("universe_manifest_sha256"),
        "universe manifest",
    )
    _require_bound_file(
        price_manifest_path,
        candidate_inputs.get("price_manifest_sha256"),
        "price manifest",
    )
    _require_bound_file(
        share_manifest_path,
        candidate_inputs.get("share_manifest_sha256"),
        "share manifest",
    )
    price_audit = audit_current_stock_price_snapshot(
        price_manifest_path,
        universe_manifest_path=universe_manifest_path,
        now=recorded_at,
    )
    if not price_audit.passed:
        raise StockShadowStateError(
            f"Stock-price snapshot failed its gate: {json.dumps(price_audit.to_dict())}"
        )
    share_audit = audit_current_stock_share_snapshot(
        share_manifest_path,
        universe_manifest_path=universe_manifest_path,
        require_passed_data_gate=False,
        now=recorded_at,
    )
    if not share_audit.passed:
        raise StockShadowStateError(
            f"Stock-share snapshot failed its gate: {json.dumps(share_audit.to_dict())}"
        )
    universe, universe_manifest = load_locked_current_universe(
        universe_manifest_path,
        now=recorded_at,
    )
    price_manifest = _read_json(price_manifest_path)
    prices = pd.read_parquet(price_manifest_path.parent / str(price_manifest["data_file"]))
    prices.columns = pd.MultiIndex.from_tuples(prices.columns, names=["field", "ticker"])
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index)).tz_localize(None)
    if pd.Timestamp(prices.index.max()) != current_session:
        raise StockShadowStateError("Candidate and price snapshots do not share the latest session.")
    share_manifest = _read_json(share_manifest_path)
    share_gate_passed = share_manifest.get("shares_data_gate_passed") is True
    experimental_signals = candidate.get("experimental_signals")
    share_signal = (
        experimental_signals.get(SHARE_TURNOVER_SIGNAL_FAMILY)
        if isinstance(experimental_signals, dict)
        else None
    )
    if not isinstance(share_signal, dict):
        raise StockShadowStateError("Candidate snapshot is missing its share-turnover experiment.")
    if share_signal.get("data_gate_passed") is not share_gate_passed:
        raise StockShadowStateError(
            "Candidate and share manifest disagree on the share-turnover data gate."
        )

    config, raw_config = load_stock_shadow_config(config_path)
    specification = {
        "config": raw_config,
        "signal": {
            "families": list(SCORE_COLUMNS),
            "consensus": candidate.get("consensus_method"),
            "experimental_share_turnover": {
                "family": SHARE_TURNOVER_SIGNAL_FAMILY,
                "method": SHARE_TURNOVER_METHOD,
                "holding_sessions": list(config.share_turnover_holding_sessions),
            },
            "signal_at": "regular_session_close",
            "execute_at": "next_regular_session_open",
        },
        "data_cost_policy": "no_paid_sources",
    }
    specification_hash = hashlib.sha256(
        json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    previous_path = _latest_state_path(state_dir)
    previous = None
    if previous_path is not None:
        if not verify_stock_shadow_state(previous_path):
            raise StockShadowStateError("Previous stock shadow state failed its hash check.")
        previous = _read_json(previous_path)
        _validate_state_transition(
            previous,
            current_session,
            prices.index,
            specification_hash,
            policy_hash,
        )

    current_tickers = set(universe["ticker"].astype(str))
    membership = pd.DataFrame(False, index=prices.index, columns=prices["Close"].columns)
    membership.loc[:, membership.columns.intersection(sorted(current_tickers))] = True
    features = build_stock_features(prices, membership)
    day = features.xs(current_session, level="date")
    turnover_features: pd.DataFrame | None = None
    if share_gate_passed:
        shares = pd.read_parquet(
            share_manifest_path.parent / str(share_manifest["data_file"])
        )
        turnover_features = build_share_turnover_features(
            prices,
            shares,
            current_session,
            config=ShareTurnoverSignalConfig(),
        )
        day = day.join(
            turnover_features.drop(
                columns=["close", "median_dollar_volume_20d"],
                errors="ignore",
            ),
            how="left",
        )
    consensus_rank = _consensus_rank(day)
    candidates = _consensus_tickers(candidate, config.maximum_positions)
    short_volume_rank = _family_rank(day, "short_volume")
    short_volume_candidates = _family_tickers(
        candidate,
        "short_volume",
        config.maximum_positions,
    )
    share_turnover_rank = (
        turnover_features["selection_rank"]
        if turnover_features is not None
        else pd.Series(dtype=float)
    )
    share_turnover_candidates = _experimental_tickers(
        candidate,
        SHARE_TURNOVER_SIGNAL_FAMILY,
        config.maximum_positions,
    )
    market_state = _market_state(
        prices["Close"][config.benchmark].dropna(),
        current_session,
        config,
        open_price=_required_price(
            prices["Open"].loc[current_session],
            config.benchmark,
            "benchmark open",
        ),
    )
    candidate_market_state = candidate.get("market_state")
    if not isinstance(candidate_market_state, dict):
        raise StockShadowStateError("Candidate snapshot is missing its market state.")
    if candidate_market_state.get("risk_on") is not market_state["risk_on"]:
        raise StockShadowStateError("Candidate and state market-risk calculations disagree.")

    prior_held = (
        held_tickers_from_payload(previous, arm_names=("consensus",))
        if previous is not None
        else ()
    )
    alpha_gate = _alpha_gate(
        alpha_validation_path,
        candidate,
        required_symbols={*prior_held, *candidates, config.benchmark},
        current_session=current_session,
    )
    earnings_gate, blackout = _earnings_gate(
        earnings_path,
        prices.index,
        current_session,
        recorded_at,
        config,
    )

    policy = ExitPolicy(
        hard_stop_fraction=config.hard_stop_fraction,
        atr_multiple=config.atr_multiple,
        maximum_holding_sessions=config.maximum_holding_sessions,
        rank_exit_multiple=config.rank_exit_multiple,
    )
    arm_payloads: dict[str, object] = {}
    target_counts: dict[str, int] = {}
    for arm_name in config.arms:
        guarded = arm_name == "consensus_market_guard"
        if arm_name in {"consensus", "consensus_market_guard"}:
            arm_rank = consensus_rank
            arm_candidates = candidates
            arm_policy = policy
            entry_reason = "top_consensus_entry"
            prospective_role = (
                "primary_consensus"
                if arm_name == "consensus"
                else "diagnostic_market_guard_comparator"
            )
            independently_validated = arm_name == "consensus"
            signal_family = "consensus"
            signal_available = True
        elif arm_name.startswith("short_volume_hold"):
            holding = _short_volume_holding_from_arm(arm_name, config)
            arm_rank = short_volume_rank
            arm_candidates = short_volume_candidates
            arm_policy = ExitPolicy(
                hard_stop_fraction=config.hard_stop_fraction,
                atr_multiple=config.atr_multiple,
                maximum_holding_sessions=holding,
                rank_exit_multiple=config.rank_exit_multiple,
            )
            entry_reason = "top_short_volume_entry"
            prospective_role = f"diagnostic_short_volume_max_hold_{holding}_sessions"
            independently_validated = False
            signal_family = "short_volume"
            signal_available = True
        else:
            holding = _share_turnover_holding_from_arm(arm_name, config)
            arm_rank = share_turnover_rank
            arm_candidates = share_turnover_candidates
            arm_policy = ExitPolicy(
                hard_stop_fraction=config.hard_stop_fraction,
                atr_multiple=config.atr_multiple,
                maximum_holding_sessions=holding,
                rank_exit_multiple=config.rank_exit_multiple,
            )
            entry_reason = "top_share_turnover_skip3_entry"
            prospective_role = (
                "diagnostic_academic_share_turnover_skip3_"
                f"max_hold_{holding}_sessions"
            )
            independently_validated = False
            signal_family = SHARE_TURNOVER_SIGNAL_FAMILY
            signal_available = share_gate_passed
        prior_arm = None
        if previous is not None:
            previous_arms = previous.get("arms")
            if not isinstance(previous_arms, dict) or arm_name not in previous_arms:
                raise StockShadowStateError(f"Previous state is missing arm {arm_name}.")
            prior_arm = previous_arms[arm_name]
            if not isinstance(prior_arm, dict):
                raise StockShadowStateError(f"Previous arm {arm_name} has an invalid schema.")
        arm = _advance_arm(
            arm_name,
            prior_arm,
            current_session,
            prices,
            day,
            arm_rank,
            arm_candidates,
            current_tickers,
            blackout,
            guarded=guarded,
            market_risk_on=bool(market_state["risk_on"]),
            config=config,
            policy=arm_policy,
            entry_reason=entry_reason,
            signal_available=signal_available,
            signal_unavailable_reason=(
                None
                if signal_available
                else "share_turnover_data_gate_failed_fail_safe_exit"
            ),
        )
        primary_arm = arm_name == "consensus"
        account = arm.get("paper_account_at_close")
        valuation_complete = isinstance(account, dict) and account.get(
            "valuation_complete"
        ) is True
        arm["prospective_role"] = prospective_role
        arm["signal_family"] = signal_family
        arm["maximum_holding_sessions"] = arm_policy.maximum_holding_sessions
        arm["independent_price_validation_applies"] = independently_validated
        arm["signal_data_available_at_close"] = signal_available
        arm["signal_data_unavailable_reason"] = (
            None
            if signal_available
            else "share_turnover_data_gate_failed_fail_safe_exit"
        )
        arm["eligible_for_diagnostic_prospective_performance"] = bool(
            not primary_arm and valuation_complete and signal_available
        )
        arm["eligible_for_primary_prospective_performance"] = bool(
            primary_arm
            and alpha_gate["passed"]
            and earnings_gate["passed"]
            and valuation_complete
        )
        arm_payloads[arm_name] = arm
        target_counts[arm_name] = len(arm["target_for_next_open"])

    initialization = previous is None
    primary_arm = arm_payloads["consensus"]
    assert isinstance(primary_arm, dict)
    primary_eligible = bool(primary_arm["eligible_for_primary_prospective_performance"])
    payload: dict[str, object] = {
        "schema_version": 5,
        "record_type": "prospective_stock_shadow_state",
        "research_status": "paper_only_human_execution_required",
        "data_cost_policy": "no_paid_sources",
        "action_authorized": False,
        "initialization": initialization,
        "as_of_session": current_session.date().isoformat(),
        "recorded_at_utc": recorded_at.isoformat(),
        "previous_record": previous_path.name if previous_path else None,
        "previous_record_sha256": previous.get("record_sha256") if previous else None,
        "lineage_id": stock_shadow_lineage_id(config_path),
        "specification": specification,
        "specification_sha256": specification_hash,
        "stock_policy_sha256": policy_hash,
        "implementation_sha256": package_implementation_hash,
        "market_state": market_state,
        "independent_price_validation": alpha_gate,
        "earnings_risk_validation": earnings_gate,
        "share_turnover_data_validation": {
            "status": "passed" if share_gate_passed else "failed",
            "passed": share_gate_passed,
            "artifact": share_manifest_path.name,
            "manifest_sha256": file_sha256(share_manifest_path),
            "coverage_fraction": share_manifest.get("validation", {}).get(
                "coverage_fraction"
            ) if isinstance(share_manifest.get("validation"), dict) else None,
            "stale_tickers": share_manifest.get("validation", {}).get("stale_tickers", [])
            if isinstance(share_manifest.get("validation"), dict)
            else [],
            "fetch_error_tickers": share_manifest.get("validation", {}).get(
                "fetch_error_tickers", []
            )
            if isinstance(share_manifest.get("validation"), dict)
            else [],
            "missing_tickers": share_manifest.get("validation", {}).get(
                "missing_tickers", []
            )
            if isinstance(share_manifest.get("validation"), dict)
            else [],
            "historical_backfill_authorized": False,
            "primary_consensus_gate_affected": False,
        },
        "eligible_for_primary_prospective_performance": primary_eligible,
        "operational_action_gate_passed": primary_eligible,
        "arms": arm_payloads,
        "inputs": {
            "candidate_snapshot": candidate_path.name,
            "candidate_record_sha256": candidate["record_sha256"],
            "candidate_file_sha256": file_sha256(candidate_path),
            "universe_manifest": universe_manifest_path.name,
            "universe_manifest_sha256": file_sha256(universe_manifest_path),
            "universe_tabular_sha256": universe_manifest.get("tabular_sha256"),
            "price_manifest": price_manifest_path.name,
            "price_manifest_sha256": file_sha256(price_manifest_path),
            "price_tabular_sha256": price_manifest.get("tabular_sha256"),
            "share_manifest": share_manifest_path.name,
            "share_manifest_sha256": file_sha256(share_manifest_path),
            "share_tabular_sha256": share_manifest.get("tabular_sha256"),
            "share_data_role": share_manifest.get("data_role"),
            "config": config_path.name,
            "config_sha256": file_sha256(config_path),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    record_hash = hashlib.sha256(canonical).hexdigest()
    payload["record_sha256"] = record_hash
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = recorded_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = state_dir / (
        f"state-{current_session.strftime('%Y%m%d')}-{stamp}-{record_hash[:12]}.json"
    )
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return StockShadowRecord(
        path,
        current_session.date().isoformat(),
        initialization,
        target_counts,
        record_hash,
    )


def held_tickers_from_latest_state(
    state_dir: Path,
    *,
    arm_names: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    path = _latest_state_path(state_dir)
    if path is None:
        return ()
    if not verify_stock_shadow_state(path):
        raise StockShadowStateError("Latest stock shadow state failed its hash check.")
    return held_tickers_from_payload(_read_json(path), arm_names=arm_names)


def stock_shadow_lineage_id(config_path: Path) -> str:
    """Identify one immutable decision-policy/config lineage."""
    load_stock_shadow_config(config_path)
    return (
        f"policy-{stock_policy_sha256()[:12]}-"
        f"config-{file_sha256(config_path)[:12]}"
    )


def stock_shadow_lineage_dir(root: Path, config_path: Path) -> Path:
    return root / stock_shadow_lineage_id(config_path)


def latest_stock_shadow_state(state_dir: Path) -> Path | None:
    path = _latest_state_path(state_dir)
    if path is not None and not verify_stock_shadow_state(path):
        raise StockShadowStateError("Latest stock shadow state failed its hash check.")
    return path


def held_tickers_from_payload(
    payload: dict[str, object],
    *,
    arm_names: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    arms = payload.get("arms")
    if not isinstance(arms, dict):
        return ()
    held: set[str] = set()
    selected_arms = arms.values() if arm_names is None else (arms.get(name) for name in arm_names)
    for arm in selected_arms:
        if not isinstance(arm, dict):
            continue
        positions = arm.get("positions_at_close")
        if isinstance(positions, dict):
            held.update(str(ticker) for ticker in positions)
    return tuple(sorted(held))


def verify_stock_shadow_state(path: Path) -> bool:
    payload = _read_json(path)
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def _advance_arm(
    arm_name: str,
    prior_arm: dict[str, object] | None,
    current_session: pd.Timestamp,
    prices: pd.DataFrame,
    day: pd.DataFrame,
    consensus_rank: pd.Series,
    candidates: list[str],
    current_tickers: set[str],
    blackout: set[str],
    *,
    guarded: bool,
    market_risk_on: bool,
    config: StockShadowConfig,
    policy: ExitPolicy,
    entry_reason: str = "top_consensus_entry",
    signal_available: bool = True,
    signal_unavailable_reason: str | None = None,
) -> dict[str, object]:
    if not signal_available and not signal_unavailable_reason:
        raise StockShadowStateError("An unavailable signal must have an explicit fail-safe reason.")
    prior_positions_raw = {} if prior_arm is None else prior_arm.get("positions_at_close", {})
    prior_target_raw = {} if prior_arm is None else prior_arm.get("target_for_next_open", {})
    if not isinstance(prior_positions_raw, dict) or not isinstance(prior_target_raw, dict):
        raise StockShadowStateError(f"Prior arm state is malformed for {arm_name}.")
    positions = {
        str(ticker): {"entry_session": str(details["entry_session"])}
        for ticker, details in prior_positions_raw.items()
        if isinstance(details, dict) and "entry_session" in details
    }
    prior_target = {str(ticker) for ticker in prior_target_raw}
    current_open = prices["Open"].loc[current_session]
    cash, shares, prior_cumulative_cost = _prior_paper_account(prior_arm, positions)
    share_rebase_factors = _rebase_adjusted_shares(prior_arm, shares, prices)
    equity_at_open = cash + sum(
        quantity * _required_price(current_open, ticker, "portfolio open")
        for ticker, quantity in shares.items()
    )
    one_way_cost_fraction = config.round_trip_cost_bps / 20_000.0
    executions: list[dict[str, object]] = []
    trading_cost = 0.0
    for ticker in sorted(set(positions) - prior_target):
        price = _required_price(current_open, ticker, "exit open")
        quantity = shares.pop(ticker)
        gross_notional = quantity * price
        estimated_cost = gross_notional * one_way_cost_fraction
        cash += gross_notional - estimated_cost
        trading_cost += estimated_cost
        executions.append(
            {
                "ticker": ticker,
                "action": "SELL",
                "price": price,
                "shares": quantity,
                "gross_notional": gross_notional,
                "estimated_cost": estimated_cost,
                "cash_flow": gross_notional - estimated_cost,
                "reason": "prior_close_target",
            }
        )
        del positions[ticker]

    entry_tickers = sorted(prior_target - set(positions))
    desired = {
        ticker: equity_at_open * float(prior_target_raw[ticker])
        for ticker in entry_tickers
    }
    desired_total = sum(desired.values())
    affordable_gross = cash / (1.0 + one_way_cost_fraction)
    purchase_scale = min(1.0, affordable_gross / desired_total) if desired_total > 0.0 else 0.0
    for ticker in entry_tickers:
        price = _required_price(current_open, ticker, "entry open")
        gross_notional = desired[ticker] * purchase_scale
        if gross_notional <= 0.0:
            raise StockShadowStateError(f"Paper account cannot fund target entry for {ticker}.")
        quantity = gross_notional / price
        estimated_cost = gross_notional * one_way_cost_fraction
        cash -= gross_notional + estimated_cost
        trading_cost += estimated_cost
        shares[ticker] = quantity
        positions[ticker] = {"entry_session": current_session.date().isoformat()}
        executions.append(
            {
                "ticker": ticker,
                "action": "BUY",
                "price": price,
                "shares": quantity,
                "gross_notional": gross_notional,
                "estimated_cost": estimated_cost,
                "cash_flow": -(gross_notional + estimated_cost),
                "reason": "prior_close_target",
            }
        )
    if cash < -1e-10:
        raise StockShadowStateError("Paper account spent more cash than it held.")
    cash = max(0.0, cash)
    if set(shares) != set(positions):
        raise StockShadowStateError("Paper account shares disagree with logical positions.")

    enriched: dict[str, dict[str, object]] = {}
    exits: dict[str, list[str]] = {}
    for ticker, position in positions.items():
        entry_session = pd.Timestamp(position["entry_session"])
        if entry_session not in prices.index:
            raise StockShadowStateError(f"Entry session is outside the current price snapshot: {ticker}")
        entry_price = _required_price(prices["Open"].loc[entry_session], ticker, "entry basis")
        path = prices["Close"][ticker].loc[entry_session:current_session].dropna()
        if path.empty or path.index.max() != current_session:
            exits[ticker] = ["missing_close"]
            high_watermark = float(path.max()) if not path.empty else entry_price
            holding_sessions = len(path)
        elif not signal_available:
            high_watermark = float(path.max())
            holding_sessions = len(path)
            assert signal_unavailable_reason is not None
            exits[ticker] = [signal_unavailable_reason]
        else:
            high_watermark = float(path.max())
            holding_sessions = len(path)
            if ticker not in current_tickers:
                exits[ticker] = ["left_current_universe"]
            elif ticker not in day.index or not bool(day.loc[ticker, "eligible"]):
                exits[ticker] = ["eligibility_filter_failed"]
            else:
                reasons = exit_reasons(
                    day.loc[ticker],
                    entry_price=entry_price,
                    high_watermark=high_watermark,
                    holding_sessions=holding_sessions,
                    cross_section_rank=float(consensus_rank.get(ticker, np.inf)),
                    entry_top_n=config.maximum_positions,
                    policy=policy,
                )
                if reasons:
                    exits[ticker] = reasons
        if guarded and not market_risk_on:
            exits.setdefault(ticker, []).append("market_regime_risk_off")
        enriched[ticker] = {
            "entry_session": entry_session.date().isoformat(),
            "entry_adjusted_open": entry_price,
            "high_watermark_adjusted_close": high_watermark,
            "holding_sessions": holding_sessions,
            "current_adjusted_close": (
                float(path.loc[current_session]) if current_session in path.index else None
            ),
            "paper_shares": shares[ticker],
        }

    survivors = set(positions) - set(exits)
    selected = set(survivors)
    skipped: dict[str, list[str]] = {}
    allow_entries = not guarded or market_risk_on
    for ticker in candidates:
        if len(selected) >= config.maximum_positions:
            break
        if ticker in exits:
            continue
        if ticker in blackout and ticker not in positions:
            skipped[ticker] = ["scheduled_earnings_entry_blackout"]
            continue
        if not allow_entries and ticker not in positions:
            skipped[ticker] = ["market_regime_risk_off"]
            continue
        selected.add(ticker)

    weight = min(config.maximum_position_weight, 1.0 / config.maximum_positions)
    target = dict.fromkeys(sorted(selected), weight)
    decisions: list[dict[str, object]] = []
    for ticker in sorted(exits):
        decisions.append(
            _decision(
                ticker,
                "SELL",
                exits[ticker],
                current_session,
                day,
                consensus_rank,
                enriched[ticker],
            )
        )
    for ticker in sorted(survivors):
        decisions.append(
            _decision(
                ticker,
                "HOLD",
                ["exit_conditions_clear"],
                current_session,
                day,
                consensus_rank,
                enriched[ticker],
            )
        )
    for ticker in candidates:
        if ticker in selected and ticker not in positions:
            decisions.append(
                _decision(
                    ticker,
                    "BUY",
                    [entry_reason],
                    current_session,
                    day,
                    consensus_rank,
                    None,
                )
            )
    for ticker, reasons in skipped.items():
        decisions.append(
            _decision(
                ticker,
                "SKIP",
                reasons,
                current_session,
                day,
                consensus_rank,
                enriched.get(ticker),
            )
        )
    account = _mark_paper_account(
        cash,
        shares,
        prices["Close"].loc[current_session],
        equity_at_open=equity_at_open,
        trading_cost=trading_cost,
        cumulative_trading_cost=prior_cumulative_cost + trading_cost,
        one_way_cost_fraction=one_way_cost_fraction,
        gross_traded_notional=sum(
            float(row["gross_notional"])
            for row in executions
        ),
        mark_session=current_session,
        share_rebase_factors=share_rebase_factors,
    )
    return {
        "arm": arm_name,
        "signal_data_available_at_close": signal_available,
        "signal_data_unavailable_reason": signal_unavailable_reason,
        "market_guard_enabled": guarded,
        "executions_at_open": executions,
        "positions_at_close": enriched,
        "target_for_next_open": target,
        "target_cash_weight": 1.0 - sum(target.values()),
        "decisions": decisions,
        "paper_account_at_close": account,
    }


def _decision(
    ticker: str,
    action: str,
    reasons: list[str],
    session: pd.Timestamp,
    day: pd.DataFrame,
    consensus_rank: pd.Series,
    position: dict[str, object] | None,
) -> dict[str, object]:
    row = day.loc[ticker] if ticker in day.index else pd.Series(dtype=object)
    rank = _finite_float(consensus_rank.get(ticker))
    return {
        "as_of_session": session.date().isoformat(),
        "effective_at": "next_regular_session_open",
        "ticker": ticker,
        "action": action,
        "reasons": reasons,
        "selection_rank": rank,
        "consensus_rank": rank,
        "close": _finite_float(row.get("close")),
        "return_21d": _finite_float(row.get("return_21d")),
        "return_63d": _finite_float(row.get("return_63d")),
        "return_12_1": _finite_float(row.get("return_12_1")),
        "proximity_52w_high": _finite_float(row.get("proximity_52w_high")),
        "volume_ratio_20_126": _finite_float(row.get("volume_ratio_20_126")),
        "formation_return_t20_t3": _finite_float(row.get("formation_return_t20_t3")),
        "share_turnover_t20_t3": _finite_float(row.get("share_turnover_t20_t3")),
        "shares_outstanding_at_capture": _finite_float(
            row.get("shares_outstanding_at_capture")
        ),
        "return_percentile": _finite_float(row.get("return_percentile")),
        "share_turnover_percentile": _finite_float(
            row.get("share_turnover_percentile")
        ),
        "atr_fraction_14d": _finite_float(row.get("atr_fraction_14d")),
        "position": position,
    }


def _consensus_rank(day: pd.DataFrame) -> pd.Series:
    scores = day[list(SCORE_COLUMNS.values())].mean(axis=1)
    eligible = (
        day["eligible"].fillna(False).astype(bool)
        & day["trend_positive"].fillna(False).astype(bool)
    )
    return scores.where(eligible).rank(ascending=False, method="average")


def _consensus_tickers(candidate: dict[str, object], maximum_positions: int) -> list[str]:
    rows = candidate.get("consensus_candidates")
    if not isinstance(rows, list):
        raise StockShadowStateError("Candidate snapshot is missing consensus candidates.")
    tickers = [str(row["ticker"]) for row in rows if isinstance(row, dict) and "ticker" in row]
    if not tickers or len(tickers) > maximum_positions:
        raise StockShadowStateError("Candidate consensus size is invalid.")
    return tickers


def _family_rank(day: pd.DataFrame, family: str) -> pd.Series:
    score_column = SCORE_COLUMNS[family]
    eligible = (
        day["eligible"].fillna(False).astype(bool)
        & day["trend_positive"].fillna(False).astype(bool)
    )
    return day[score_column].where(eligible).rank(ascending=False, method="average")


def _family_tickers(
    candidate: dict[str, object],
    family: str,
    maximum_positions: int,
) -> list[str]:
    rows = candidate.get("family_candidates")
    if not isinstance(rows, list):
        raise StockShadowStateError("Candidate snapshot is missing family candidates.")
    selected = sorted(
        (
            row
            for row in rows
            if isinstance(row, dict) and row.get("signal_family") == family
        ),
        key=lambda row: int(row["rank"]),
    )
    tickers = [str(row["ticker"]) for row in selected[:maximum_positions]]
    if not tickers or len(tickers) > maximum_positions or len(tickers) != len(set(tickers)):
        raise StockShadowStateError(f"Candidate family size is invalid: {family}")
    return tickers


def _experimental_tickers(
    candidate: dict[str, object],
    family: str,
    maximum_positions: int,
) -> list[str]:
    signals = candidate.get("experimental_signals")
    if not isinstance(signals, dict):
        raise StockShadowStateError("Candidate snapshot is missing experimental signals.")
    signal = signals.get(family)
    if not isinstance(signal, dict):
        raise StockShadowStateError(f"Candidate snapshot is missing experiment {family}.")
    data_gate_passed = signal.get("data_gate_passed")
    if data_gate_passed is False:
        if signal.get("status") != "data_gate_failed" or signal.get("candidates") != []:
            raise StockShadowStateError(
                f"Unavailable experimental candidates are malformed for {family}."
            )
        return []
    if data_gate_passed is not True or signal.get("status") != "available":
        raise StockShadowStateError(f"Experimental data-gate status is invalid for {family}.")
    rows = signal.get("candidates")
    if not isinstance(rows, list):
        raise StockShadowStateError(f"Experimental candidates are missing for {family}.")
    selected = sorted(
        (row for row in rows if isinstance(row, dict) and row.get("ticker")),
        key=lambda row: int(row["rank"]),
    )
    tickers = [str(row["ticker"]) for row in selected[:maximum_positions]]
    if not tickers or len(tickers) > maximum_positions or len(tickers) != len(set(tickers)):
        raise StockShadowStateError(f"Experimental candidate size is invalid: {family}")
    return tickers


def _short_volume_holding_from_arm(
    arm_name: str,
    config: StockShadowConfig,
) -> int:
    prefix = "short_volume_hold"
    if not arm_name.startswith(prefix):
        raise StockShadowStateError(f"Unsupported stock shadow arm: {arm_name}")
    try:
        holding = int(arm_name.removeprefix(prefix))
    except ValueError as exc:
        raise StockShadowStateError(f"Invalid stock shadow arm: {arm_name}") from exc
    if holding not in config.short_volume_holding_sessions:
        raise StockShadowStateError(f"Unregistered stock shadow arm: {arm_name}")
    return holding


def _share_turnover_holding_from_arm(
    arm_name: str,
    config: StockShadowConfig,
) -> int:
    prefix = "share_turnover_hold"
    if not arm_name.startswith(prefix):
        raise StockShadowStateError(f"Unsupported stock shadow arm: {arm_name}")
    try:
        holding = int(arm_name.removeprefix(prefix))
    except ValueError as exc:
        raise StockShadowStateError(f"Invalid stock shadow arm: {arm_name}") from exc
    if holding not in config.share_turnover_holding_sessions:
        raise StockShadowStateError(f"Unregistered stock shadow arm: {arm_name}")
    return holding


def _market_state(
    close: pd.Series,
    current_session: pd.Timestamp,
    config: StockShadowConfig,
    *,
    open_price: float,
) -> dict[str, object]:
    history = close.loc[:current_session]
    moving_average = float(history.rolling(config.moving_average_sessions).mean().iloc[-1])
    volatility = float(
        history.pct_change(fill_method=None).rolling(config.volatility_sessions).std().iloc[-1]
        * np.sqrt(252)
    )
    latest = float(history.iloc[-1])
    risk_on = latest > moving_average and volatility <= config.maximum_annualized_volatility
    return {
        "benchmark": config.benchmark,
        "open": open_price,
        "close": latest,
        "moving_average": moving_average,
        "moving_average_sessions": config.moving_average_sessions,
        "realized_volatility_annualized": volatility,
        "volatility_sessions": config.volatility_sessions,
        "maximum_annualized_volatility": config.maximum_annualized_volatility,
        "risk_on": risk_on,
    }


def _prior_paper_account(
    prior_arm: dict[str, object] | None,
    positions: dict[str, dict[str, str]],
) -> tuple[float, dict[str, float], float]:
    if prior_arm is None:
        return 1.0, {}, 0.0
    raw = prior_arm.get("paper_account_at_close")
    if not isinstance(raw, dict):
        raise StockShadowStateError("Prior arm is missing its self-financing paper account.")
    try:
        cash = float(raw["cash"])
        cumulative_cost = float(raw["cumulative_trading_cost"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StockShadowStateError("Prior paper account has invalid cash or cost fields.") from exc
    raw_shares = raw.get("shares")
    if not isinstance(raw_shares, dict):
        raise StockShadowStateError("Prior paper account has invalid shares.")
    shares = {str(ticker): float(value) for ticker, value in raw_shares.items()}
    if (
        not np.isfinite(cash)
        or cash < -1e-10
        or not np.isfinite(cumulative_cost)
        or cumulative_cost < 0.0
        or any(not np.isfinite(value) or value <= 0.0 for value in shares.values())
    ):
        raise StockShadowStateError("Prior paper account contains impossible values.")
    if set(shares) != set(positions):
        raise StockShadowStateError("Prior paper account shares disagree with positions.")
    return max(0.0, cash), shares, cumulative_cost


def _mark_paper_account(
    cash: float,
    shares: dict[str, float],
    close: pd.Series,
    *,
    equity_at_open: float,
    trading_cost: float,
    cumulative_trading_cost: float,
    one_way_cost_fraction: float,
    gross_traded_notional: float,
    mark_session: pd.Timestamp,
    share_rebase_factors: dict[str, float],
) -> dict[str, object]:
    marks: dict[str, float | None] = {}
    market_values: dict[str, float | None] = {}
    for ticker, quantity in sorted(shares.items()):
        value = _finite_float(close.get(ticker))
        if value is None or value <= 0.0:
            marks[ticker] = None
            market_values[ticker] = None
        else:
            marks[ticker] = value
            market_values[ticker] = quantity * value
    valuation_complete = all(value is not None for value in market_values.values())
    gross_exposure = (
        sum(float(value) for value in market_values.values() if value is not None)
        if valuation_complete
        else None
    )
    total_equity = cash + gross_exposure if gross_exposure is not None else None
    return {
        "base_currency": "USD",
        "accounting": "fractional_shares_no_survivor_rebalancing",
        "adjustment_policy": "rebase_shares_to_prior_locked_close_mark",
        "starting_equity": 1.0,
        "mark_session": mark_session.date().isoformat(),
        "cash": cash,
        "shares": dict(sorted(shares.items())),
        "share_rebase_factors_at_open": dict(sorted(share_rebase_factors.items())),
        "adjusted_close_marks": marks,
        "market_values": market_values,
        "gross_exposure": gross_exposure,
        "total_equity": total_equity,
        "cash_weight": (
            cash / total_equity if total_equity is not None and total_equity > 0.0 else None
        ),
        "valuation_complete": valuation_complete,
        "equity_at_open_before_costs": equity_at_open,
        "gross_turnover_at_open": (
            gross_traded_notional / equity_at_open if equity_at_open > 0.0 else None
        ),
        "one_way_cost_fraction": one_way_cost_fraction,
        "trading_cost_this_open": trading_cost,
        "cumulative_trading_cost": cumulative_trading_cost,
    }


def _rebase_adjusted_shares(
    prior_arm: dict[str, object] | None,
    shares: dict[str, float],
    prices: pd.DataFrame,
) -> dict[str, float]:
    if prior_arm is None or not shares:
        return {}
    account = prior_arm.get("paper_account_at_close")
    if not isinstance(account, dict):
        raise StockShadowStateError("Prior arm is missing its paper account for adjustment rebasing.")
    marks = account.get("adjusted_close_marks")
    if not isinstance(marks, dict):
        raise StockShadowStateError("Prior paper account is missing adjusted close marks.")
    mark_session = pd.Timestamp(str(account.get("mark_session")))
    if mark_session not in prices.index:
        raise StockShadowStateError("Prior account mark session is outside the current snapshot.")
    factors: dict[str, float] = {}
    for ticker in sorted(shares):
        prior_mark = _finite_float(marks.get(ticker))
        revised_mark = _required_price(
            prices["Close"].loc[mark_session],
            ticker,
            "revised prior close",
        )
        if prior_mark is None or prior_mark <= 0.0:
            raise StockShadowStateError(f"Prior adjusted close mark is invalid for {ticker}.")
        factor = prior_mark / revised_mark
        shares[ticker] *= factor
        factors[ticker] = factor
    return factors


def _alpha_gate(
    path: Path | None,
    candidate: dict[str, object],
    *,
    required_symbols: set[str],
    current_session: pd.Timestamp,
) -> dict[str, object]:
    if path is None:
        return {
            "status": "missing",
            "passed": False,
            "required_symbols": sorted(required_symbols),
        }
    if not verify_alpha_candidate_validation(path):
        raise StockShadowStateError("Alpha candidate validation failed its hash check.")
    payload = _read_json(path)
    if payload.get("candidate_record_sha256") != candidate.get("record_sha256"):
        raise StockShadowStateError("Alpha validation is bound to a different candidate snapshot.")
    validation = payload.get("validation")
    checks = payload.get("symbol_checks")
    if not isinstance(validation, dict) or not isinstance(checks, list):
        raise StockShadowStateError("Alpha validation has an invalid schema.")
    checked_symbols = {
        str(row["symbol"])
        for row in checks
        if isinstance(row, dict) and row.get("passed") is True
    }
    missing = sorted(required_symbols - checked_symbols)
    passed = (
        validation.get("status") == "passed"
        and validation.get("expected_session") == current_session.date().isoformat()
        and not missing
    )
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "artifact": path.name,
        "record_sha256": payload.get("record_sha256"),
        "required_symbols": sorted(required_symbols),
        "missing_or_failed_symbols": missing,
    }


def _earnings_gate(
    path: Path | None,
    sessions: pd.DatetimeIndex,
    current_session: pd.Timestamp,
    recorded_at: datetime,
    config: StockShadowConfig,
) -> tuple[dict[str, object], set[str]]:
    if path is None:
        return {"status": "missing", "passed": False, "blocked_entries": []}, set()
    manifest_path = path.with_suffix(".manifest.json")
    if not manifest_path.exists():
        raise StockShadowStateError("Earnings snapshot manifest is missing.")
    manifest = _read_json(manifest_path)
    if manifest.get("role") != "prospective_event_risk_only_not_historical_backfill":
        raise StockShadowStateError("Earnings snapshot does not have a prospective-only role.")
    if file_sha256(path) != manifest.get("parquet_sha256"):
        raise StockShadowStateError("Earnings snapshot hash does not match its manifest.")
    captured = _as_utc(datetime.fromisoformat(str(manifest["captured_at_utc"])))
    if captured > recorded_at:
        raise StockShadowStateError("Earnings snapshot was captured after the decision record.")
    calendar = pd.read_parquet(path)
    flags = build_earnings_event_flags(
        calendar,
        sessions,
        lead_sessions=config.earnings_lead_sessions,
        cooling_sessions=config.earnings_cooling_sessions,
    )
    blocked = set(
        flags.loc[
            (flags["date"] == current_session) & flags["entry_blocked"], "ticker"
        ].astype(str)
    )
    return {
        "status": "passed",
        "passed": True,
        "artifact": path.name,
        "manifest_sha256": file_sha256(manifest_path),
        "captured_at_utc": captured.isoformat(),
        "blocked_entries": sorted(blocked),
    }, blocked


def _validate_state_transition(
    previous: dict[str, object],
    current_session: pd.Timestamp,
    sessions: pd.DatetimeIndex,
    specification_hash: str,
    policy_hash: str,
) -> None:
    prior_session = pd.Timestamp(previous["as_of_session"])
    if current_session <= prior_session:
        raise StockShadowStateError("A stock state already exists for this or a later session.")
    if prior_session not in sessions or current_session not in sessions:
        raise StockShadowStateError("State transition sessions are missing from the price snapshot.")
    if sessions.get_loc(current_session) - sessions.get_loc(prior_session) != 1:
        raise StockShadowStateError(
            "Stock state has a session gap; current membership cannot be used to invent missed states."
        )
    if previous.get("specification_sha256") != specification_hash:
        raise StockShadowStateError("Stock shadow specification changed; start a new state directory.")
    if previous.get("stock_policy_sha256") != policy_hash:
        raise StockShadowStateError(
            "Stock shadow decision policy changed; start a new state directory."
        )


def _validate_config(config: StockShadowConfig) -> None:
    if config.maximum_positions < 1:
        raise ValueError("maximum_positions must be positive.")
    if not 0.0 < config.maximum_position_weight <= 1.0:
        raise ValueError("maximum_position_weight must be in (0, 1].")
    if config.maximum_positions * config.maximum_position_weight > 1.0:
        raise ValueError("Stock shadow maximum weights exceed 100% gross exposure.")
    if not config.short_volume_holding_sessions:
        raise ValueError("Stock shadow must register short-volume holding experiments.")
    if any(value < 1 for value in config.short_volume_holding_sessions):
        raise ValueError("Short-volume holding sessions must be positive.")
    if len(set(config.short_volume_holding_sessions)) != len(
        config.short_volume_holding_sessions
    ):
        raise ValueError("Short-volume holding sessions must be unique.")
    if not config.share_turnover_holding_sessions:
        raise ValueError("Stock shadow must register share-turnover holding experiments.")
    if any(value < 1 for value in config.share_turnover_holding_sessions):
        raise ValueError("Share-turnover holding sessions must be positive.")
    if len(set(config.share_turnover_holding_sessions)) != len(
        config.share_turnover_holding_sessions
    ):
        raise ValueError("Share-turnover holding sessions must be unique.")
    expected_arms = {
        "consensus",
        "consensus_market_guard",
        *(f"short_volume_hold{value}" for value in config.short_volume_holding_sessions),
        *(
            f"share_turnover_hold{value}"
            for value in config.share_turnover_holding_sessions
        ),
    }
    if set(config.arms) != expected_arms or len(config.arms) != len(expected_arms):
        raise ValueError("Stock shadow arms do not match the frozen experiment registry.")
    if config.maximum_holding_sessions < 1:
        raise ValueError("maximum_holding_sessions must be positive.")
    if config.round_trip_cost_bps < 0.0:
        raise ValueError("round_trip_cost_bps cannot be negative.")


def _latest_state_path(state_dir: Path) -> Path | None:
    paths = sorted(state_dir.glob("state-*.json")) if state_dir.exists() else []
    return paths[-1] if paths else None


def _require_bound_file(path: Path, expected: object, label: str) -> None:
    if not path.exists() or not isinstance(expected, str) or file_sha256(path) != expected:
        raise StockShadowStateError(f"Candidate-bound {label} does not match.")


def _required_price(series: pd.Series, ticker: str, label: str) -> float:
    value = series.get(ticker)
    if value is None or not np.isfinite(float(value)) or float(value) <= 0.0:
        raise StockShadowStateError(f"Missing or invalid {label} for {ticker}.")
    return float(value)


def _finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise StockShadowStateError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
