from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports/latest"
SHADOW = ROOT / "reports/shadow"

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

overview, action, risk, methods = st.tabs(["Performance", "Next action", "Risk", "Methods"])

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
