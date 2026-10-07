from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from swing_trader.stock_prospective import (
    StockProspectiveEvaluationError,
    evaluate_stock_shadow_lineage,
    verify_stock_shadow_evaluation,
    write_stock_shadow_evaluation,
)


def test_prospective_evaluator_uses_prior_decision_gate_and_self_financing_equity(
    tmp_path: Path,
) -> None:
    states = tmp_path / "states"
    states.mkdir()
    first = _state(
        states / "state-20261005-a.json",
        session="2026-10-05",
        initialization=True,
        previous=None,
        previous_hash=None,
        eligible=True,
        primary_equity=1.0,
        guarded_equity=1.0,
        spy_open=100.0,
        spy_close=100.0,
    )
    first_payload = json.loads(first.read_text())
    second = _state(
        states / "state-20261006-b.json",
        session="2026-10-06",
        initialization=False,
        previous=first.name,
        previous_hash=first_payload["record_sha256"],
        eligible=False,
        primary_equity=1.10,
        guarded_equity=1.05,
        spy_open=100.0,
        spy_close=105.0,
        executions=[{"action": "BUY"}],
    )
    second_payload = json.loads(second.read_text())
    _state(
        states / "state-20261007-c.json",
        session="2026-10-07",
        initialization=False,
        previous=second.name,
        previous_hash=second_payload["record_sha256"],
        eligible=True,
        primary_equity=1.0,
        guarded_equity=1.06,
        spy_open=106.0,
        spy_close=110.0,
        executions=[{"action": "SELL"}],
    )

    result = evaluate_stock_shadow_lineage(states)

    assert result["states_seen"] == 3
    assert result["stock_policy_sha256"] == "policy-test"
    assert result["state_package_implementation_sha256"] == ["package-test"]
    assert len(result["evaluation_implementation_sha256"]) == 64
    assert result["diagnostic_all_sessions"]["primary_consensus"]["sessions"] == 2
    assert result["eligible_original_gate_only"]["sessions"] == 1
    assert result["eligible_original_gate_only"]["completed_exits"] == 0
    assert result["transitions"][0]["eligible"] is True
    assert result["transitions"][1]["eligible"] is False
    assert result["readiness"]["status"] == "insufficient_prospective_evidence"
    assert result["eligible_original_gate_only"]["paired_stationary_bootstrap"] is None
    horizon_one = result["rolling_policy_horizons"]["by_horizon"]["1"]
    horizon_two = result["rolling_policy_horizons"]["by_horizon"]["2"]
    assert (horizon_one["matured_windows"], horizon_one["eligible_windows"]) == (2, 1)
    assert (horizon_two["matured_windows"], horizon_two["eligible_windows"]) == (1, 0)

    output = write_stock_shadow_evaluation(
        states,
        tmp_path / "evaluations",
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert verify_stock_shadow_evaluation(output)


def test_prospective_evaluator_rejects_broken_state_chain(tmp_path: Path) -> None:
    states = tmp_path / "states"
    states.mkdir()
    _state(
        states / "state-20261005-a.json",
        session="2026-10-05",
        initialization=False,
        previous=None,
        previous_hash=None,
        eligible=True,
        primary_equity=1.0,
        guarded_equity=1.0,
        spy_open=100.0,
        spy_close=100.0,
    )

    try:
        evaluate_stock_shadow_lineage(states)
    except StockProspectiveEvaluationError as exc:
        assert "start from cash" in str(exc)
    else:
        raise AssertionError("Broken lineage was accepted.")


def _state(
    path: Path,
    *,
    session: str,
    initialization: bool,
    previous: str | None,
    previous_hash: str | None,
    eligible: bool,
    primary_equity: float,
    guarded_equity: float,
    spy_open: float,
    spy_close: float,
    executions: list[dict[str, str]] | None = None,
) -> Path:
    execution_rows = executions or []
    payload: dict[str, object] = {
        "schema_version": 1,
        "record_type": "prospective_stock_shadow_state",
        "lineage_id": "policy-test-config-test",
        "stock_policy_sha256": "policy-test",
        "implementation_sha256": "package-test",
        "initialization": initialization,
        "as_of_session": session,
        "previous_record": previous,
        "previous_record_sha256": previous_hash,
        "eligible_for_primary_prospective_performance": eligible,
        "specification": {"config": {"evaluation": {"horizons": [1, 2]}}},
        "market_state": {
            "open": spy_open,
            "close": spy_close,
            "risk_on": True,
        },
        "arms": {
            "consensus": _arm(primary_equity, execution_rows),
            "consensus_market_guard": _arm(guarded_equity, []),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _arm(equity: float, executions: list[dict[str, str]]) -> dict[str, object]:
    return {
        "executions_at_open": executions,
        "paper_account_at_close": {
            "total_equity": equity,
            "valuation_complete": True,
            "one_way_cost_fraction": 0.0025,
            "equity_at_open_before_costs": 1.0,
        },
    }
