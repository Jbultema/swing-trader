from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

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
        diagnostic_equity=1.0,
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
        diagnostic_equity=1.02,
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
        diagnostic_equity=1.04,
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
    assert result["diagnostic_all_sessions"]["experimental_arms"][
        "short_volume_hold5"
    ]["sessions"] == 2
    assert result["schema_version"] == 6
    assert result["transitions"][0]["diagnostic_arm_net_returns"][
        "short_volume_hold5"
    ] == pytest.approx(0.02)
    assert result["transitions"][0]["return_attribution"]["arms"]["consensus"][
        "net_return"
    ] == pytest.approx(0.10)
    primary_attribution = result["session_return_attribution"]["arms"]["consensus"]
    assert primary_attribution["sessions"] == 2
    assert primary_attribution["linked_cumulative_net_return"] == pytest.approx(0.0)
    comparisons = result["diagnostic_arm_comparisons"]
    assert comparisons["per_arm"]["short_volume_hold5"]["versus_primary_consensus"][
        "status"
    ] == "insufficient_sessions"
    assert comparisons["family_vs_primary_consensus"]["status"] == "insufficient_sessions"
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


def test_diagnostic_arm_inference_waits_for_preregistered_sample_sizes(
    tmp_path: Path,
) -> None:
    states = tmp_path / "states"
    states.mkdir()
    previous_name: str | None = None
    previous_hash: str | None = None
    for position in range(64):
        path = states / f"state-{position:03d}.json"
        written = _state(
            path,
            session=(date(2026, 1, 1) + timedelta(days=position)).isoformat(),
            initialization=position == 0,
            previous=previous_name,
            previous_hash=previous_hash,
            eligible=True,
            primary_equity=1.001**position,
            guarded_equity=1.001**position,
            diagnostic_equity=1.0015**position,
            spy_open=100.0,
            spy_close=100.0,
        )
        payload = json.loads(written.read_text())
        previous_name = written.name
        previous_hash = payload["record_sha256"]

    result = evaluate_stock_shadow_lineage(states)

    comparisons = result["diagnostic_arm_comparisons"]
    assert comparisons["per_arm"]["short_volume_hold5"]["versus_primary_consensus"][
        "status"
    ] == "estimated"
    family = comparisons["family_vs_primary_consensus"]
    assert family["status"] == "estimated"
    assert family["summary"]["sessions"] == 63
    assert family["variants"][0]["variant"] == "short_volume_hold5"


def test_diagnostic_comparisons_use_a_hold21_reference_per_signal_family(
    tmp_path: Path,
) -> None:
    states = tmp_path / "states"
    states.mkdir()
    first = _state(
        states / "state-a.json",
        session="2026-10-05",
        initialization=True,
        previous=None,
        previous_hash=None,
        eligible=True,
        primary_equity=1.0,
        guarded_equity=1.0,
        spy_open=100.0,
        spy_close=100.0,
        diagnostic_equities={
            "consensus_breadth_guard": 1.0,
            "classic_12_1_hold21": 1.0,
            "short_volume_hold5": 1.0,
            "short_volume_hold21": 1.0,
            "share_turnover_hold5": 1.0,
            "share_turnover_hold21": 1.0,
        },
    )
    first_payload = json.loads(first.read_text())
    _state(
        states / "state-b.json",
        session="2026-10-06",
        initialization=False,
        previous=first.name,
        previous_hash=first_payload["record_sha256"],
        eligible=True,
        primary_equity=1.01,
        guarded_equity=1.01,
        spy_open=100.0,
        spy_close=101.0,
        diagnostic_equities={
            "consensus_breadth_guard": 1.005,
            "classic_12_1_hold21": 1.015,
            "short_volume_hold5": 1.01,
            "short_volume_hold21": 1.02,
            "share_turnover_hold5": 1.03,
            "share_turnover_hold21": 1.04,
        },
    )

    comparisons = evaluate_stock_shadow_lineage(states)["diagnostic_arm_comparisons"]

    assert comparisons["same_signal_reference_arms"] == {
        "breadth_guard": "consensus_breadth_guard",
        "classic_12_1": "classic_12_1_hold21",
        "short_volume": "short_volume_hold21",
        "share_turnover": "share_turnover_hold21",
    }
    assert comparisons["per_arm"]["share_turnover_hold21"][
        "versus_same_signal_hold21"
    ]["status"] == "reference_arm"
    assert comparisons["per_arm"]["share_turnover_hold5"][
        "versus_same_signal_hold21"
    ]["reference"] == "share_turnover_hold21"
    assert comparisons["per_arm"]["classic_12_1_hold21"][
        "versus_same_signal_hold21"
    ]["status"] == "reference_arm"
    assert comparisons["per_arm"]["consensus_breadth_guard"][
        "versus_same_signal_hold21"
    ]["status"] == "reference_arm"


def test_diagnostic_inference_excludes_prior_signal_data_gate_failures(
    tmp_path: Path,
) -> None:
    states = tmp_path / "states"
    states.mkdir()
    equities = {
        "short_volume_hold21": 1.0,
        "share_turnover_hold21": 1.0,
    }
    first = _state(
        states / "state-a.json",
        session="2026-10-05",
        initialization=True,
        previous=None,
        previous_hash=None,
        eligible=True,
        primary_equity=1.0,
        guarded_equity=1.0,
        spy_open=100.0,
        spy_close=100.0,
        diagnostic_equities=equities,
        diagnostic_eligibility={
            "short_volume_hold21": True,
            "share_turnover_hold21": False,
        },
    )
    first_payload = json.loads(first.read_text())
    _state(
        states / "state-b.json",
        session="2026-10-06",
        initialization=False,
        previous=first.name,
        previous_hash=first_payload["record_sha256"],
        eligible=True,
        primary_equity=1.01,
        guarded_equity=1.01,
        spy_open=100.0,
        spy_close=101.0,
        diagnostic_equities={
            "short_volume_hold21": 1.02,
            "share_turnover_hold21": 1.03,
        },
    )

    result = evaluate_stock_shadow_lineage(states)

    assert result["transitions"][0]["diagnostic_arm_data_gate_eligible"] == {
        "share_turnover_hold21": False,
        "short_volume_hold21": True,
    }
    eligible = result["diagnostic_data_gate_eligible"]["experimental_arms"]
    assert eligible["short_volume_hold21"]["sessions"] == 1
    assert eligible["share_turnover_hold21"]["sessions"] == 0
    comparison = result["diagnostic_arm_comparisons"]["per_arm"][
        "share_turnover_hold21"
    ]["versus_primary_consensus"]
    assert comparison["sessions"] == 0


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
    diagnostic_equity: float | None = None,
    diagnostic_equities: dict[str, float] | None = None,
    diagnostic_eligibility: dict[str, bool] | None = None,
    executions: list[dict[str, str]] | None = None,
) -> Path:
    execution_rows = executions or []
    arms = {
        "consensus": _arm(primary_equity, execution_rows),
        "consensus_market_guard": _arm(guarded_equity, []),
    }
    if diagnostic_equity is not None:
        arms["short_volume_hold5"] = _arm(
            diagnostic_equity,
            [],
            eligible=(diagnostic_eligibility or {}).get("short_volume_hold5", True),
        )
    for arm_name, equity in (diagnostic_equities or {}).items():
        arms[arm_name] = _arm(
            equity,
            [],
            eligible=(diagnostic_eligibility or {}).get(arm_name, True),
        )
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
        "arms": arms,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _arm(
    equity: float,
    executions: list[dict[str, str]],
    *,
    eligible: bool = True,
) -> dict[str, object]:
    return {
        "eligible_for_diagnostic_prospective_performance": eligible,
        "executions_at_open": executions,
        "paper_account_at_close": {
            "total_equity": equity,
            "valuation_complete": True,
            "one_way_cost_fraction": 0.0025,
            "equity_at_open_before_costs": 1.0,
            "trading_cost_this_open": 0.0,
        },
    }
