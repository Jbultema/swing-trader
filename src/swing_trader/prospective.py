from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from swing_trader.shadow import verify_shadow_snapshot

HORIZONS = (5, 21, 63)


def evaluate_prospective_records(
    record_dir: Path,
    prices: pd.DataFrame,
) -> dict[str, object]:
    """Evaluate unique locked decisions using only later next-open observations."""
    records, excluded = _load_unique_records(record_dir)
    opens = prices["Open"]
    outcomes: list[dict[str, object]] = []
    for record in records:
        decision = record["decision"]
        specification = record["specification"]
        config = specification["config"]
        benchmark = str(config["data"]["benchmark"])
        cost_rate = float(config["execution"]["transaction_cost_bps"]) / 10_000.0
        as_of = pd.Timestamp(str(decision["as_of_close"]))
        later_dates = opens.index[opens.index > as_of]
        target = _weights(decision, "target_weight")
        current = _weights(decision, "current_weight")
        turnover = sum(
            abs(target.get(ticker, 0.0) - current.get(ticker, 0.0))
            for ticker in set(target) | set(current)
        )
        eligible = bool(record["data_quality"].get("decision_data_gate_passed", False))
        row: dict[str, object] = {
            "record_sha256": record["record_sha256"],
            "recorded_at_utc": record["recorded_from_manifest_utc"],
            "as_of_close": str(as_of.date()),
            "last_monthly_decision": decision["last_monthly_decision"],
            "specification_sha256": record["specification_sha256"],
            "implementation_sha256": record["implementation_sha256"],
            "data_gate_passed": eligible,
            "target_weights": target,
            "modeled_one_way_turnover": turnover,
            "horizons": {},
        }
        if len(later_dates):
            entry_date = later_dates[0]
            row["entry_date"] = str(entry_date.date())
            for horizon in HORIZONS:
                result = _horizon_result(
                    opens,
                    entry_date,
                    horizon,
                    target,
                    benchmark,
                    cost_rate * turnover,
                )
                row["horizons"][str(horizon)] = result  # type: ignore[index]
        else:
            row["entry_date"] = None
        outcomes.append(row)

    return {
        "schema_version": 1,
        "evaluation_method": (
            "first adjusted open after the locked as-of close through the adjusted open "
            "5, 21, or 63 sessions later; model return deducts frozen one-way turnover cost"
        ),
        "evaluated_through": str(opens.index.max().date()),
        "horizons_sessions": list(HORIZONS),
        "records_seen": len(list(record_dir.glob("*.json"))) if record_dir.exists() else 0,
        "unique_schema_v3_decisions": len(records),
        "excluded_records": excluded,
        "outcomes": outcomes,
        "eligible_summary": _eligible_summary(outcomes),
    }


def write_prospective_evaluation(
    record_dir: Path,
    prices: pd.DataFrame,
    output_path: Path,
) -> dict[str, object]:
    result = evaluate_prospective_records(record_dir, prices)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _load_unique_records(
    record_dir: Path,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    unique: dict[str, dict[str, object]] = {}
    excluded: list[dict[str, str]] = []
    for path in sorted(record_dir.glob("*.json")) if record_dir.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            excluded.append({"file": path.name, "reason": f"unreadable: {exc}"})
            continue
        if not isinstance(payload, dict) or not verify_shadow_snapshot(path):
            excluded.append({"file": path.name, "reason": "invalid_record_hash"})
            continue
        if payload.get("schema_version") != 3 or not isinstance(payload.get("specification"), dict):
            excluded.append({"file": path.name, "reason": "not_self_contained_schema_v3"})
            continue
        key = _decision_key(payload)
        unique.setdefault(key, payload)
    return list(unique.values()), excluded


def _decision_key(record: dict[str, object]) -> str:
    decision = record["decision"]
    assert isinstance(decision, dict)
    target = _weights(decision, "target_weight")
    payload = {
        "specification_sha256": record["specification_sha256"],
        "last_monthly_decision": decision["last_monthly_decision"],
        "target_weights": target,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _weights(decision: dict[str, object], field: str) -> dict[str, float]:
    actions = decision.get("hypothetical_actions", [])
    if not isinstance(actions, list):
        return {}
    return {
        str(row["ticker"]): float(row[field])
        for row in actions
        if isinstance(row, dict) and field in row and "ticker" in row
    }


def _horizon_result(
    opens: pd.DataFrame,
    entry_date: pd.Timestamp,
    horizon: int,
    target: dict[str, float],
    benchmark: str,
    modeled_cost: float,
) -> dict[str, object]:
    entry_location = opens.index.get_loc(entry_date)
    exit_location = entry_location + horizon
    if exit_location >= len(opens.index):
        return {"matured": False}
    exit_date = opens.index[exit_location]
    asset_returns = opens.loc[exit_date].div(opens.loc[entry_date]).sub(1.0)
    gross = sum(weight * float(asset_returns[ticker]) for ticker, weight in target.items())
    benchmark_return = float(asset_returns[benchmark])
    return {
        "matured": True,
        "exit_date": str(exit_date.date()),
        "model_gross_return": gross,
        "model_net_return": gross - modeled_cost,
        "benchmark_return": benchmark_return,
        "net_excess_return": gross - modeled_cost - benchmark_return,
    }


def _eligible_summary(outcomes: list[dict[str, object]]) -> dict[str, object]:
    values: dict[int, list[tuple[float, float]]] = defaultdict(list)
    eligible_decisions = 0
    for outcome in outcomes:
        if not outcome["data_gate_passed"]:
            continue
        eligible_decisions += 1
        horizons = outcome["horizons"]
        assert isinstance(horizons, dict)
        for horizon in HORIZONS:
            result = horizons.get(str(horizon), {})
            if isinstance(result, dict) and result.get("matured"):
                values[horizon].append(
                    (float(result["model_net_return"]), float(result["net_excess_return"]))
                )
    summaries = {}
    for horizon in HORIZONS:
        pairs = values[horizon]
        summaries[str(horizon)] = {
            "matured_decisions": len(pairs),
            "mean_model_net_return": (
                sum(pair[0] for pair in pairs) / len(pairs) if pairs else None
            ),
            "mean_net_excess_return": (
                sum(pair[1] for pair in pairs) / len(pairs) if pairs else None
            ),
        }
    return {"eligible_unique_decisions": eligible_decisions, "by_horizon": summaries}
