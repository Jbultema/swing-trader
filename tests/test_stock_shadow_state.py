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
    _market_breadth_state,
    _rebase_adjusted_shares,
    _validate_state_transition,
    held_tickers_from_payload,
    record_stock_shadow_state,
    stock_shadow_lineage_id,
    verify_stock_shadow_state,
)
from swing_trader.stock_shares import write_current_stock_share_snapshot
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


def test_market_breadth_guard_uses_distinct_explainable_skip_reason() -> None:
    session = pd.Timestamp("2026-10-06")
    result = _advance_arm(
        "consensus_breadth_guard",
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
        guard_reason="market_breadth_guard_risk_off",
    )

    assert result["target_for_next_open"] == {}
    assert result["decisions"][0]["reasons"] == ["market_breadth_guard_risk_off"]


def test_market_breadth_state_requires_coverage_and_majority_participation() -> None:
    sessions = pd.bdate_range("2026-09-01", periods=21)
    close = pd.DataFrame(
        {
            "A": range(100, 121),
            "B": range(200, 221),
            "C": range(300, 279, -1),
        },
        index=sessions,
        dtype=float,
    )
    config = _config()

    state = _market_breadth_state(
        close,
        {"A", "B", "C"},
        sessions[-1],
        config,
        benchmark_risk_on=True,
        previous_breadth_risk_on=None,
    )

    assert state["data_gate_passed"] is True
    assert state["mean_advancing_fraction"] == pytest.approx(2 / 3)
    assert state["combined_risk_on"] is True

    failed = _market_breadth_state(
        close[["A", "B"]],
        {"A", "B", "C"},
        sessions[-1],
        config,
        benchmark_risk_on=True,
        previous_breadth_risk_on=None,
    )
    assert failed["data_gate_passed"] is False
    assert failed["combined_risk_on"] is False


def test_market_breadth_hysteresis_holds_state_inside_turnover_band() -> None:
    sessions = pd.bdate_range("2026-09-01", periods=21)
    close = pd.DataFrame(
        {
            "A": range(100, 121),
            "B": range(200, 221),
            "C": range(300, 279, -1),
            "D": range(400, 379, -1),
        },
        index=sessions,
        dtype=float,
    )
    config = _config()

    stays_on = _market_breadth_state(
        close,
        set(close.columns),
        sessions[-1],
        config,
        benchmark_risk_on=True,
        previous_breadth_risk_on=True,
    )
    stays_off = _market_breadth_state(
        close,
        set(close.columns),
        sessions[-1],
        config,
        benchmark_risk_on=True,
        previous_breadth_risk_on=False,
    )

    assert stays_on["mean_advancing_fraction"] == pytest.approx(0.5)
    assert stays_on["breadth_risk_on"] is True
    assert stays_on["breadth_state_transition"] == "unchanged"
    assert stays_off["breadth_risk_on"] is False
    assert stays_off["breadth_state_transition"] == "unchanged"


def test_sector_entry_cap_leaves_excess_weight_in_cash_and_explains_skip() -> None:
    session = pd.Timestamp("2026-10-06")
    tickers = ["A", "B", "C", "D"]
    prices = pd.concat(
        {
            "Open": pd.DataFrame([dict.fromkeys(tickers, 100.0)], index=[session]),
            "Close": pd.DataFrame([dict.fromkeys(tickers, 100.0)], index=[session]),
        },
        axis=1,
    )
    prices.columns.names = ["field", "ticker"]
    day = pd.concat([_day(100.0).rename(index={"A": ticker}) for ticker in tickers])
    result = _advance_arm(
        "share_turnover_hold21",
        None,
        session,
        prices,
        day,
        pd.Series({ticker: float(rank) for rank, ticker in enumerate(tickers, 1)}),
        tickers,
        set(tickers),
        set(),
        guarded=False,
        market_risk_on=True,
        config=_config(),
        policy=ExitPolicy(maximum_holding_sessions=21),
        sector_by_ticker=dict.fromkeys(tickers, "Information Technology"),
    )

    assert result["target_for_next_open"] == {"A": 0.1, "B": 0.1, "C": 0.1}
    assert result["target_cash_weight"] == pytest.approx(0.7)
    skipped = next(row for row in result["decisions"] if row["ticker"] == "D")
    assert skipped["action"] == "SKIP"
    assert skipped["reasons"] == ["gics_sector_position_cap"]
    assert skipped["gics_sector"] == "Information Technology"


def test_unavailable_signal_schedules_a_fail_safe_exit_without_new_entries() -> None:
    sessions = pd.bdate_range("2026-10-05", periods=2)
    prices = _small_prices(sessions)
    config = _config()
    initial = _advance_arm(
        "share_turnover_hold5",
        None,
        sessions[0],
        prices,
        _day(100.0),
        pd.Series({"A": 1.0}),
        ["A"],
        {"A"},
        set(),
        guarded=False,
        market_risk_on=True,
        config=config,
        policy=ExitPolicy(maximum_holding_sessions=5),
    )

    failed = _advance_arm(
        "share_turnover_hold5",
        initial,
        sessions[1],
        prices,
        _day(100.0),
        pd.Series(dtype=float),
        [],
        {"A"},
        set(),
        guarded=False,
        market_risk_on=True,
        config=config,
        policy=ExitPolicy(maximum_holding_sessions=5),
        signal_available=False,
        signal_unavailable_reason="share_turnover_data_gate_failed_fail_safe_exit",
    )

    assert failed["executions_at_open"][0]["action"] == "BUY"
    assert failed["target_for_next_open"] == {}
    assert failed["signal_data_available_at_close"] is False
    decision = next(row for row in failed["decisions"] if row["ticker"] == "A")
    assert decision["action"] == "SELL"
    assert decision["reasons"] == [
        "share_turnover_data_gate_failed_fail_safe_exit"
    ]


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
    share_frame = pd.DataFrame(
        {
            "ticker": symbols,
            "shares_outstanding": pd.array(
                [2_000_000 - position * 1_000 for position in range(len(symbols))],
                dtype="Int64",
            ),
            "provider_observation_date": pd.to_datetime(["2026-10-01"] * len(symbols)),
            "captured_at_utc": [now.isoformat()] * len(symbols),
            "source_status": ["available"] * len(symbols),
            "source_observation_count": [1] * len(symbols),
            "discarded_historical_observations": [0] * len(symbols),
            "provider_request_attempts": [1] * len(symbols),
        }
    )
    share_snapshot = write_current_stock_share_snapshot(
        share_frame,
        symbols,
        tmp_path / "shares",
        universe_manifest_path=universe.manifest_path,
        universe_manifest=universe_manifest,
        captured_at=now,
        request_start=date(2025, 9, 2),
        request_end_exclusive=date(2026, 10, 8),
        minimum_coverage_fraction=1.0,
    )
    candidate = record_current_stock_candidates(
        universe.manifest_path,
        price_snapshot.manifest_path,
        tmp_path / "candidates",
        share_manifest_path=share_snapshot.manifest_path,
        now=now,
    )

    result = record_stock_shadow_state(
        candidate.path,
        universe.manifest_path,
        price_snapshot.manifest_path,
        share_snapshot.manifest_path,
        Path(__file__).parents[1] / "config/stock_shadow.toml",
        tmp_path / "states",
        now=now,
    )
    payload = json.loads(result.path.read_text())

    assert result.initialization
    assert result.targets == {
        "consensus": 10,
        "consensus_market_guard": 10,
        "consensus_breadth_guard": 10,
        "classic_12_1_hold21": 10,
        "short_volume_hold5": 10,
        "short_volume_hold10": 10,
        "short_volume_hold21": 10,
        "share_turnover_hold5": 10,
        "share_turnover_hold10": 10,
        "share_turnover_hold21": 10,
    }
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
    assert payload["market_breadth"]["data_gate_passed"] is True
    assert payload["arms"]["consensus_breadth_guard"]["prospective_role"] == (
        "diagnostic_market_breadth_guard_comparator"
    )
    assert payload["arms"]["short_volume_hold5"]["prospective_role"] == (
        "diagnostic_short_volume_max_hold_5_sessions"
    )
    assert payload["arms"]["short_volume_hold5"]["independent_price_validation_applies"] is False
    assert payload["arms"]["short_volume_hold5"]["maximum_holding_sessions"] == 5
    assert payload["arms"]["classic_12_1_hold21"]["prospective_role"] == (
        "diagnostic_simple_classic_12_1_momentum_control"
    )
    assert payload["arms"]["classic_12_1_hold21"]["signal_family"] == "classic_12_1"
    assert payload["arms"]["classic_12_1_hold21"]["maximum_holding_sessions"] == 21
    assert payload["arms"]["share_turnover_hold5"]["prospective_role"] == (
        "diagnostic_academic_share_turnover_skip3_max_hold_5_sessions"
    )
    assert payload["arms"]["share_turnover_hold5"]["signal_family"] == (
        "share_turnover_skip3"
    )
    assert payload["share_turnover_data_validation"]["passed"] is True
    assert verify_stock_shadow_state(result.path)
    evaluation = write_stock_shadow_evaluation(
        result.path.parent,
        tmp_path / "evaluations",
        now=now,
    )
    assert verify_stock_shadow_evaluation(evaluation)

    failed_share_frame = share_frame.copy()
    failed_rows = failed_share_frame.index[:10]
    failed_share_frame.loc[failed_rows, "shares_outstanding"] = pd.NA
    failed_share_frame.loc[failed_rows, "provider_observation_date"] = pd.NaT
    failed_share_frame.loc[failed_rows, "source_status"] = "fetch_error"
    failed_share_frame.loc[failed_rows, "source_observation_count"] = 0
    failed_share_frame.loc[failed_rows, "discarded_historical_observations"] = 0
    failed_share_snapshot = write_current_stock_share_snapshot(
        failed_share_frame,
        symbols,
        tmp_path / "failed-shares",
        universe_manifest_path=universe.manifest_path,
        universe_manifest=universe_manifest,
        captured_at=now,
        request_start=date(2025, 9, 2),
        request_end_exclusive=date(2026, 10, 8),
        minimum_coverage_fraction=1.0,
    )
    failed_candidate = record_current_stock_candidates(
        universe.manifest_path,
        price_snapshot.manifest_path,
        tmp_path / "failed-candidates",
        share_manifest_path=failed_share_snapshot.manifest_path,
        now=now,
    )
    failed_candidate_payload = json.loads(failed_candidate.path.read_text())
    failed_signal = failed_candidate_payload["experimental_signals"][
        "share_turnover_skip3"
    ]
    assert failed_signal["status"] == "data_gate_failed"
    assert failed_signal["candidates"] == []
    assert len(failed_candidate_payload["consensus_candidates"]) == 10

    failed_state = record_stock_shadow_state(
        failed_candidate.path,
        universe.manifest_path,
        price_snapshot.manifest_path,
        failed_share_snapshot.manifest_path,
        Path(__file__).parents[1] / "config/stock_shadow.toml",
        tmp_path / "failed-states",
        now=now,
    )
    failed_payload = json.loads(failed_state.path.read_text())
    assert failed_payload["share_turnover_data_validation"]["passed"] is False
    assert failed_payload["share_turnover_data_validation"][
        "primary_consensus_gate_affected"
    ] is False
    assert len(failed_payload["arms"]["consensus"]["target_for_next_open"]) == 10
    for arm_name in (
        "share_turnover_hold5",
        "share_turnover_hold10",
        "share_turnover_hold21",
    ):
        arm = failed_payload["arms"][arm_name]
        assert arm["target_for_next_open"] == {}
        assert arm["signal_data_available_at_close"] is False
        assert arm["eligible_for_diagnostic_prospective_performance"] is False
    assert verify_stock_shadow_state(failed_state.path)

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
        maximum_positions_per_sector=3,
        hard_stop_fraction=0.08,
        atr_multiple=3.0,
        rank_exit_multiple=2.0,
        maximum_holding_sessions=21,
        benchmark="SPY",
        moving_average_sessions=200,
        volatility_sessions=20,
        maximum_annualized_volatility=0.35,
        breadth_lookback_sessions=20,
        minimum_breadth_coverage_fraction=0.95,
        breadth_risk_on_entry_fraction=0.55,
        breadth_risk_off_exit_fraction=0.45,
        arms=(
            "consensus",
            "consensus_market_guard",
            "consensus_breadth_guard",
            "classic_12_1_hold21",
            "short_volume_hold5",
            "short_volume_hold10",
            "short_volume_hold21",
            "share_turnover_hold5",
            "share_turnover_hold10",
            "share_turnover_hold21",
        ),
        classic_momentum_holding_sessions=21,
        short_volume_holding_sessions=(5, 10, 21),
        share_turnover_holding_sessions=(5, 10, 21),
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
    sectors = (
        "Communication Services",
        "Consumer Discretionary",
        "Consumer Staples",
        "Energy",
        "Financials",
        "Health Care",
        "Industrials",
        "Information Technology",
        "Materials",
        "Utilities",
    )
    table = pd.DataFrame(
        {
            "Symbol": symbols,
            "Security": [f"Company {i}" for i in range(len(symbols))],
            "GICS Sector": [sectors[i % len(sectors)] for i in range(len(symbols))],
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
    close = pd.DataFrame(
        {
            ticker: (50.0 + position / 10.0)
            * np.linspace(1.0, 1.10 + position / 1_000.0, len(sessions))
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
