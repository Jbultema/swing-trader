from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from swing_trader.provenance import implementation_sha256, stock_evaluation_sha256
from swing_trader.stock_shadow_state import verify_stock_shadow_state
from swing_trader.stock_validation import (
    paired_stationary_bootstrap,
    stationary_bootstrap_family_validation,
)

MINIMUM_ELIGIBLE_SESSIONS = 126
MINIMUM_COMPLETED_EXITS = 30
MINIMUM_RISK_OFF_SESSIONS = 10
MINIMUM_DIAGNOSTIC_PAIRED_SESSIONS = 21
MINIMUM_DIAGNOSTIC_FAMILY_SESSIONS = 63


class StockProspectiveEvaluationError(ValueError):
    """Raised when a stock state lineage is incomplete, modified, or incomparable."""


def evaluate_stock_shadow_lineage(state_dir: Path) -> dict[str, object]:
    states = _load_state_lineage(state_dir)
    if not states:
        raise StockProspectiveEvaluationError(f"No stock states found in {state_dir}.")
    lineage_id = str(states[0][1].get("lineage_id"))
    stock_policy_hash = _state_policy_hash(states[0][1])
    sessions = [pd.Timestamp(str(payload["as_of_session"])) for _, payload in states]
    primary_equity = [_account_equity(payload, "consensus") for _, payload in states]
    guarded_equity = [
        _account_equity(payload, "consensus_market_guard") for _, payload in states
    ]
    diagnostic_arm_names = _diagnostic_arm_names(states)
    diagnostic_equity = {
        arm_name: [_account_equity(payload, arm_name) for _, payload in states]
        for arm_name in diagnostic_arm_names
    }
    arm_equity = {
        "consensus": primary_equity,
        "consensus_market_guard": guarded_equity,
        **diagnostic_equity,
    }
    spy_equity = _spy_equity(states)
    transitions: list[dict[str, object]] = []
    eligible_primary: list[float] = []
    eligible_spy: list[float] = []
    eligible_sessions: list[pd.Timestamp] = []
    eligible_exits = 0
    eligible_risk_off = 0
    for position in range(1, len(states)):
        prior = states[position - 1][1]
        current = states[position][1]
        primary_return = _return(primary_equity[position - 1], primary_equity[position])
        guarded_return = _return(guarded_equity[position - 1], guarded_equity[position])
        diagnostic_returns = {
            arm_name: _return(values[position - 1], values[position])
            for arm_name, values in diagnostic_equity.items()
        }
        spy_return = _return(spy_equity[position - 1], spy_equity[position])
        return_attribution = {
            "arms": {
                arm_name: _arm_return_attribution(
                    arm_equity[arm_name][position - 1],
                    current,
                    arm_name,
                )
                for arm_name in arm_equity
            },
            "spy_buy_hold": _spy_return_attribution(
                states,
                position,
                spy_equity[position - 1],
                spy_equity[position],
            ),
        }
        current_primary = _arm(current, "consensus")
        current_account = current_primary.get("paper_account_at_close")
        valuation_complete = isinstance(current_account, dict) and current_account.get(
            "valuation_complete"
        ) is True
        eligible = bool(
            prior.get("eligible_for_primary_prospective_performance") is True
            and valuation_complete
            and primary_return is not None
            and spy_return is not None
        )
        exits = sum(
            1
            for row in current_primary.get("executions_at_open", [])
            if isinstance(row, dict) and row.get("action") == "SELL"
        )
        risk_on = _risk_on(prior)
        transitions.append(
            {
                "from_close": sessions[position - 1].date().isoformat(),
                "to_close": sessions[position].date().isoformat(),
                "decision_gate_from_prior_close_passed": prior.get(
                    "eligible_for_primary_prospective_performance"
                )
                is True,
                "current_valuation_complete": valuation_complete,
                "eligible": eligible,
                "primary_net_return": primary_return,
                "market_guard_net_return": guarded_return,
                "diagnostic_arm_net_returns": diagnostic_returns,
                "spy_buy_hold_return": spy_return,
                "return_attribution": return_attribution,
                "primary_net_excess_vs_spy": (
                    primary_return - spy_return
                    if primary_return is not None and spy_return is not None
                    else None
                ),
                "primary_exits_at_open": exits,
                "prior_market_risk_on": risk_on,
            }
        )
        if eligible:
            eligible_primary.append(float(primary_return))
            eligible_spy.append(float(spy_return))
            eligible_sessions.append(sessions[position])
            eligible_exits += exits
            eligible_risk_off += int(not risk_on)

    all_index = pd.DatetimeIndex(sessions[1:])
    all_primary = pd.Series(
        [_return(primary_equity[i - 1], primary_equity[i]) for i in range(1, len(states))],
        index=all_index,
        dtype=float,
    )
    all_guarded = pd.Series(
        [_return(guarded_equity[i - 1], guarded_equity[i]) for i in range(1, len(states))],
        index=all_index,
        dtype=float,
    )
    all_spy = pd.Series(
        [_return(spy_equity[i - 1], spy_equity[i]) for i in range(1, len(states))],
        index=all_index,
        dtype=float,
    )
    all_diagnostics = {
        arm_name: pd.Series(
            [_return(values[i - 1], values[i]) for i in range(1, len(states))],
            index=all_index,
            dtype=float,
        )
        for arm_name, values in diagnostic_equity.items()
    }
    eligible_primary_series = pd.Series(
        eligible_primary,
        index=pd.DatetimeIndex(eligible_sessions),
        dtype=float,
    )
    eligible_spy_series = pd.Series(
        eligible_spy,
        index=pd.DatetimeIndex(eligible_sessions),
        dtype=float,
    )
    readiness_reasons = []
    if len(eligible_primary_series) < MINIMUM_ELIGIBLE_SESSIONS:
        readiness_reasons.append("minimum_eligible_sessions_not_met")
    if eligible_exits < MINIMUM_COMPLETED_EXITS:
        readiness_reasons.append("minimum_completed_exits_not_met")
    if eligible_risk_off < MINIMUM_RISK_OFF_SESSIONS:
        readiness_reasons.append("minimum_risk_off_sessions_not_met")
    bootstrap = None
    if len(eligible_primary_series) >= 21:
        bootstrap = paired_stationary_bootstrap(
            eligible_primary_series,
            eligible_spy_series,
            mean_block_sessions=21,
            samples=2_000,
            seed=20261007,
        )
    horizon_outcomes = _horizon_outcomes(states, sessions, primary_equity)
    diagnostic_comparisons = _diagnostic_arm_comparisons(
        all_diagnostics,
        all_primary,
        all_spy,
    )
    session_return_attribution = _aggregate_return_attribution(
        transitions,
        tuple(arm_equity),
    )
    return {
        "schema_version": 5,
        "record_type": "prospective_stock_shadow_evaluation",
        "research_status": "prospective_paper_only_not_trading_authority",
        "data_cost_policy": "no_paid_sources",
        "action_authorized": False,
        "lineage_id": lineage_id,
        "stock_policy_sha256": stock_policy_hash,
        "state_package_implementation_sha256": sorted(
            {
                str(payload["implementation_sha256"])
                for _, payload in states
                if isinstance(payload.get("implementation_sha256"), str)
            }
        ),
        "evaluation_implementation_sha256": stock_evaluation_sha256(),
        "evaluation_package_implementation_sha256": implementation_sha256(),
        "states_seen": len(states),
        "state_record_sha256": [payload["record_sha256"] for _, payload in states],
        "first_session": sessions[0].date().isoformat(),
        "last_session": sessions[-1].date().isoformat(),
        "method": (
            "self-financing fractional-share paper accounts; prior-close decision executes at "
            "next adjusted open; survivors are not resized; 50bp round-trip cost is split evenly "
            "across entry and exit; eligibility belongs to the prior decision gate"
        ),
        "transitions": transitions,
        "diagnostic_all_sessions": {
            "primary_consensus": _metrics(all_primary),
            "market_guard_comparator": _metrics(all_guarded),
            "experimental_arms": {
                arm_name: _metrics(values) for arm_name, values in all_diagnostics.items()
            },
            "spy_buy_hold": _metrics(all_spy),
        },
        "diagnostic_arm_comparisons": diagnostic_comparisons,
        "session_return_attribution": session_return_attribution,
        "eligible_original_gate_only": {
            "sessions": len(eligible_primary_series),
            "completed_exits": eligible_exits,
            "risk_off_sessions": eligible_risk_off,
            "primary_consensus": _metrics(eligible_primary_series),
            "spy_buy_hold": _metrics(eligible_spy_series),
            "paired_stationary_bootstrap": bootstrap,
        },
        "rolling_policy_horizons": horizon_outcomes,
        "readiness": {
            "status": (
                "minimum_monitoring_evidence_met_not_trading_authority"
                if not readiness_reasons
                else "insufficient_prospective_evidence"
            ),
            "minimum_eligible_sessions": MINIMUM_ELIGIBLE_SESSIONS,
            "minimum_completed_exits": MINIMUM_COMPLETED_EXITS,
            "minimum_risk_off_sessions": MINIMUM_RISK_OFF_SESSIONS,
            "reasons": readiness_reasons,
        },
    }


def _diagnostic_arm_comparisons(
    diagnostic_returns: dict[str, pd.Series],
    primary_returns: pd.Series,
    spy_returns: pd.Series,
) -> dict[str, object]:
    arm_names = tuple(diagnostic_returns)
    per_arm: dict[str, object] = {}
    reference_arms = {
        "short_volume": "short_volume_hold21",
        "share_turnover": "share_turnover_hold21",
    }
    for position, arm_name in enumerate(arm_names):
        values = diagnostic_returns[arm_name]
        family = _diagnostic_arm_family(arm_name)
        same_signal_reference = reference_arms[family]
        reference = diagnostic_returns.get(same_signal_reference)
        row: dict[str, object] = {
            "versus_primary_consensus": _paired_diagnostic(
                values,
                primary_returns,
                seed=20261007 + position,
            ),
            "versus_spy": _paired_diagnostic(
                values,
                spy_returns,
                seed=20261107 + position,
            ),
        }
        if arm_name == same_signal_reference:
            row["versus_same_signal_hold21"] = {
                "status": "reference_arm",
                "reference": same_signal_reference,
            }
        elif reference is None:
            row["versus_same_signal_hold21"] = {
                "status": "reference_arm_missing",
                "reference": same_signal_reference,
            }
        else:
            comparison = _paired_diagnostic(
                values,
                reference,
                seed=20261207 + position,
            )
            comparison["reference"] = same_signal_reference
            row["versus_same_signal_hold21"] = comparison
        per_arm[arm_name] = row

    complete = pd.concat(
        [
            pd.DataFrame(diagnostic_returns),
            primary_returns.rename("__primary__"),
        ],
        axis=1,
    ).dropna()
    family: dict[str, object]
    if not arm_names:
        family = {
            "status": "not_applicable",
            "reason": "no_diagnostic_arms",
        }
    elif len(complete) < MINIMUM_DIAGNOSTIC_FAMILY_SESSIONS:
        family = {
            "status": "insufficient_sessions",
            "sessions": len(complete),
            "minimum_sessions": MINIMUM_DIAGNOSTIC_FAMILY_SESSIONS,
            "benchmark": "primary_consensus",
        }
    else:
        validation = stationary_bootstrap_family_validation(
            complete[list(arm_names)],
            complete["__primary__"],
            mean_block_sessions=21,
            samples=2_000,
            seed=20261307,
            fdr_level=0.05,
        )
        summary = dict(validation.summary)
        summary["interpretation"] = (
            "Prospective Yahoo-only diagnostic with family-wide error control; it remains "
            "ineligible for primary evidence and is not trading authority."
        )
        family = {
            "status": "estimated",
            "benchmark": "primary_consensus",
            "summary": summary,
            "variants": json.loads(
                validation.variants.reset_index().to_json(orient="records")
            ),
        }
    return {
        "status": "diagnostic_only_not_primary_evidence",
        "minimum_paired_sessions": MINIMUM_DIAGNOSTIC_PAIRED_SESSIONS,
        "minimum_family_sessions": MINIMUM_DIAGNOSTIC_FAMILY_SESSIONS,
        "same_signal_reference_arms": reference_arms,
        "method": (
            "paired stationary bootstrap for each preregistered arm; common stationary "
            "bootstrap and Benjamini-Yekutieli correction across the complete arm family"
        ),
        "per_arm": per_arm,
        "family_vs_primary_consensus": family,
    }


def _paired_diagnostic(
    candidate: pd.Series,
    benchmark: pd.Series,
    *,
    seed: int,
) -> dict[str, object]:
    paired = pd.concat(
        [candidate.rename("candidate"), benchmark.rename("benchmark")],
        axis=1,
    ).dropna()
    if len(paired) < MINIMUM_DIAGNOSTIC_PAIRED_SESSIONS:
        return {
            "status": "insufficient_sessions",
            "sessions": len(paired),
            "minimum_sessions": MINIMUM_DIAGNOSTIC_PAIRED_SESSIONS,
        }
    result: dict[str, object] = {
        "status": "estimated",
        **paired_stationary_bootstrap(
            paired["candidate"],
            paired["benchmark"],
            mean_block_sessions=21,
            samples=2_000,
            seed=seed,
        ),
    }
    result["interpretation"] = (
        "Prospective unvalidated diagnostic only; the interval does not establish a persistent "
        "edge or authorize trading."
    )
    return result


def _arm_return_attribution(
    prior_equity: float | None,
    current: dict[str, object],
    arm_name: str,
) -> dict[str, object]:
    if prior_equity is None:
        return {
            "status": "unavailable_missing_prior_equity",
            "overnight_gross_return": None,
            "execution_cost_fraction_of_open_equity": None,
            "intraday_return_after_open_cost": None,
            "net_return": None,
            "linked_net_return": None,
            "reconciliation_error": None,
        }
    account = _arm(current, arm_name).get("paper_account_at_close")
    if not isinstance(account, dict):
        raise StockProspectiveEvaluationError(f"Arm {arm_name} is missing its paper account.")
    open_before_costs = _positive_float(
        account.get("equity_at_open_before_costs"),
        f"{arm_name} equity at open before costs",
    )
    trading_cost = _nonnegative_float(
        account.get("trading_cost_this_open"),
        f"{arm_name} trading cost",
    )
    open_after_costs = open_before_costs - trading_cost
    if open_after_costs <= 0.0:
        raise StockProspectiveEvaluationError(
            f"Arm {arm_name} has nonpositive equity after open costs."
        )
    close_equity = _positive_float(account.get("total_equity"), f"{arm_name} close equity")
    overnight = open_before_costs / prior_equity - 1.0
    cost_fraction = trading_cost / open_before_costs
    intraday = close_equity / open_after_costs - 1.0
    net = close_equity / prior_equity - 1.0
    linked = (1.0 + overnight) * (1.0 - cost_fraction) * (1.0 + intraday) - 1.0
    error = linked - net
    if abs(error) > 1e-10:
        raise StockProspectiveEvaluationError(
            f"Arm {arm_name} return attribution does not reconcile."
        )
    return {
        "status": "attributed",
        "overnight_gross_return": overnight,
        "execution_cost_fraction_of_open_equity": cost_fraction,
        "intraday_return_after_open_cost": intraday,
        "net_return": net,
        "linked_net_return": linked,
        "reconciliation_error": error,
    }


def _spy_return_attribution(
    states: list[tuple[Path, dict[str, object]]],
    position: int,
    prior_equity: float,
    current_equity: float,
) -> dict[str, object]:
    current = states[position][1]
    market = current.get("market_state")
    if not isinstance(market, dict):
        raise StockProspectiveEvaluationError("State is missing benchmark marks.")
    open_price = _positive_float(market.get("open"), "SPY attribution open")
    close_price = _positive_float(market.get("close"), "SPY attribution close")
    if position == 1:
        primary_account = _arm(current, "consensus").get("paper_account_at_close")
        if not isinstance(primary_account, dict):
            raise StockProspectiveEvaluationError("State is missing its paper account.")
        one_way_cost = _nonnegative_float(
            primary_account.get("one_way_cost_fraction"),
            "SPY synthetic entry cost rate",
        )
        overnight = 0.0
        cost_fraction = one_way_cost / (1.0 + one_way_cost)
    else:
        prior_market = states[position - 1][1].get("market_state")
        if not isinstance(prior_market, dict):
            raise StockProspectiveEvaluationError("Prior state is missing benchmark marks.")
        prior_close = _positive_float(prior_market.get("close"), "prior SPY close")
        overnight = open_price / prior_close - 1.0
        cost_fraction = 0.0
    intraday = close_price / open_price - 1.0
    net = current_equity / prior_equity - 1.0
    linked = (1.0 + overnight) * (1.0 - cost_fraction) * (1.0 + intraday) - 1.0
    error = linked - net
    if abs(error) > 1e-10:
        raise StockProspectiveEvaluationError("SPY return attribution does not reconcile.")
    return {
        "status": "attributed",
        "overnight_gross_return": overnight,
        "execution_cost_fraction_of_open_equity": cost_fraction,
        "intraday_return_after_open_cost": intraday,
        "net_return": net,
        "linked_net_return": linked,
        "reconciliation_error": error,
    }


def _aggregate_return_attribution(
    transitions: list[dict[str, object]],
    arm_names: tuple[str, ...],
) -> dict[str, object]:
    arms = {
        arm_name: _aggregate_component_rows(
            [
                transition["return_attribution"]["arms"][arm_name]  # type: ignore[index]
                for transition in transitions
            ]
        )
        for arm_name in arm_names
    }
    spy_rows = [
        transition["return_attribution"]["spy_buy_hold"]  # type: ignore[index]
        for transition in transitions
    ]
    return {
        "method": (
            "multiplicatively linked prior-close-to-open gross return, explicit open trading-cost "
            "drag, and post-cost open-to-close return; components are not additive"
        ),
        "arms": arms,
        "spy_buy_hold": _aggregate_component_rows(spy_rows),
    }


def _aggregate_component_rows(rows: list[object]) -> dict[str, float | int | None]:
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in {
            "attributed",
            "unavailable_missing_prior_equity",
        }:
            raise StockProspectiveEvaluationError("Return attribution row is malformed.")
    attributed = [
        row for row in rows if isinstance(row, dict) and row.get("status") == "attributed"
    ]
    unavailable = len(rows) - len(attributed)
    if not attributed:
        return {
            "sessions": 0,
            "unavailable_sessions": unavailable,
            "cumulative_overnight_gross_return": None,
            "cumulative_execution_cost_drag": None,
            "cumulative_intraday_after_open_cost_return": None,
            "linked_cumulative_net_return": None,
            "maximum_absolute_reconciliation_error": None,
        }
    typed: list[dict[str, float]] = []
    numeric_fields = (
        "overnight_gross_return",
        "execution_cost_fraction_of_open_equity",
        "intraday_return_after_open_cost",
        "net_return",
        "linked_net_return",
        "reconciliation_error",
    )
    for row in attributed:
        typed.append({key: float(row[key]) for key in numeric_fields})
    overnight = np.asarray([row["overnight_gross_return"] for row in typed], dtype=float)
    cost = np.asarray(
        [row["execution_cost_fraction_of_open_equity"] for row in typed],
        dtype=float,
    )
    intraday = np.asarray(
        [row["intraday_return_after_open_cost"] for row in typed],
        dtype=float,
    )
    net = np.asarray([row["net_return"] for row in typed], dtype=float)
    linked = (1.0 + overnight) * (1.0 - cost) * (1.0 + intraday)
    linked_cumulative = float(np.prod(linked) - 1.0)
    net_cumulative = float(np.prod(1.0 + net) - 1.0)
    if abs(linked_cumulative - net_cumulative) > 1e-10:
        raise StockProspectiveEvaluationError(
            "Cumulative return attribution does not reconcile."
        )
    return {
        "sessions": len(typed),
        "unavailable_sessions": unavailable,
        "cumulative_overnight_gross_return": float(np.prod(1.0 + overnight) - 1.0),
        "cumulative_execution_cost_drag": float(np.prod(1.0 - cost) - 1.0),
        "cumulative_intraday_after_open_cost_return": float(
            np.prod(1.0 + intraday) - 1.0
        ),
        "linked_cumulative_net_return": linked_cumulative,
        "maximum_absolute_reconciliation_error": float(
            max(abs(row["reconciliation_error"]) for row in typed)
        ),
    }


def _horizon_outcomes(
    states: list[tuple[Path, dict[str, object]]],
    sessions: list[pd.Timestamp],
    primary_equity: list[float | None],
) -> dict[str, object]:
    specification = states[0][1].get("specification")
    if not isinstance(specification, dict):
        return {"status": "missing_frozen_horizons", "by_horizon": {}}
    config = specification.get("config")
    evaluation = config.get("evaluation") if isinstance(config, dict) else None
    raw_horizons = evaluation.get("horizons") if isinstance(evaluation, dict) else None
    if not isinstance(raw_horizons, list) or not raw_horizons:
        return {"status": "missing_frozen_horizons", "by_horizon": {}}
    horizons = tuple(int(value) for value in raw_horizons)
    if any(value < 1 for value in horizons):
        raise StockProspectiveEvaluationError("Frozen evaluation horizons must be positive.")
    by_horizon: dict[str, object] = {}
    for horizon in horizons:
        rows: list[dict[str, object]] = []
        for start in range(0, len(states) - horizon):
            end = start + horizon
            entry_market = states[start + 1][1].get("market_state")
            exit_market = states[end][1].get("market_state")
            if not isinstance(entry_market, dict) or not isinstance(exit_market, dict):
                raise StockProspectiveEvaluationError("Horizon state is missing SPY marks.")
            entry_open = _positive_float(entry_market.get("open"), "SPY horizon entry open")
            exit_close = _positive_float(exit_market.get("close"), "SPY horizon exit close")
            entry_account = _arm(states[start + 1][1], "consensus").get(
                "paper_account_at_close"
            )
            if not isinstance(entry_account, dict):
                raise StockProspectiveEvaluationError("Horizon state is missing its paper account.")
            entry_equity_before_costs = _positive_float(
                entry_account.get("equity_at_open_before_costs"),
                "policy horizon entry equity",
            )
            model_return = _return(entry_equity_before_costs, primary_equity[end])
            if model_return is None:
                continue
            cost = _nonnegative_float(
                entry_account.get("one_way_cost_fraction"),
                "horizon cost rate",
            )
            spy_return = exit_close / (entry_open * (1.0 + cost)) - 1.0
            gates = [
                states[position][1].get("eligible_for_primary_prospective_performance") is True
                for position in range(start, end)
            ]
            valuations = [
                _account_valuation_complete(states[position][1], "consensus")
                for position in range(start + 1, end + 1)
            ]
            eligible = all(gates) and all(valuations)
            rows.append(
                {
                    "decision_close": sessions[start].date().isoformat(),
                    "entry_open_session": sessions[start + 1].date().isoformat(),
                    "exit_close_session": sessions[end].date().isoformat(),
                    "eligible": eligible,
                    "primary_policy_net_return": model_return,
                    "spy_same_window_return": spy_return,
                    "net_excess_vs_spy": model_return - spy_return,
                }
            )
        eligible_rows = [row for row in rows if row["eligible"] is True]
        by_horizon[str(horizon)] = {
            "matured_windows": len(rows),
            "eligible_windows": len(eligible_rows),
            "mean_primary_policy_net_return": _mean_field(
                eligible_rows,
                "primary_policy_net_return",
            ),
            "mean_spy_same_window_return": _mean_field(
                eligible_rows,
                "spy_same_window_return",
            ),
            "mean_net_excess_vs_spy": _mean_field(eligible_rows, "net_excess_vs_spy"),
            "positive_excess_fraction": (
                sum(float(row["net_excess_vs_spy"]) > 0.0 for row in eligible_rows)
                / len(eligible_rows)
                if eligible_rows
                else None
            ),
            "windows": rows,
        }
    return {
        "status": "evaluated",
        "method": (
            "policy equity from the next open before that open's costs to the horizon close; "
            "every intervening prior-decision gate and current valuation must pass"
        ),
        "by_horizon": by_horizon,
    }


def write_stock_shadow_evaluation(
    state_dir: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> Path:
    evaluated_at = _as_utc(now or datetime.now(UTC))
    output = evaluate_stock_shadow_lineage(state_dir)
    output["evaluated_at_utc"] = evaluated_at.isoformat()
    canonical = json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
    record_hash = hashlib.sha256(canonical).hexdigest()
    output["record_sha256"] = record_hash
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = evaluated_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"evaluation-{stamp}-{record_hash[:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    return path


def verify_stock_shadow_evaluation(path: Path) -> bool:
    payload = _read_json(path)
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def _load_state_lineage(state_dir: Path) -> list[tuple[Path, dict[str, object]]]:
    paths = sorted(state_dir.glob("state-*.json")) if state_dir.exists() else []
    result: list[tuple[Path, dict[str, object]]] = []
    prior_name: str | None = None
    prior_hash: str | None = None
    lineage_id: object = None
    stock_policy_hash: str | None = None
    for position, path in enumerate(paths):
        if not verify_stock_shadow_state(path):
            raise StockProspectiveEvaluationError(f"State hash failed: {path.name}")
        payload = _read_json(path)
        if position == 0:
            if payload.get("initialization") is not True:
                raise StockProspectiveEvaluationError("State lineage does not start from cash.")
            lineage_id = payload.get("lineage_id")
            stock_policy_hash = _state_policy_hash(payload)
        else:
            if (
                payload.get("previous_record") != prior_name
                or payload.get("previous_record_sha256") != prior_hash
            ):
                raise StockProspectiveEvaluationError("State lineage prior-record chain is broken.")
            if payload.get("lineage_id") != lineage_id:
                raise StockProspectiveEvaluationError("State lineage identifier changed.")
            if _state_policy_hash(payload) != stock_policy_hash:
                raise StockProspectiveEvaluationError("State decision-policy hash changed.")
        result.append((path, payload))
        prior_name = path.name
        prior_hash = str(payload["record_sha256"])
    return result


def _state_policy_hash(payload: dict[str, object]) -> str:
    value = payload.get("stock_policy_sha256")
    if isinstance(value, str):
        return value
    legacy = payload.get("implementation_sha256")
    if isinstance(legacy, str):
        return legacy
    raise StockProspectiveEvaluationError("State has no decision-policy provenance.")


def _diagnostic_arm_names(
    states: list[tuple[Path, dict[str, object]]],
) -> tuple[str, ...]:
    base = {"consensus", "consensus_market_guard"}
    first_arms = states[0][1].get("arms")
    if not isinstance(first_arms, dict):
        raise StockProspectiveEvaluationError("State is missing its arm registry.")
    diagnostic = tuple(
        sorted(
            set(first_arms) - base,
            key=lambda name: (
                _diagnostic_arm_family(name),
                int(name.rsplit("hold", maxsplit=1)[1]),
            ),
        )
    )
    expected = base | set(diagnostic)
    for _, payload in states:
        arms = payload.get("arms")
        if not isinstance(arms, dict) or set(arms) != expected:
            raise StockProspectiveEvaluationError("State arm registry changed within the lineage.")
    return diagnostic


def _diagnostic_arm_family(arm_name: str) -> str:
    if arm_name.startswith("short_volume_hold"):
        return "short_volume"
    if arm_name.startswith("share_turnover_hold"):
        return "share_turnover"
    raise StockProspectiveEvaluationError(f"State contains an unknown diagnostic arm: {arm_name}")


def _spy_equity(states: list[tuple[Path, dict[str, object]]]) -> list[float]:
    result = [1.0]
    shares: float | None = None
    cash = 1.0
    for position in range(1, len(states)):
        payload = states[position][1]
        market = payload.get("market_state")
        if not isinstance(market, dict):
            raise StockProspectiveEvaluationError("State is missing benchmark marks.")
        open_price = _positive_float(market.get("open"), "SPY open")
        close_price = _positive_float(market.get("close"), "SPY close")
        if shares is None:
            primary = _arm(payload, "consensus")
            account = primary.get("paper_account_at_close")
            if not isinstance(account, dict):
                raise StockProspectiveEvaluationError("State is missing its paper account.")
            cost = _nonnegative_float(account.get("one_way_cost_fraction"), "cost rate")
            gross = cash / (1.0 + cost)
            shares = gross / open_price
            cash = 0.0
        result.append(cash + shares * close_price)
    return result


def _account_equity(payload: dict[str, object], arm_name: str) -> float | None:
    account = _arm(payload, arm_name).get("paper_account_at_close")
    if not isinstance(account, dict):
        raise StockProspectiveEvaluationError(f"Arm {arm_name} is missing its paper account.")
    value = account.get("total_equity")
    if value is None:
        return None
    return _positive_float(value, f"{arm_name} equity")


def _account_valuation_complete(payload: dict[str, object], arm_name: str) -> bool:
    account = _arm(payload, arm_name).get("paper_account_at_close")
    return isinstance(account, dict) and account.get("valuation_complete") is True


def _mean_field(rows: list[dict[str, object]], field: str) -> float | None:
    if not rows:
        return None
    return sum(float(row[field]) for row in rows) / len(rows)


def _arm(payload: dict[str, object], arm_name: str) -> dict[str, object]:
    arms = payload.get("arms")
    if not isinstance(arms, dict) or not isinstance(arms.get(arm_name), dict):
        raise StockProspectiveEvaluationError(f"State is missing arm {arm_name}.")
    return arms[arm_name]  # type: ignore[return-value]


def _risk_on(payload: dict[str, object]) -> bool:
    market = payload.get("market_state")
    if not isinstance(market, dict) or not isinstance(market.get("risk_on"), bool):
        raise StockProspectiveEvaluationError("State has an invalid market regime.")
    return bool(market["risk_on"])


def _return(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    if start <= 0.0 or end <= 0.0:
        raise StockProspectiveEvaluationError("Paper equity must remain positive.")
    return end / start - 1.0


def _metrics(returns: pd.Series) -> dict[str, float | int | None]:
    clean = returns.dropna().astype(float)
    if clean.empty:
        return {
            "sessions": 0,
            "cumulative_return": None,
            "annualized_return": None,
            "annualized_volatility": None,
            "sharpe_zero_cash_benchmark": None,
            "maximum_drawdown": None,
            "positive_session_fraction": None,
        }
    wealth = (1.0 + clean).cumprod()
    cumulative = float(wealth.iloc[-1] - 1.0)
    annualized = float((1.0 + cumulative) ** (252.0 / len(clean)) - 1.0)
    volatility = float(clean.std(ddof=1) * np.sqrt(252)) if len(clean) > 1 else None
    sharpe = (
        float(clean.mean() / clean.std(ddof=1) * np.sqrt(252))
        if len(clean) > 1 and float(clean.std(ddof=1)) > 0.0
        else None
    )
    wealth_with_start = np.concatenate(([1.0], wealth.to_numpy(dtype=float)))
    drawdown = wealth_with_start / np.maximum.accumulate(wealth_with_start) - 1.0
    return {
        "sessions": len(clean),
        "cumulative_return": cumulative,
        "annualized_return": annualized,
        "annualized_volatility": volatility,
        "sharpe_zero_cash_benchmark": sharpe,
        "maximum_drawdown": float(drawdown.min()),
        "positive_session_fraction": float(clean.gt(0.0).mean()),
    }


def _positive_float(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StockProspectiveEvaluationError(f"Invalid {label}.") from exc
    if not np.isfinite(result) or result <= 0.0:
        raise StockProspectiveEvaluationError(f"Invalid {label}.")
    return result


def _nonnegative_float(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StockProspectiveEvaluationError(f"Invalid {label}.") from exc
    if not np.isfinite(result) or result < 0.0:
        raise StockProspectiveEvaluationError(f"Invalid {label}.")
    return result


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise StockProspectiveEvaluationError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
