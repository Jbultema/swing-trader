from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from swing_trader.provenance import file_sha256, stock_policy_sha256
from swing_trader.stock_gap_recovery import (
    GAP_RECOVERY_REASON,
    record_fail_closed_gap_states,
    tracked_tickers_from_state,
)
from swing_trader.stock_live_data import write_current_stock_price_snapshot
from swing_trader.stock_prospective import evaluate_stock_shadow_lineage
from swing_trader.stock_shadow_state import (
    load_stock_shadow_config,
    stock_shadow_lineage_id,
    verify_stock_shadow_state,
)


def test_gap_recovery_accounts_for_prior_target_then_exits_without_backdated_signal(
    tmp_path: Path,
) -> None:
    config_path = Path("config/stock_shadow.toml").resolve()
    config, raw_config = load_stock_shadow_config(config_path)
    captured_at = datetime(2026, 10, 8, 22, tzinfo=UTC)
    sessions = pd.bdate_range(end="2026-10-08", periods=260)
    prices = _prices(sessions)
    universe_manifest = tmp_path / "universe.manifest.json"
    universe_manifest.write_text('{"tabular_sha256":"universe"}\n', encoding="utf-8")
    snapshot = write_current_stock_price_snapshot(
        prices,
        ("A", "SPY"),
        tmp_path / "prices",
        universe_manifest_path=universe_manifest,
        universe_manifest={"tabular_sha256": "universe", "universe_role": "test"},
        benchmark="SPY",
        captured_at=captured_at,
        requested_start=date(2025, 1, 1),
        requested_end_exclusive=date(2026, 10, 9),
    )
    state_dir = tmp_path / "states"
    state_dir.mkdir()
    previous_path = _initial_state(
        state_dir,
        config_path,
        raw_config,
        sessions[-4],
        config.arms,
    )

    assert tracked_tickers_from_state(previous_path) == ("A",)
    recovered = record_fail_closed_gap_states(
        previous_path,
        snapshot.manifest_path,
        config_path,
        state_dir,
        stop_before_session=sessions[-1].date().isoformat(),
        now=captured_at,
    )

    assert [json.loads(path.read_text())["as_of_session"] for path in recovered] == [
        sessions[-3].date().isoformat(),
        sessions[-2].date().isoformat(),
    ]
    first = json.loads(recovered[0].read_text())
    second = json.loads(recovered[1].read_text())
    assert all(verify_stock_shadow_state(path) for path in recovered)
    assert first["gap_recovery"]["historical_signal_generation_authorized"] is False
    assert first["eligible_for_primary_prospective_performance"] is False
    first_primary = first["arms"]["consensus"]
    second_primary = second["arms"]["consensus"]
    assert [row["action"] for row in first_primary["executions_at_open"]] == ["BUY"]
    assert first_primary["target_for_next_open"] == {}
    assert [row["action"] for row in first_primary["decisions"]] == ["SELL"]
    assert first_primary["decisions"][0]["reasons"] == [GAP_RECOVERY_REASON]
    assert [row["action"] for row in second_primary["executions_at_open"]] == ["SELL"]
    assert second_primary["positions_at_close"] == {}
    assert second_primary["paper_account_at_close"]["valuation_complete"] is True
    assert (
        record_fail_closed_gap_states(
            recovered[-1],
            snapshot.manifest_path,
            config_path,
            state_dir,
            stop_before_session=sessions[-1].date().isoformat(),
            now=captured_at,
        )
        == ()
    )
    evaluation = evaluate_stock_shadow_lineage(state_dir)
    assert evaluation["states_seen"] == 3
    assert evaluation["eligible_original_gate_only"]["sessions"] == 0
    assert evaluation["last_session"] == sessions[-2].date().isoformat()


def test_tracked_tickers_can_scope_action_validation_to_primary_arm(tmp_path: Path) -> None:
    config_path = Path("config/stock_shadow.toml").resolve()
    _, raw_config = load_stock_shadow_config(config_path)
    state_dir = tmp_path / "states"
    state_dir.mkdir()
    path = _initial_state(
        state_dir,
        config_path,
        raw_config,
        pd.Timestamp("2026-10-06"),
        ("consensus", "classic_12_1_hold21"),
    )
    payload = json.loads(path.read_text())
    payload["arms"]["classic_12_1_hold21"]["target_for_next_open"] = {"B": 0.1}
    payload.pop("record_sha256")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert tracked_tickers_from_state(path) == ("A", "B")
    assert tracked_tickers_from_state(path, arm_names=("consensus",)) == ("A",)


def _prices(sessions: pd.DatetimeIndex) -> pd.DataFrame:
    columns = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], ["A", "SPY"]],
        names=["field", "ticker"],
    )
    frame = pd.DataFrame(index=sessions, columns=columns, dtype=float)
    for offset, ticker in enumerate(("A", "SPY")):
        close = pd.Series(
            [100.0 + offset * 50.0 + position * 0.1 for position in range(len(sessions))],
            index=sessions,
        )
        frame[("Open", ticker)] = close * 0.999
        frame[("High", ticker)] = close * 1.01
        frame[("Low", ticker)] = close * 0.99
        frame[("Close", ticker)] = close
        frame[("Volume", ticker)] = 1_000_000.0
    return frame


def _initial_state(
    state_dir: Path,
    config_path: Path,
    raw_config: dict[str, object],
    session: pd.Timestamp,
    arm_names: tuple[str, ...],
) -> Path:
    arm = {
        "market_guard_enabled": False,
        "executions_at_open": [],
        "positions_at_close": {},
        "target_for_next_open": {"A": 0.1},
        "target_cash_weight": 0.9,
        "decisions": [],
        "paper_account_at_close": {
            "base_currency": "USD",
            "accounting": "fractional_shares_no_survivor_rebalancing",
            "adjustment_policy": "rebase_shares_to_prior_locked_close_mark",
            "starting_equity": 1.0,
            "mark_session": session.date().isoformat(),
            "cash": 1.0,
            "shares": {},
            "share_rebase_factors_at_open": {},
            "adjusted_close_marks": {},
            "market_values": {},
            "gross_exposure": 0.0,
            "total_equity": 1.0,
            "cash_weight": 1.0,
            "valuation_complete": True,
            "equity_at_open_before_costs": 1.0,
            "gross_turnover_at_open": 0.0,
            "one_way_cost_fraction": 0.0025,
            "trading_cost_this_open": 0.0,
            "cumulative_trading_cost": 0.0,
        },
        "prospective_role": "test",
        "signal_family": "consensus",
        "maximum_holding_sessions": 21,
        "entry_rank_threshold": 10,
        "rank_exit_enabled": True,
        "hold_rank_threshold": 20.0,
        "rank_exit_multiple": 2.0,
        "independent_price_validation_applies": False,
        "signal_data_available_at_close": True,
        "signal_data_unavailable_reason": None,
        "eligible_for_diagnostic_prospective_performance": False,
        "eligible_for_primary_prospective_performance": False,
    }
    specification = {
        "config": raw_config,
        "signal": {},
        "data_cost_policy": "no_paid_sources",
    }
    specification_hash = hashlib.sha256(
        json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload: dict[str, object] = {
        "schema_version": 7,
        "record_type": "prospective_stock_shadow_state",
        "research_status": "paper_only_human_execution_required",
        "data_cost_policy": "no_paid_sources",
        "action_authorized": False,
        "initialization": True,
        "as_of_session": session.date().isoformat(),
        "recorded_at_utc": datetime(2026, 10, 5, 22, tzinfo=UTC).isoformat(),
        "previous_record": None,
        "previous_record_sha256": None,
        "lineage_id": stock_shadow_lineage_id(config_path),
        "specification": specification,
        "specification_sha256": specification_hash,
        "stock_policy_sha256": stock_policy_sha256(),
        "implementation_sha256": "initial-implementation",
        "market_state": {
            "benchmark": "SPY",
            "open": 100.0,
            "close": 100.0,
            "risk_on": True,
        },
        "market_breadth": {"breadth_risk_on": True},
        "independent_price_validation": {"status": "missing", "passed": False},
        "earnings_risk_validation": {"status": "missing", "passed": False},
        "share_turnover_data_validation": {"status": "missing", "passed": False},
        "eligible_for_primary_prospective_performance": False,
        "operational_action_gate_passed": False,
        "arms": {name: {**arm, "arm": name} for name in arm_names},
        "inputs": {
            "config": config_path.name,
            "config_sha256": file_sha256(config_path),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = state_dir / f"state-{session.strftime('%Y%m%d')}-initial.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
