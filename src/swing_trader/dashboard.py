from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from swing_trader.finra_activity import audit_finra_activity_snapshot
from swing_trader.sec_filing_events import audit_sec_event_snapshot
from swing_trader.stock_audit import audit_stock_research_bundle
from swing_trader.stock_prospective import verify_stock_shadow_evaluation
from swing_trader.stock_shadow_state import (
    stock_shadow_lineage_dir,
    verify_stock_shadow_state,
)

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports/latest"
SHADOW = ROOT / "reports/shadow"
STOCK_REPORTS = ROOT / "reports/stock/latest"
STOCK_LINEAGES = ROOT / "reports/stock-shadow/lineages"
STOCK_SHADOW_CONFIG = ROOT / "config/stock_shadow.toml"


def _latest_stock_state() -> Path | None:
    paths = list(STOCK_LINEAGES.glob("*/states/state-*.json"))
    return max(paths, key=lambda path: path.name) if paths else None


def _percentage(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.1%}"


def _stock_signal_label(value: object) -> str:
    if value == "classic_12_1":
        return "simple 12-1 cross-sectional individual-stock momentum control"
    if value == "short_volume":
        return (
            "short-horizon price plus relative-volume proxy "
            "(not FINRA short volume or academic share turnover)"
        )
    if value == "consensus":
        return "three-family consensus"
    if value == "share_turnover_skip3":
        return (
            "academic short-term momentum adaptation: prior return plus true "
            "volume/shares turnover, latest three sessions skipped"
        )
    return str(value or "legacy")


def _stock_arm_sort_key(name: str) -> tuple[int, int]:
    if name.startswith("classic_12_1_hold"):
        return (0, int(name.removeprefix("classic_12_1_hold")))
    if name.startswith("short_volume_hold"):
        return (1, int(name.removeprefix("short_volume_hold")))
    if name.startswith("share_turnover_hold"):
        return (2, int(name.removeprefix("share_turnover_hold")))
    return (-1, 0)


def _decimal(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.4f}"


st.set_page_config(page_title="Swing Trader", page_icon="↗", layout="wide")
st.title("Swing Trader")
st.caption("Explainable long-only research · human execution required · no broker connection")

if not (REPORTS / "manifest.json").exists():
    st.warning("No research snapshot. Run `swing-trader daily` first.")
    st.stop()

manifest = json.loads((REPORTS / "manifest.json").read_text())
decisions = json.loads((REPORTS / "latest_decisions.json").read_text())
metrics = pd.read_csv(REPORTS / "metrics.csv")
equity = pd.read_csv(REPORTS / "equity.csv", parse_dates=["date"]).set_index("date")
regimes = pd.read_csv(REPORTS / "regime_metrics.csv")
validation = json.loads((REPORTS / "validation_summary.json").read_text())
data_quality = json.loads((REPORTS / "data_quality.json").read_text())
prospective_path = REPORTS / "prospective_evaluation.json"
prospective = json.loads(prospective_path.read_text()) if prospective_path.exists() else None

status, as_of, exposure, shadow_count = st.columns(4)
status.metric(
    "Status",
    "DATA RECONCILED" if decisions["data_reconciled"] else "DATA GATE FAILED",
)
as_of.metric("Signals as of", decisions["as_of_close"])
exposure.metric("Gross exposure cap", f"{decisions['market']['gross_exposure_cap']:.0%}")
shadow_files = sorted(SHADOW.glob("*.json")) if SHADOW.exists() else []
shadow_count.metric("Locked shadows", len(shadow_files))

overview, action, risk, stock, methods = st.tabs(
    ["Performance", "Next action", "Risk", "Stock research", "Methods"]
)

with overview:
    full = metrics.loc[metrics["split"] == "full"].copy()
    cols = st.columns(4)
    candidate = full.loc[full["strategy"] == "classic_12m_dual_momentum"].iloc[0]
    cols[0].metric("CAGR", f"{candidate['cagr']:.1%}")
    cols[1].metric("Max drawdown", f"{candidate['max_drawdown']:.1%}")
    cols[2].metric("Sharpe (0% RF)", f"{candidate['sharpe_zero_rf']:.2f}")
    cols[3].metric("Annual turnover", f"{candidate['annualized_turnover']:.1f}×")
    normalized = equity.div(equity.iloc[0]).reset_index().melt("date", var_name="strategy")
    st.plotly_chart(
        px.line(normalized, x="date", y="value", color="strategy", log_y=True),
        width="stretch",
    )
    st.dataframe(
        metrics[
            [
                "strategy",
                "split",
                "cagr",
                "annualized_volatility",
                "sharpe_zero_rf",
                "max_drawdown",
                "calmar",
            ]
        ],
        hide_index=True,
        width="stretch",
    )

with action:
    st.subheader("Champion: monthly long-only dual momentum")
    st.caption(f"Last scheduled decision: {decisions['last_monthly_decision']}")
    if not decisions["data_reconciled"]:
        st.error(
            "No action is valid: the independent monthly price reconciliation did not pass. "
            "The table below is hypothetical research output only."
        )
    st.dataframe(
        pd.DataFrame(decisions["hypothetical_actions"]),
        hide_index=True,
        width="stretch",
    )
    st.info(
        "These are auditable research tickets, never orders. Even with reconciled data, "
        "live use requires separate prospective and account-readiness approval."
    )

with risk:
    overlay = decisions["capital_preservation_overlay"]
    if overlay["triggered"]:
        st.error(
            "Capital-preservation overlay is triggered and would exit to cash. "
            "It remains a comparator without allocation authority."
        )
    else:
        st.success("Capital-preservation overlay is not triggered.")
    st.json(overlay)
    st.subheader("Known hostile regimes")
    st.dataframe(regimes, hide_index=True, width="stretch")
    st.json(decisions["market"])
    st.warning(
        "Stops reduce modeled exposure after observed damage; they cannot prevent overnight gaps or guarantee an execution price."
    )

with stock:
    st.subheader("Individual-stock momentum research")
    latest_stock_state = _latest_stock_state()
    if latest_stock_state is None:
        st.info(
            "No prospective stock state exists yet. Run `swing-trader stock-daily` after a "
            "completed U.S. session; the command never places an order."
        )
    elif not verify_stock_shadow_state(latest_stock_state):
        st.error("The latest prospective stock state was modified or is unreadable.")
    else:
        stock_state = json.loads(latest_stock_state.read_text())
        lineage_dir = latest_stock_state.parents[1]
        current_lineage = stock_shadow_lineage_dir(STOCK_LINEAGES, STOCK_SHADOW_CONFIG)
        current_policy = lineage_dir == current_lineage
        primary_gate = bool(stock_state["eligible_for_primary_prospective_performance"])
        if primary_gate:
            st.success(
                "Primary prospective data gate passed. This remains a paper decision with no "
                "broker connection or trade authority."
            )
        else:
            st.error(
                "Primary prospective data gate failed. Targets remain diagnostic and must not "
                "be treated as validated performance or an order."
            )
        share_gate = stock_state.get("share_turnover_data_validation", {})
        if isinstance(share_gate, dict) and share_gate.get("passed") is False:
            st.warning(
                "The share-turnover feed failed its separate data gate. The primary consensus "
                "continued, while exact share-turnover arms scheduled fail-safe exits and are "
                "excluded from eligible diagnostic inference."
            )
        if not current_policy:
            st.warning(
                "This is the newest recorded lineage, but its decision-policy/config hash is not "
                "current. A changed trading policy must start a new paper sequence from cash."
            )
        market = stock_state["market_state"]
        primary = stock_state["arms"]["consensus"]
        account = primary.get("paper_account_at_close", {})
        summary_cols = st.columns(6)
        summary_cols[0].metric("As-of close", stock_state["as_of_session"])
        summary_cols[1].metric("Lineage", "CURRENT" if current_policy else "OLDER")
        summary_cols[2].metric("Market regime", "RISK ON" if market["risk_on"] else "RISK OFF")
        summary_cols[3].metric("Next-open names", len(primary["target_for_next_open"]))
        summary_cols[4].metric("Paper equity", _decimal(account.get("total_equity")))
        summary_cols[5].metric(
            "Cumulative costs",
            _decimal(account.get("cumulative_trading_cost")),
        )
        st.caption(
            f"Lineage `{stock_state['lineage_id']}` · state `{latest_stock_state.name}` · "
            "signals observed at the close and effective no earlier than the next regular open."
        )
        st.subheader("Why each prospective action occurred")
        arm_labels = [
            ("consensus", "Primary consensus"),
            ("consensus_market_guard", "Diagnostic market guard"),
        ]
        arm_labels.extend(
            (
                arm_name,
                "Diagnostic classic 12-1 stock momentum control · "
                f"maximum hold {stock_state['arms'][arm_name]['maximum_holding_sessions']} sessions",
            )
            for arm_name in sorted(stock_state["arms"], key=_stock_arm_sort_key)
            if arm_name.startswith("classic_12_1_hold")
        )
        arm_labels.extend(
            (
                arm_name,
                "Diagnostic short-volume · "
                f"maximum hold {stock_state['arms'][arm_name]['maximum_holding_sessions']} sessions",
            )
            for arm_name in sorted(
                stock_state["arms"],
                key=_stock_arm_sort_key,
            )
            if arm_name.startswith("short_volume_hold")
        )
        arm_labels.extend(
            (
                arm_name,
                "Diagnostic academic share turnover · "
                f"maximum hold {stock_state['arms'][arm_name]['maximum_holding_sessions']} sessions",
            )
            for arm_name in sorted(stock_state["arms"], key=_stock_arm_sort_key)
            if arm_name.startswith("share_turnover_hold")
        )
        for arm_name, arm_label in arm_labels:
            arm = stock_state["arms"][arm_name]
            with st.expander(arm_label, expanded=arm_name == "consensus"):
                decisions_frame = pd.DataFrame(arm["decisions"])
                if not decisions_frame.empty:
                    if "selection_rank" not in decisions_frame:
                        decisions_frame["selection_rank"] = decisions_frame.get(
                            "consensus_rank"
                        )
                    decisions_frame["reasons"] = decisions_frame["reasons"].map(
                        lambda values: ", ".join(str(value) for value in values)
                    )
                    decision_columns = [
                        "ticker",
                        "gics_sector",
                        "action",
                        "reasons",
                        "selection_rank",
                        "close",
                        "return_21d",
                        "return_63d",
                        "formation_return_t20_t3",
                        "share_turnover_t20_t3",
                        "return_percentile",
                        "share_turnover_percentile",
                        "atr_fraction_14d",
                        "effective_at",
                    ]
                    st.dataframe(
                        decisions_frame[
                            [
                                column
                                for column in decision_columns
                                if column in decisions_frame
                            ]
                        ],
                        hide_index=True,
                        width="stretch",
                    )
                target = pd.DataFrame(
                    [
                        {"ticker": ticker, "target_weight": weight}
                        for ticker, weight in arm["target_for_next_open"].items()
                    ]
                )
                st.caption(
                    f"Next-open target cash: {float(arm['target_cash_weight']):.1%}; "
                    f"role: {arm['prospective_role']}; signal: "
                    f"{_stock_signal_label(arm.get('signal_family'))}; independently validated: "
                    f"{arm.get('independent_price_validation_applies', False)}; signal data "
                    f"available: {arm.get('signal_data_available_at_close', True)}."
                )
                st.dataframe(target, hide_index=True, width="stretch")

        finra_paths = sorted(
            (lineage_dir / "finra-activity").glob("finra-activity-*.manifest.json")
        )
        if finra_paths:
            finra_path = finra_paths[-1]
            finra_audit = audit_finra_activity_snapshot(finra_path)
            st.subheader("Experimental FINRA activity context")
            if not finra_audit.integrity_passed:
                st.error("The latest FINRA activity diagnostic failed its integrity check.")
            else:
                finra_manifest = json.loads(finra_path.read_text())
                st.caption(
                    "Public FINRA-reported off-exchange short-sale volume. This is not short "
                    "interest, has no bullish/bearish interpretation, and was not used for "
                    "candidate ranking or the portfolio target."
                )
                st.dataframe(
                    pd.DataFrame(finra_manifest["candidate_diagnostics"]),
                    hide_index=True,
                    width="stretch",
                )

        sec_paths = sorted((lineage_dir / "sec-events").glob("sec-events-*.manifest.json"))
        if sec_paths:
            sec_path = sec_paths[-1]
            sec_audit = audit_sec_event_snapshot(sec_path)
            st.subheader("Experimental SEC filing-event context")
            if not sec_audit.integrity_passed:
                st.error("The latest SEC filing-event diagnostic failed its integrity check.")
            else:
                sec_manifest = json.loads(sec_path.read_text())
                st.caption(
                    "Official EDGAR acceptance metadata for the candidate shortlist. Filing "
                    "counts are not sentiment, earnings surprises, or directional signals and "
                    "were not used for candidate ranking or the portfolio target."
                )
                st.dataframe(
                    pd.DataFrame(sec_manifest["candidate_diagnostics"]),
                    hide_index=True,
                    width="stretch",
                )

        evaluation_paths = sorted((lineage_dir / "evaluations").glob("evaluation-*.json"))
        if not evaluation_paths:
            st.info("No prospective stock outcome evaluation is available for this lineage yet.")
        elif not verify_stock_shadow_evaluation(evaluation_paths[-1]):
            st.error("The latest prospective stock evaluation failed its content-hash check.")
        else:
            stock_evaluation = json.loads(evaluation_paths[-1].read_text())
            if stock_state["record_sha256"] not in stock_evaluation["state_record_sha256"]:
                st.error("The latest evaluation does not include the displayed stock state.")
            else:
                eligible_stock = stock_evaluation["eligible_original_gate_only"]
                readiness = stock_evaluation["readiness"]
                performance_cols = st.columns(5)
                performance_cols[0].metric("Eligible sessions", eligible_stock["sessions"])
                performance_cols[1].metric("Completed exits", eligible_stock["completed_exits"])
                performance_cols[2].metric("Risk-off sessions", eligible_stock["risk_off_sessions"])
                performance_cols[3].metric(
                    "Primary net return",
                    _percentage(eligible_stock["primary_consensus"]["cumulative_return"]),
                )
                performance_cols[4].metric(
                    "SPY same-session return",
                    _percentage(eligible_stock["spy_buy_hold"]["cumulative_return"]),
                )
                if readiness["status"] == "insufficient_prospective_evidence":
                    st.warning(
                        "Prospective evidence is not mature: " + ", ".join(readiness["reasons"])
                    )
                else:
                    st.info(
                        "The preregistered monitoring minimum is met, but this is still paper "
                        "evidence and not trading authority."
                    )
                st.json(eligible_stock)
                diagnostic_metrics = stock_evaluation.get("diagnostic_all_sessions", {}).get(
                    "experimental_arms", {}
                )
                if diagnostic_metrics:
                    st.subheader("Unvalidated short-horizon holding experiments")
                    st.caption(
                        "These arms use the same next-open accounting and costs, but Yahoo is "
                        "their only market-data source. They are diagnostic and are excluded from "
                        "the primary eligible-performance record. Exact share-turnover inference "
                        "also excludes transitions whose prior signal-data gate failed."
                    )
                    eligible_diagnostics = stock_evaluation.get(
                        "diagnostic_data_gate_eligible", {}
                    ).get("experimental_arms", {})
                    diagnostic_rows = []
                    for arm_name, values in diagnostic_metrics.items():
                        diagnostic_rows.append(
                            {
                                "arm": arm_name,
                                "eligible_sessions": eligible_diagnostics.get(
                                    arm_name, {}
                                ).get("sessions"),
                                **values,
                            }
                        )
                    st.dataframe(
                        pd.DataFrame(diagnostic_rows),
                        hide_index=True,
                        width="stretch",
                    )
                    comparisons = stock_evaluation.get("diagnostic_arm_comparisons", {})
                    comparison_rows = []
                    for arm_name, arm_comparisons in comparisons.get("per_arm", {}).items():
                        for benchmark, comparison in arm_comparisons.items():
                            comparison_rows.append(
                                {
                                    "arm": arm_name,
                                    "comparison": benchmark,
                                    "status": comparison.get("status"),
                                    "sessions": comparison.get("sessions"),
                                    "annualized_mean_excess": comparison.get(
                                        "observed_annualized_mean_excess_return"
                                    ),
                                    "ci_2_5": comparison.get("ci_2_5"),
                                    "ci_97_5": comparison.get("ci_97_5"),
                                    "positive_resample_probability": comparison.get(
                                        "probability_resampled_mean_excess_is_positive"
                                    ),
                                }
                            )
                    if comparison_rows:
                        st.caption(
                            "Paired inference begins after 21 sessions; the family-wide test "
                            "begins after 63 and corrects for testing all frozen arms."
                        )
                        st.dataframe(
                            pd.DataFrame(comparison_rows),
                            hide_index=True,
                            width="stretch",
                        )
                    family = comparisons.get("family_vs_primary_consensus", {})
                    if family.get("status") == "estimated":
                        st.caption(
                            "Family-wide comparison against the primary consensus; still "
                            "Yahoo-only diagnostic evidence."
                        )
                        st.dataframe(
                            pd.DataFrame(family["variants"]),
                            hide_index=True,
                            width="stretch",
                        )
                    elif family:
                        st.info(
                            "Family-wide inference is not mature: "
                            f"{family.get('sessions', 0)} of "
                            f"{family.get('minimum_sessions', 63)} sessions."
                        )
                return_attribution = stock_evaluation.get("session_return_attribution", {})
                attribution_rows = []
                for arm_name, values in return_attribution.get("arms", {}).items():
                    attribution_rows.append({"arm": arm_name, **values})
                spy_attribution = return_attribution.get("spy_buy_hold")
                if isinstance(spy_attribution, dict):
                    attribution_rows.append({"arm": "SPY buy-and-hold", **spy_attribution})
                if attribution_rows and any(row.get("sessions", 0) for row in attribution_rows):
                    st.subheader("Where returns occurred")
                    st.caption(
                        "Prior close-to-open, explicit open trading-cost drag, and post-cost "
                        "open-to-close returns are linked multiplicatively, not added."
                    )
                    st.dataframe(
                        pd.DataFrame(attribution_rows),
                        hide_index=True,
                        width="stretch",
                    )
                rolling = stock_evaluation.get("rolling_policy_horizons")
                if not isinstance(rolling, dict) or not isinstance(
                    rolling.get("by_horizon"), dict
                ):
                    st.info(
                        "This intact evaluation predates rolling policy horizons; a new "
                        "implementation lineage will add them without rewriting old evidence."
                    )
                else:
                    horizon_rows = []
                    for horizon, horizon_result in rolling["by_horizon"].items():
                        horizon_rows.append(
                            {
                                "sessions": int(horizon),
                                "matured_windows": horizon_result["matured_windows"],
                                "eligible_windows": horizon_result["eligible_windows"],
                                "mean_primary_net_return": horizon_result[
                                    "mean_primary_policy_net_return"
                                ],
                                "mean_spy_return": horizon_result[
                                    "mean_spy_same_window_return"
                                ],
                                "mean_net_excess": horizon_result["mean_net_excess_vs_spy"],
                                "positive_excess_fraction": horizon_result[
                                    "positive_excess_fraction"
                                ],
                            }
                        )
                    st.subheader("Frozen 5/21/63-session policy outcomes")
                    st.dataframe(pd.DataFrame(horizon_rows), hide_index=True, width="stretch")

    st.divider()
    st.subheader("Retrospective stock research")
    stock_manifest_path = STOCK_REPORTS / "manifest.json"
    if not stock_manifest_path.exists():
        st.info(
            "No survivor-free stock research bundle exists yet. The Yahoo prototype failed the "
            "historical coverage gate. Retrospective results stay hidden until a no-paid "
            "point-in-time panel passes it; use the prospective stock shadow in the meantime."
        )
    else:
        stock_audit = audit_stock_research_bundle(STOCK_REPORTS)
        if not stock_audit.integrity_passed:
            st.error(
                "Stock evidence failed integrity or implementation verification. Results are "
                "hidden rather than presented from a stale or modified bundle."
            )
            st.json(stock_audit.to_dict())
        else:
            stock_manifest = json.loads(stock_manifest_path.read_text())
            stock_metrics = pd.read_csv(STOCK_REPORTS / "metrics.csv")
            stock_validation = json.loads(
                (STOCK_REPORTS / "statistical_validation.json").read_text()
            )
            stock_decisions = pd.read_parquet(STOCK_REPORTS / "selected_decisions.parquet")
            stock_capacity = pd.read_csv(STOCK_REPORTS / "capacity.csv")
            stock_regimes = pd.read_csv(STOCK_REPORTS / "hostile_regimes.csv")
            stock_concentration = pd.read_csv(STOCK_REPORTS / "asset_concentration.csv")
            selected_stock = stock_manifest["selected_variant"]
            sealed = stock_metrics.loc[
                (stock_metrics["strategy"] == selected_stock)
                & (stock_metrics["split"] == "sealed_test")
            ]
            family = stock_validation["selection_family"]
            pbo = stock_validation["approximate_combinatorial_pbo"]
            walk_forward = stock_validation["walk_forward_vs_spy"]
            sealed_concentration = stock_validation["selected_variant_asset_concentration"][
                "sealed_test"
            ]

            cols = st.columns(5)
            cols[0].metric("Evidence integrity", "PASS")
            cols[1].metric("Selected variant", selected_stock)
            cols[2].metric(
                "Sealed-test CAGR",
                f"{float(sealed.iloc[0]['cagr']):.1%}" if not sealed.empty else "N/A",
            )
            pbo_value = pbo.get("probability_selected_variant_below_oos_median")
            cols[3].metric(
                "Approximate PBO",
                f"{float(pbo_value):.1%}" if pbo_value is not None else "N/A",
            )
            cols[4].metric(
                "Walk-forward P(excess > 0)",
                f"{float(walk_forward['probability_resampled_mean_excess_is_positive']):.1%}",
            )
            st.warning(
                "This is retrospective, research-only evidence. It is not today's stock "
                "recommendation and cannot authorize an order."
            )

            st.subheader("Why each modeled action occurred")
            latest_date = pd.Timestamp(stock_decisions["date"].max())
            latest = stock_decisions.loc[stock_decisions["date"] == latest_date].copy()
            latest["reasons"] = latest["reasons"].map(
                lambda values: (
                    values if isinstance(values, str) else ", ".join(str(value) for value in values)
                )
            )
            st.caption(
                f"Latest retrospective close in this bundle: {latest_date.date()}; modeled "
                "execution is no earlier than the next regular-session open."
            )
            st.dataframe(
                latest[
                    [
                        "ticker",
                        "action",
                        "reasons",
                        "rank",
                        "score",
                        "close",
                        "entry_price",
                        "high_watermark",
                        "holding_sessions",
                    ]
                ],
                hide_index=True,
                width="stretch",
            )

            st.subheader("Validation and fragility")
            validation_cols = st.columns(4)
            validation_cols[0].metric(
                "Family-wide p-value",
                f"{float(family['family_wide_best_variant_p_value']):.3f}",
            )
            validation_cols[1].metric("BY-FDR discoveries", int(family["fdr_discoveries"]))
            validation_cols[2].metric(
                "Largest sealed winner",
                sealed_concentration["largest_positive_contributor"] or "none",
            )
            validation_cols[3].metric(
                "Top-5 positive P&L share",
                f"{float(sealed_concentration['top_5_positive_pnl_share']):.1%}",
            )
            st.dataframe(
                stock_metrics.loc[
                    stock_metrics["strategy"].isin(
                        [selected_stock, *stock_manifest["required_comparators"]]
                    )
                ],
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "Ticker concentration is measured from actual held weights. Current sector or "
                "AI labels are not projected backward into historical claims."
            )
            st.dataframe(
                stock_concentration.loc[stock_concentration["split"] == "sealed_test"].head(20),
                hide_index=True,
                width="stretch",
            )
            st.subheader("Hostile regimes and execution capacity")
            st.dataframe(stock_regimes, hide_index=True, width="stretch")
            st.dataframe(stock_capacity, hide_index=True, width="stretch")
            if stock_audit.warnings:
                st.json(stock_audit.to_dict())

with methods:
    st.subheader("Decision data gate")
    st.json(data_quality)
    st.subheader("Validation warning")
    pbo = validation["approximate_pbo"]["probability_selected_variant_below_oos_median"]
    bootstrap = validation["paired_block_bootstrap"]
    st.warning(
        f"Approximate PBO is {pbo:.1%}. The paired block bootstrap estimates only a "
        f"{bootstrap['probability_champion_mean_return_exceeds_spy']:.1%} chance that "
        "historical mean return exceeded SPY; the 95% interval crosses zero."
    )
    st.json(validation)
    st.subheader("Universe and technology-concentration sensitivity")
    st.json(validation["universe_sensitivity"])
    st.subheader("Prospective shadow evaluation")
    if prospective is None:
        st.info("No prospective outcome evaluation has been generated yet.")
    else:
        eligible = prospective["eligible_summary"]
        st.caption(
            f"{prospective['unique_schema_v3_decisions']} unique frozen decisions; "
            f"{eligible['eligible_unique_decisions']} passed their original data gate."
        )
        st.json(eligible)
    st.json(manifest)
    if shadow_files:
        st.caption(f"Latest locked prospective record: {shadow_files[-1].name}")
    st.markdown(
        "The candidate was specified from published momentum and volatility-management priors. "
        "Retrospective performance does not authorize live trading; prospective shadow evidence is still required."
    )
