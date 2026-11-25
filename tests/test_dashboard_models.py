from __future__ import annotations

import json
from pathlib import Path

import pytest

from swing_trader.dashboard_models import (
    StockDashboardState,
    percentage_points_frame,
    policy_forward_frame,
    prospective_maturity,
    prospective_performance_rows,
    stock_action_frame,
    stock_decision_headline,
    stock_price_map,
    stock_target_frame,
)


def _payload(*, eligible: bool = True) -> dict[str, object]:
    return {
        "record_sha256": "state-1",
        "lineage_id": "policy-a-config-a",
        "stock_policy_sha256": "policy-a",
        "as_of_session": "2026-10-07",
        "eligible_for_primary_prospective_performance": eligible,
        "market_state": {"risk_on": True, "close": 100.0},
        "market_breadth": {"breadth_risk_on": False},
        "arms": {
            "consensus": {
                "target_for_next_open": {"AAA": 0.1},
                "target_cash_weight": 0.9,
                "paper_account_at_close": {
                    "total_equity": 1.0,
                    "adjusted_close_marks": {},
                },
                "decisions": [
                    {
                        "ticker": "AAA",
                        "gics_sector": "Technology",
                        "action": "BUY",
                        "reasons": ["top_consensus_entry"],
                        "selection_rank": 1,
                        "close": 50.0,
                        "return_21d": 0.12,
                        "return_63d": 0.25,
                        "return_12_1": 0.4,
                        "proximity_52w_high": 0.98,
                        "volume_ratio_20_126": 1.2,
                    }
                ],
            }
        },
    }


def test_decision_models_are_summary_first_and_human_readable(tmp_path) -> None:
    state = StockDashboardState(
        path=tmp_path / "state.json",
        payload=_payload(),
        current_policy=True,
        integrity_passed=True,
    )

    headline = stock_decision_headline(state)
    actions = stock_action_frame(state.payload)
    targets = stock_target_frame(state.payload)

    assert headline["label"] == "REVIEW"
    assert headline["BUY"] == 1
    assert actions.iloc[0]["Why"] == (
        "Strongest agreement across the three momentum screens"
    )
    assert actions.iloc[0]["Distance from 52-week high"] == pytest.approx(-0.02)
    assert set(targets["Ticker"]) == {"AAA", "CASH"}
    assert stock_price_map(state.payload) == {"AAA": 50.0}


def test_percentage_points_frame_scales_display_copy_without_mutating_source() -> None:
    source = stock_target_frame(_payload())

    display = percentage_points_frame(source, ("Target weight",))

    assert display.loc[display["Ticker"] == "AAA", "Target weight"].item() == pytest.approx(10.0)
    assert display.loc[display["Ticker"] == "CASH", "Target weight"].item() == pytest.approx(90.0)
    assert source.loc[source["Ticker"] == "AAA", "Target weight"].item() == pytest.approx(0.1)


def test_failed_gate_and_old_policy_suppress_action_claim() -> None:
    failed = StockDashboardState(
        path=Path("state.json"),
        payload=_payload(eligible=False),
        current_policy=True,
        integrity_passed=True,
    )
    old = StockDashboardState(
        path=Path("state.json"),
        payload=_payload(),
        current_policy=False,
        integrity_passed=True,
    )

    assert stock_decision_headline(failed)["label"] == "NO ACTION"
    assert stock_decision_headline(old)["label"] == "WAIT"


def test_gap_recovery_is_explained_as_fail_closed_not_a_current_signal() -> None:
    payload = _payload(eligible=False)
    payload["gap_recovery"] = {
        "status": "missed_session_recovered_fail_closed",
        "historical_signal_generation_authorized": False,
    }
    state = StockDashboardState(
        path=Path("state.json"),
        payload=payload,
        current_policy=True,
        integrity_passed=True,
    )

    headline = stock_decision_headline(state)

    assert headline["label"] == "NO ACTION"
    assert headline["headline"] == "Missed session was recovered fail-closed"
    assert "no backdated signal" in str(headline["detail"])


def test_empty_prospective_metrics_render_as_an_empty_state() -> None:
    evaluation = {
        "eligible_original_gate_only": {
            "sessions": 0,
            "completed_exits": 0,
            "risk_off_sessions": 0,
            "primary_consensus": {"cumulative_return": None},
            "spy_buy_hold": {"cumulative_return": None},
        },
        "readiness": {
            "status": "insufficient_prospective_evidence",
            "minimum_eligible_sessions": 126,
            "minimum_completed_exits": 30,
            "minimum_risk_off_sessions": 10,
        },
    }

    maturity = prospective_maturity(evaluation)
    assert maturity["session_progress"] == 0.0
    assert "collecting evidence" in str(maturity["message"])
    assert prospective_performance_rows(evaluation).empty


def test_policy_path_only_uses_requested_lineage(monkeypatch, tmp_path) -> None:
    first = _payload()
    second = _payload()
    second["record_sha256"] = "state-2"
    second["as_of_session"] = "2026-10-08"
    second["market_state"] = {"risk_on": True, "close": 102.0}
    second["arms"]["consensus"]["paper_account_at_close"]["total_equity"] = 1.01
    other = _payload()
    other["lineage_id"] = "policy-other"
    paths = []
    for index, payload in enumerate((first, second, other)):
        path = tmp_path / f"state-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths.append(path)
    monkeypatch.setattr(
        "swing_trader.dashboard_models.verify_stock_shadow_state",
        lambda _path: True,
    )

    frame = policy_forward_frame(paths, lineage_id="policy-a-config-a")

    assert len(frame) == 2
    assert frame["recovery"].eq(False).all()
    assert frame.iloc[-1]["Policy return"] == pytest.approx(0.01)
    assert frame.iloc[-1]["SPY return"] == pytest.approx(0.02)
    assert frame.iloc[-1]["Excess return"] == pytest.approx(-0.01)


def test_policy_forward_frame_marks_recovery_states(monkeypatch, tmp_path) -> None:
    initial = _payload(eligible=False)
    recovery = _payload(eligible=False)
    recovery["record_sha256"] = "state-2"
    recovery["as_of_session"] = "2026-10-08"
    recovery["gap_recovery"] = {
        "status": "missed_session_recovered_fail_closed",
        "historical_signal_generation_authorized": False,
    }
    paths = []
    for index, payload in enumerate((initial, recovery)):
        path = tmp_path / f"state-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths.append(path)
    monkeypatch.setattr(
        "swing_trader.dashboard_models.verify_stock_shadow_state",
        lambda _path: True,
    )

    frame = policy_forward_frame(paths, lineage_id="policy-a-config-a")

    assert frame["recovery"].tolist() == [False, True]
