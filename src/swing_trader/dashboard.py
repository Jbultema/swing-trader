from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from swing_trader.stock_audit import audit_stock_research_bundle

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports/latest"
SHADOW = ROOT / "reports/shadow"
STOCK_REPORTS = ROOT / "reports/stock/latest"

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
    stock_manifest_path = STOCK_REPORTS / "manifest.json"
    if not stock_manifest_path.exists():
        st.info(
            "No survivor-free stock research bundle exists yet. The Yahoo prototype failed the "
            "coverage gate; run the preregistered pipeline only after importing licensed "
            "point-in-time data."
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
