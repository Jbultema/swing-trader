"""Native monthly ETF-control diagnostics; independent of the stock shadow."""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from swing_trader.backtest import classic_dual_momentum_weights, month_end_mask
from swing_trader.config import load_config
from swing_trader.dashboard_models import latest_stock_dashboard_state, stock_decision_headline


def signals(prices, config):
    momentum = prices[list(config.data.tickers)].div(prices[list(config.data.tickers)].shift(252)).sub(1)
    targets = classic_dual_momentum_weights(prices, config.data.tickers, config.strategy.top_n)
    result = pd.DataFrame(index=prices.index)
    result["scheduled"] = month_end_mask(prices.index)
    result["positive_momentum_share"] = momentum.gt(0).mean(axis=1).where(momentum.notna().all(axis=1))
    result["target_exposure"] = targets.sum(axis=1)
    result["reason"] = "monthly_schedule_holding_prior_target"
    result.loc[result.scheduled, "reason"] = "monthly_positive_12m_momentum_selection"
    for ticker in momentum:
        result[f"momentum_{ticker}"] = momentum[ticker]
    for ticker in targets:
        result[f"target_{ticker}"] = targets[ticker]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cutoff")
    args = parser.parse_args()
    config = load_config("config/default.toml")
    prices = pd.read_parquet(args.bundle / "prices.parquet").loc[:args.cutoff]
    args.output.mkdir(parents=True, exist_ok=False)
    signals(prices, config).to_parquet(args.output / "signals.parquet")
    state = latest_stock_dashboard_state(Path("reports/stock-shadow/lineages"), Path("config/stock_shadow.toml"))
    context = {"status": "missing", "action_authorized": False}
    if state is not None:
        context = {"as_of_session": state.payload.get("as_of_session"),
                   "recorded_at_utc": state.payload.get("recorded_at_utc"),
                   "integrity_passed": state.integrity_passed, "current_policy": state.current_policy,
                   "headline": stock_decision_headline(state), "action_authorized": False,
                   "source_sha256": hashlib.sha256(state.path.read_bytes()).hexdigest()}
    (args.output / "stock_context.json").write_text(json.dumps(context, indent=2) + "\n")
    (args.output / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "system": "swing-trader",
        "signals_sha256": hashlib.sha256((args.output / "signals.parquet").read_bytes()).hexdigest(),
        "bundle_sha256": hashlib.sha256((args.bundle / "manifest.json").read_bytes()).hexdigest(),
        "semantics": "frozen_monthly_etf_control_not_stock_shadow",
        "driver_columns": ["scheduled", "positive_momentum_share"],
        "monitor_columns": [],
        "rules": {"momentum_lookback_sessions": 252, "rebalance": "planned_month_end", "top_n": config.strategy.top_n, "absolute_momentum_gate": 0},
        "context_artifacts": {"stock_context.json": hashlib.sha256((args.output / "stock_context.json").read_bytes()).hexdigest()},
        "limitations": ["Monthly 12-month momentum control, not the faster stock-shadow strategy.", "Targets are not actual holdings or executions."],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
