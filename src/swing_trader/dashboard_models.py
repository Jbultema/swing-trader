from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from swing_trader.stock_shadow_state import stock_shadow_lineage_dir, verify_stock_shadow_state


@dataclass(frozen=True)
class StockDashboardState:
    path: Path
    payload: dict[str, object]
    current_policy: bool
    integrity_passed: bool


_REASON_LABELS = {
    "top_consensus_entry": "Strongest agreement across the three momentum screens",
    "retained_consensus_signal": "Still passes the momentum, trend, rank, and risk rules",
    "retained_signal": "Still passes the selected signal and exit rules",
    "hard_loss_limit": "Closed below the fixed loss limit",
    "atr_trailing_exit": "Closed below the volatility-adjusted trailing exit",
    "trend_broken": "Price trend no longer has the required 50/200-day structure",
    "short_momentum_non_positive": "The latest 21-session return is no longer positive",
    "rank_decay": "Cross-sectional rank deteriorated beyond the retention band",
    "maximum_holding_period": "Reached the maximum holding period",
    "market_regime_risk_off": "Benchmark trend or volatility guard is risk-off",
    "market_breadth_guard_risk_off": "Market participation is too weak for new risk",
    "scheduled_earnings_entry_blackout": "A scheduled earnings event blocks a new entry",
    "left_point_in_time_universe": "Security left the eligible point-in-time universe",
    "missing_close": "A trustworthy close was unavailable, so the position must exit",
    "missing_risk_feature": "A required exit-risk input was unavailable",
    "missed_session_fail_closed_exit": "A scheduled close was missed, so the recovery state exits to cash without inventing a backdated signal",
    "missed_session_market_state_unavailable": "The historical market-state decision was not captured prospectively",
    "top_classic_12_1_momentum_entry": "Top positive 12-to-1-month momentum control",
    "top_short_volume_entry": "Top short-horizon price and relative-volume signal",
    "top_share_turnover_skip3_entry": "Top return and share-turnover intersection",
}


def latest_stock_dashboard_state(
    lineage_root: Path,
    config_path: Path,
) -> StockDashboardState | None:
    paths = list(lineage_root.glob("*/states/state-*.json"))
    if not paths:
        return None
    path = max(paths, key=lambda candidate: candidate.name)
    integrity = verify_stock_shadow_state(path)
    payload = json.loads(path.read_text(encoding="utf-8")) if integrity else {}
    current = path.parents[1] == stock_shadow_lineage_dir(lineage_root, config_path)
    return StockDashboardState(
        path=path,
        payload=payload,
        current_policy=current,
        integrity_passed=integrity,
    )


def stock_decision_headline(state: StockDashboardState) -> dict[str, object]:
    if not state.integrity_passed:
        return {
            "label": "STOP",
            "headline": "Stock state failed integrity verification",
            "detail": "Do not use the displayed targets. Rebuild and verify the stock state.",
            "tone": "critical",
        }
    payload = state.payload
    primary = _primary_arm(payload)
    decisions = _records(primary.get("decisions"))
    counts = {
        action: sum(str(row.get("action", "")).upper() == action for row in decisions)
        for action in ("BUY", "SELL", "HOLD", "SKIP")
    }
    market = _mapping(payload.get("market_state"))
    gate = bool(payload.get("eligible_for_primary_prospective_performance"))
    if isinstance(payload.get("gap_recovery"), Mapping):
        return {
            "label": "NO ACTION",
            "headline": "Missed session was recovered fail-closed",
            "detail": "Observed prices preserve paper accounting, but no backdated signal was created. The next current refresh can resume from cash.",
            "tone": "warning",
            **counts,
        }
    if not state.current_policy:
        return {
            "label": "WAIT",
            "headline": "Refresh the stock decision before reviewing a trade",
            "detail": "The latest intact state belongs to an older policy lineage. Its targets remain visible for audit only.",
            "tone": "warning",
            **counts,
        }
    if not gate:
        return {
            "label": "NO ACTION",
            "headline": "Independent decision data gate did not pass",
            "detail": "Targets are diagnostic only. No paper or live execution should be inferred from this state.",
            "tone": "critical",
            **counts,
        }
    if not bool(market.get("risk_on")):
        return {
            "label": "RISK OFF",
            "headline": "Exit or remain in cash at the next regular-session open",
            "detail": _count_detail(counts),
            "tone": "critical",
            **counts,
        }
    return {
        "label": "REVIEW",
        "headline": _action_sentence(counts),
        "detail": "Signals were known at the close; any human execution is no earlier than the next regular-session open.",
        "tone": "success" if counts["BUY"] + counts["SELL"] == 0 else "warning",
        **counts,
    }


def stock_action_frame(
    payload: Mapping[str, object],
    *,
    arm_name: str = "consensus",
) -> pd.DataFrame:
    arm = _arm(payload, arm_name)
    target = {
        str(ticker): float(weight)
        for ticker, weight in _mapping(arm.get("target_for_next_open")).items()
    }
    rows: list[dict[str, object]] = []
    for decision in _records(arm.get("decisions")):
        ticker = str(decision.get("ticker", ""))
        reasons = _reason_list(decision.get("reasons"))
        rows.append(
            {
                "Action": str(decision.get("action", "")).upper(),
                "Ticker": ticker,
                "Sector": str(decision.get("gics_sector") or "Unknown"),
                "Why": "; ".join(human_reason(reason) for reason in reasons),
                "Target weight": target.get(ticker, 0.0),
                "Rank": _number(decision.get("selection_rank") or decision.get("consensus_rank")),
                "Close": _number(decision.get("close")),
                "21-session return": _number(decision.get("return_21d")),
                "63-session return": _number(decision.get("return_63d")),
                "12-to-1 return": _number(decision.get("return_12_1")),
                "Distance from 52-week high": _distance_from_high(
                    decision.get("proximity_52w_high")
                ),
                "Volume vs baseline": _number(decision.get("volume_ratio_20_126")),
                "Holding sessions": _integer(decision.get("holding_sessions")),
                "Entry price": _number(
                    decision.get("entry_adjusted_open") or decision.get("entry_price")
                ),
                "Hard-loss exit": _number(decision.get("hard_loss_trigger_adjusted_close")),
                "ATR trailing exit": _number(
                    decision.get("atr_trailing_trigger_adjusted_close")
                ),
                "Effective": str(decision.get("effective_at") or "next regular-session open"),
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "Action",
                "Ticker",
                "Sector",
                "Why",
                "Target weight",
                "Rank",
                "Close",
            ]
        )
    action_order = {"SELL": 0, "BUY": 1, "HOLD": 2, "SKIP": 3}
    frame = pd.DataFrame(rows)
    frame["_order"] = frame["Action"].map(action_order).fillna(9)
    return frame.sort_values(["_order", "Rank", "Ticker"], na_position="last").drop(
        columns="_order"
    )


def stock_target_frame(
    payload: Mapping[str, object],
    *,
    arm_name: str = "consensus",
) -> pd.DataFrame:
    arm = _arm(payload, arm_name)
    decisions = stock_action_frame(payload, arm_name=arm_name)
    why_by_ticker = (
        decisions.set_index("Ticker")["Why"].to_dict() if not decisions.empty else {}
    )
    rows = [
        {
            "Ticker": str(ticker),
            "Target weight": float(weight),
            "Why held": why_by_ticker.get(str(ticker), "Selected by the frozen policy"),
        }
        for ticker, weight in _mapping(arm.get("target_for_next_open")).items()
    ]
    cash_weight = float(arm.get("target_cash_weight", max(0.0, 1.0 - sum(row["Target weight"] for row in rows))))
    rows.append(
        {
            "Ticker": "CASH",
            "Target weight": cash_weight,
            "Why held": "Unallocated capital remains in cash; the system never forces full investment.",
        }
    )
    return pd.DataFrame(rows).sort_values("Target weight", ascending=False, kind="stable")


def percentage_points_frame(
    frame: pd.DataFrame,
    columns: Sequence[str],
) -> pd.DataFrame:
    """Return a display-only copy with decimal fractions expressed as percent points."""
    display = frame.copy()
    for column in columns:
        if column in display.columns:
            display[column] = pd.to_numeric(display[column], errors="coerce") * 100.0
    return display


def stock_price_map(payload: Mapping[str, object], *, arm_name: str = "consensus") -> dict[str, float]:
    arm = _arm(payload, arm_name)
    prices: dict[str, float] = {}
    for decision in _records(arm.get("decisions")):
        price = _number(decision.get("close"))
        ticker = str(decision.get("ticker", "")).upper()
        if ticker and price is not None and price > 0.0:
            prices[ticker] = price
    account = _mapping(arm.get("paper_account_at_close"))
    for ticker, price in _mapping(account.get("adjusted_close_marks")).items():
        number = _number(price)
        if number is not None and number > 0.0:
            prices[str(ticker).upper()] = number
    return prices


def stock_risk_summary(payload: Mapping[str, object]) -> dict[str, object]:
    market = _mapping(payload.get("market_state"))
    breadth = _mapping(payload.get("market_breadth"))
    primary = _primary_arm(payload)
    account = _mapping(primary.get("paper_account_at_close"))
    return {
        "market_regime": "RISK ON" if bool(market.get("risk_on")) else "RISK OFF",
        "spy_close": _number(market.get("close")),
        "spy_200d_average": _number(market.get("moving_average")),
        "spy_realized_volatility": _number(market.get("realized_volatility_annualized")),
        "volatility_limit": _number(market.get("maximum_annualized_volatility")),
        "breadth_regime": "RISK ON" if bool(breadth.get("breadth_risk_on")) else "RISK OFF",
        "mean_advancing_fraction": _number(breadth.get("mean_advancing_fraction")),
        "breadth_gate_passed": bool(breadth.get("data_gate_passed")),
        "gross_exposure": _number(account.get("gross_exposure")),
        "cash_weight": _number(account.get("cash_weight")),
        "cumulative_cost": _number(account.get("cumulative_trading_cost")),
    }


def policy_forward_frame(
    state_paths: Sequence[Path],
    *,
    lineage_id: str,
    arm_name: str = "consensus",
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for path in sorted(state_paths, key=lambda candidate: candidate.name):
        if not verify_stock_shadow_state(path):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("lineage_id") != lineage_id:
            continue
        arm = _arm(payload, arm_name)
        account = _mapping(arm.get("paper_account_at_close"))
        market = _mapping(payload.get("market_state"))
        equity = _number(account.get("total_equity"))
        spy_close = _number(market.get("close"))
        if equity is None or spy_close is None:
            continue
        rows.append(
            {
                "date": pd.Timestamp(str(payload["as_of_session"])),
                "policy_equity": equity,
                "spy_close": spy_close,
                "eligible": bool(payload.get("eligible_for_primary_prospective_performance")),
                "recovery": isinstance(payload.get("gap_recovery"), Mapping),
                "state_record_sha256": payload.get("record_sha256"),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    frame["Policy"] = frame["policy_equity"] / float(frame.iloc[0]["policy_equity"])
    frame["SPY"] = frame["spy_close"] / float(frame.iloc[0]["spy_close"])
    frame["Policy return"] = frame["Policy"] - 1.0
    frame["SPY return"] = frame["SPY"] - 1.0
    frame["Excess return"] = frame["Policy return"] - frame["SPY return"]
    frame["Policy drawdown"] = frame["Policy"] / frame["Policy"].cummax() - 1.0
    return frame.reset_index(drop=True)


def prospective_maturity(evaluation: Mapping[str, object] | None) -> dict[str, object]:
    if not evaluation:
        return {
            "status": "not_started",
            "sessions": 0,
            "required_sessions": 126,
            "completed_exits": 0,
            "required_exits": 30,
            "risk_off_sessions": 0,
            "required_risk_off_sessions": 10,
            "message": "No forward outcome evaluation exists for this policy lineage yet.",
        }
    eligible = _mapping(evaluation.get("eligible_original_gate_only"))
    readiness = _mapping(evaluation.get("readiness"))
    sessions = int(eligible.get("sessions") or 0)
    exits = int(eligible.get("completed_exits") or 0)
    risk_off = int(eligible.get("risk_off_sessions") or 0)
    required_sessions = int(readiness.get("minimum_eligible_sessions") or 126)
    required_exits = int(readiness.get("minimum_completed_exits") or 30)
    required_risk_off = int(readiness.get("minimum_risk_off_sessions") or 10)
    if readiness.get("status") == "insufficient_prospective_evidence":
        message = (
            "Forward monitoring is collecting evidence. Metrics remain intentionally blank until "
            "at least one eligible transition exists; readiness requires all three milestones."
        )
    else:
        message = (
            "The preregistered evidence minimum is met. This remains paper evidence and does not "
            "authorize automated or live trading."
        )
    return {
        "status": str(readiness.get("status") or "unknown"),
        "sessions": sessions,
        "required_sessions": required_sessions,
        "session_progress": min(sessions / required_sessions, 1.0),
        "completed_exits": exits,
        "required_exits": required_exits,
        "exit_progress": min(exits / required_exits, 1.0),
        "risk_off_sessions": risk_off,
        "required_risk_off_sessions": required_risk_off,
        "risk_off_progress": min(risk_off / required_risk_off, 1.0),
        "message": message,
    }


def prospective_performance_rows(
    evaluation: Mapping[str, object] | None,
) -> pd.DataFrame:
    if not evaluation:
        return pd.DataFrame()
    eligible = _mapping(evaluation.get("eligible_original_gate_only"))
    primary = _mapping(eligible.get("primary_consensus"))
    spy = _mapping(eligible.get("spy_buy_hold"))
    sessions = int(eligible.get("sessions") or 0)
    if sessions == 0:
        return pd.DataFrame()
    fields = (
        ("Cumulative return", "cumulative_return"),
        ("Annualized return", "annualized_return"),
        ("Annualized volatility", "annualized_volatility"),
        ("Sharpe vs cash", "sharpe_zero_cash_benchmark"),
        ("Maximum drawdown", "maximum_drawdown"),
        ("Positive sessions", "positive_session_fraction"),
    )
    return pd.DataFrame(
        [
            {
                "Metric": label,
                "Policy": _number(primary.get(key)),
                "SPY": _number(spy.get(key)),
            }
            for label, key in fields
        ]
    )


def human_reason(reason: str) -> str:
    return _REASON_LABELS.get(reason, reason.replace("_", " ").strip().capitalize())


def _action_sentence(counts: Mapping[str, int]) -> str:
    parts = []
    for action, noun in (("BUY", "buy"), ("SELL", "sell"), ("HOLD", "hold"), ("SKIP", "skip")):
        count = int(counts.get(action, 0))
        if count:
            parts.append(f"{count} {noun}{'' if count == 1 else 's'}")
    return "Next-open review: " + ", ".join(parts) if parts else "No stock action is proposed"


def _count_detail(counts: Mapping[str, int]) -> str:
    sells = int(counts.get("SELL", 0))
    holds = int(counts.get("HOLD", 0))
    return f"{sells} modeled sells and {holds} residual holds are recorded in the decision ledger."


def _primary_arm(payload: Mapping[str, object]) -> Mapping[str, object]:
    return _arm(payload, "consensus")


def _arm(payload: Mapping[str, object], arm_name: str) -> Mapping[str, object]:
    arm = _mapping(payload.get("arms")).get(arm_name)
    return arm if isinstance(arm, Mapping) else {}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _records(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _reason_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)] if value else []


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _distance_from_high(value: object) -> float | None:
    number = _number(value)
    return number - 1.0 if number is not None else None
