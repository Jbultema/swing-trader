from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from swing_trader.stock_candidates import record_current_stock_candidates
from swing_trader.stock_live_data import write_current_stock_price_snapshot
from swing_trader.stock_prospective import (
    verify_stock_shadow_evaluation,
    write_stock_shadow_evaluation,
)
from swing_trader.stock_shadow_state import (
    StockShadowConfig,
    StockShadowStateError,
    _advance_arm,
    _rebase_adjusted_shares,
    _validate_state_transition,
    held_tickers_from_payload,
    record_stock_shadow_state,
    stock_shadow_lineage_id,
    verify_stock_shadow_state,
)
from swing_trader.stock_signals import ExitPolicy
from swing_trader.stock_universe import (
    SEC_COMPANY_TICKERS_URL,
    WIKIPEDIA_SP500_URL,
    SourceDocument,
    download_current_sp500_snapshot,
)


def test_state_executes_prior_target_then_schedules_loss_exit_for_next_open() -> None:
    sessions = pd.bdate_range("2026-10-05", periods=3)
    prices = _small_prices(sessions)
    config = _config()
    policy = ExitPolicy(
        hard_stop_fraction=config.hard_stop_fraction,
        atr_multiple=config.atr_multiple,
        maximum_holding_sessions=config.maximum_holding_sessions,
        rank_exit_multiple=config.rank_exit_multiple,
    )
    rank = pd.Series({"A": 1.0})

    initial = _advance_arm(
        "consensus",
        None,
        sessions[0],
        prices,
        _day(100.0),
        rank,
        ["A"],
        {"A"},
        set(),
        guarded=False,
        market_risk_on=True,
        config=config,
        policy=policy,
    )
    loss_close = _advance_arm(
        "consensus",
        initial,
        sessions[1],
        prices,
        _day(100.0, return_21d=-0.01),
        rank,
        ["A"],
        {"A"},
        set(),
        guarded=False,
        market_risk_on=True,
        config=config,
        policy=policy,
    )
    exited = _advance_arm(
        "consensus",
        loss_close,
        sessions[2],
        prices,
        _day(99.0, return_21d=-0.02),
        rank,
        [],
        {"A"},
        set(),
        guarded=False,
        market_risk_on=True,
        config=config,
        policy=policy,
    )

    assert initial["positions_at_close"] == {}
    assert initial["target_for_next_open"] == {"A": 0.1}
    buy = loss_close["executions_at_open"][0]
    assert (buy["ticker"], buy["action"], buy["price"], buy["reason"]) == (
        "A",
        "BUY",
        110.0,
        "prior_close_target",
    )
    assert buy["estimated_cost"] > 0.0
    assert loss_close["target_for_next_open"] == {}
    loss_decision = next(
        row for row in loss_close["decisions"] if row["ticker"] == "A"
    )
    assert loss_decision["action"] == "SELL"
    assert "hard_loss_limit" in loss_decision["reasons"]
    sell = exited["executions_at_open"][0]
    assert (sell["ticker"], sell["action"], sell["price"], sell["reason"]) == (
        "A",
        "SELL",
        99.0,
        "prior_close_target",
    )
    assert exited["positions_at_close"] == {}
    assert exited["paper_account_at_close"]["shares"] == {}
    assert exited["paper_account_at_close"]["cumulative_trading_cost"] > 0.0


def test_market_guard_blocks_entries_and_explains_the_skip() -> None:
    session = pd.Timestamp("2026-10-06")
    result = _advance_arm(
        "consensus_market_guard",
        None,
        session,
        _small_prices(pd.DatetimeIndex([session])).iloc[[0]],
        _day(100.0),
        pd.Series({"A": 1.0}),
        ["A"],
        {"A"},
        set(),
        guarded=True,
        market_risk_on=False,
        config=_config(),
        policy=ExitPolicy(),
    )

    assert result["target_for_next_open"] == {}
    assert result["decisions"][0]["action"] == "SKIP"
    assert result["decisions"][0]["reasons"] == ["market_regime_risk_off"]


def test_state_transition_refuses_to_invent_a_missed_session() -> None:
    sessions = pd.bdate_range("2026-10-05", periods=3)
    previous = {
        "as_of_session": sessions[0].date().isoformat(),
        "specification_sha256": "spec",
        "stock_policy_sha256": "policy",
    }

    with pytest.raises(StockShadowStateError, match="session gap"):
        _validate_state_transition(
            previous,
            sessions[2],
            sessions,
            "spec",
            "policy",
        )


def test_state_transition_continues_across_nonpolicy_package_changes() -> None:
    sessions = pd.bdate_range("2026-10-05", periods=2)
    previous = {
        "as_of_session": sessions[0].date().isoformat(),
        "specification_sha256": "spec",
        "stock_policy_sha256": "policy",
        "implementation_sha256": "older-full-package",
    }

    _validate_state_transition(previous, sessions[1], sessions, "spec", "policy")

    with pytest.raises(StockShadowStateError, match="decision policy changed"):
        _validate_state_transition(previous, sessions[1], sessions, "spec", "changed-policy")


def test_full_initial_state_is_immutable_and_ineligible_without_free_cross_checks(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 10, 7, 20, tzinfo=UTC)
    symbols = [f"T{i:03d}" for i in range(490)]
    universe = _universe_snapshot(tmp_path / "universe", symbols, now)
    tickers = (*symbols, "SPY")
    sessions = pd.bdate_range(end="2026-10-06", periods=260)
    prices = _large_prices(sessions, tickers)
    universe_manifest = json.loads(universe.manifest_path.read_text())
    price_snapshot = write_current_stock_price_snapshot(
        prices,
        tickers,
        tmp_path / "prices",
        universe_manifest_path=universe.manifest_path,
        universe_manifest=universe_manifest,
        benchmark="SPY",
        captured_at=now,
        requested_start=date(2025, 1, 1),
        requested_end_exclusive=date(2026, 10, 7),
    )
    candidate = record_current_stock_candidates(
        universe.manifest_path,
        price_snapshot.manifest_path,
        tmp_path / "candidates",
        now=now,
    )

    result = record_stock_shadow_state(
        candidate.path,
        universe.manifest_path,
        price_snapshot.manifest_path,
        Path(__file__).parents[1] / "config/stock_shadow.toml",
        tmp_path / "states",
        now=now,
    )
    payload = json.loads(result.path.read_text())

    assert result.initialization
    assert result.targets == {"consensus": 10, "consensus_market_guard": 10}
    assert payload["eligible_for_primary_prospective_performance"] is False
    assert payload["operational_action_gate_passed"] is False
    assert payload["independent_price_validation"]["status"] == "missing"
    assert payload["earnings_risk_validation"]["status"] == "missing"
    assert payload["arms"]["consensus"]["prospective_role"] == "primary_consensus"
    assert payload["arms"]["consensus"]["paper_account_at_close"]["total_equity"] == 1.0
    assert payload["arms"]["consensus"]["paper_account_at_close"]["shares"] == {}
    assert payload["arms"]["consensus_market_guard"]["prospective_role"] == (
        "diagnostic_market_guard_comparator"
    )
    assert verify_stock_shadow_state(result.path)
    evaluation = write_stock_shadow_evaluation(
        result.path.parent,
        tmp_path / "evaluations",
        now=now,
    )
    assert verify_stock_shadow_evaluation(evaluation)

    payload["action_authorized"] = True
    result.path.write_text(json.dumps(payload), encoding="utf-8")
    assert not verify_stock_shadow_state(result.path)


def test_free_validation_shortlist_uses_only_primary_arm_holdings() -> None:
    payload = {
        "arms": {
            "consensus": {"positions_at_close": {"A": {}, "B": {}}},
            "consensus_market_guard": {"positions_at_close": {"C": {}}},
        }
    }

    assert held_tickers_from_payload(payload, arm_names=("consensus",)) == ("A", "B")
    assert held_tickers_from_payload(payload) == ("A", "B", "C")


def test_lineage_id_binds_decision_policy_and_frozen_config() -> None:
    config_path = Path(__file__).parents[1] / "config/stock_shadow.toml"

    lineage = stock_shadow_lineage_id(config_path)

    assert lineage.startswith("policy-")
    assert "-config-" in lineage


def test_paper_shares_rebase_when_adjusted_history_revises() -> None:
    sessions = pd.bdate_range("2026-10-05", periods=2)
    prices = _small_prices(sessions)
    prices.loc[sessions[0], ("Close", "A")] = 50.0
    shares = {"A": 1.0}
    prior_arm = {
        "paper_account_at_close": {
            "mark_session": sessions[0].date().isoformat(),
            "adjusted_close_marks": {"A": 100.0},
        }
    }

    factors = _rebase_adjusted_shares(prior_arm, shares, prices)

    assert factors == {"A": 2.0}
    assert shares == {"A": 2.0}


def _config() -> StockShadowConfig:
    return StockShadowConfig(
        maximum_positions=10,
        maximum_position_weight=0.1,
        hard_stop_fraction=0.08,
        atr_multiple=3.0,
        rank_exit_multiple=2.0,
        maximum_holding_sessions=21,
        benchmark="SPY",
        moving_average_sessions=200,
        volatility_sessions=20,
        maximum_annualized_volatility=0.35,
        arms=("consensus", "consensus_market_guard"),
        earnings_lead_sessions=2,
        earnings_cooling_sessions=1,
        round_trip_cost_bps=50.0,
        evaluation_horizons=(5, 21, 63),
    )


def _day(close: float, *, return_21d: float = 0.05) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "close": [close],
            "eligible": [True],
            "trend_positive": [True],
            "return_21d": [return_21d],
            "return_63d": [0.1],
            "return_12_1": [0.2],
            "proximity_52w_high": [0.99],
            "volume_ratio_20_126": [1.2],
            "atr_fraction_14d": [0.01],
            "score_short_volume": [1.0],
            "score_smooth_momentum": [1.0],
            "score_volume_breakout": [1.0],
        },
        index=["A"],
    )


def _small_prices(sessions: pd.DatetimeIndex) -> pd.DataFrame:
    opens = [100.0, 110.0, 99.0][: len(sessions)]
    closes = [100.0, 100.0, 99.0][: len(sessions)]
    frame = pd.concat(
        {
            "Open": pd.DataFrame({"A": opens}, index=sessions),
            "Close": pd.DataFrame({"A": closes}, index=sessions),
        },
        axis=1,
    )
    frame.columns.names = ["field", "ticker"]
    return frame


def _universe_snapshot(
    output: Path,
    symbols: list[str],
    captured_at: datetime,
):
    table = pd.DataFrame(
        {
            "Symbol": symbols,
            "Security": [f"Company {i}" for i in range(len(symbols))],
            "GICS Sector": ["Industrials"] * len(symbols),
            "GICS Sub-Industry": ["Research"] * len(symbols),
            "Headquarters Location": ["Denver, Colorado"] * len(symbols),
            "Date added": ["2020-01-02"] * len(symbols),
            "CIK": [1000 + i for i in range(len(symbols))],
            "Founded": ["2000"] * len(symbols),
        }
    )
    sec = {
        str(i): {
            "cik_str": 1000 + i,
            "ticker": symbol,
            "title": f"Company {i}",
        }
        for i, symbol in enumerate(symbols)
    }
    documents = {
        WIKIPEDIA_SP500_URL: SourceDocument(
            WIKIPEDIA_SP500_URL,
            table.to_html(index=False, table_id="constituents").encode(),
            captured_at.isoformat(),
            "text/html",
        ),
        SEC_COMPANY_TICKERS_URL: SourceDocument(
            SEC_COMPANY_TICKERS_URL,
            json.dumps(sec).encode(),
            captured_at.isoformat(),
            "application/json",
        ),
    }
    return download_current_sp500_snapshot(
        output,
        fetcher=documents.__getitem__,
        captured_at=captured_at,
    )


def _large_prices(
    sessions: pd.DatetimeIndex,
    tickers: tuple[str, ...],
) -> pd.DataFrame:
    trend = np.linspace(1.0, 1.35, len(sessions))
    close = pd.DataFrame(
        {
            ticker: (50.0 + position / 10.0) * trend
            for position, ticker in enumerate(tickers)
        },
        index=sessions,
    )
    frame = pd.concat(
        {
            "Open": close - 0.1,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": pd.DataFrame(1_000_000.0, index=sessions, columns=tickers),
        },
        axis=1,
    )
    frame.columns.names = ["field", "ticker"]
    return frame
