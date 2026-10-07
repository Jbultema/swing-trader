from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports/latest"

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

status, as_of, exposure = st.columns(3)
status.metric("Status", "RESEARCH ONLY")
as_of.metric("Signals as of", decisions["as_of_close"])
exposure.metric("Gross exposure cap", f"{decisions['market']['gross_exposure_cap']:.0%}")

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
            ["strategy", "split", "cagr", "annualized_volatility", "sharpe_zero_rf", "max_drawdown", "calmar"]
        ],
        hide_index=True,
        width="stretch",
    )

with action:
    st.subheader("Champion: monthly long-only dual momentum")
    st.caption(f"Last scheduled decision: {decisions['last_monthly_decision']}")
    st.dataframe(pd.DataFrame(decisions["actions"]), hide_index=True, width="stretch")
    st.info("These are auditable decision-support tickets, not orders. Review prices and account restrictions manually.")

with risk:
    st.subheader("Known hostile regimes")
    st.dataframe(regimes, hide_index=True, width="stretch")
    st.json(decisions["market"])
    st.warning("Stops reduce modeled exposure after observed damage; they cannot prevent overnight gaps or guarantee an execution price.")

with methods:
    st.json(manifest)
    st.markdown(
        "The candidate was specified from published momentum and volatility-management priors. "
        "Retrospective performance does not authorize live trading; prospective shadow evidence is still required."
    )
