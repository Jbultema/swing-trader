from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from swing_trader.backtest import (
    BacktestResult,
    buy_and_hold_weights,
    classic_dual_momentum_weights,
    guarded_dual_momentum_weights,
    month_end_mask,
    moving_average_weights,
    panic_guarded_dual_momentum_weights,
    run_backtest,
)
from swing_trader.config import AppConfig
from swing_trader.metrics import performance_metrics, regime_metrics
from swing_trader.strategy import StrategyRun, build_strategy
from swing_trader.validation import build_validation_artifacts


def run_research(
    prices: pd.DataFrame,
    config: AppConfig,
    output_dir: Path,
) -> dict[str, BacktestResult]:
    close = prices["Close"]
    adjusted_open = prices["Open"]
    candidate = build_strategy(prices, config.strategy, config.data.benchmark)
    champion_weights = classic_dual_momentum_weights(
        close, config.data.tickers, config.strategy.top_n
    )
    comparisons = {
        "swing_momentum_v2": candidate.target_weights,
        "buy_hold_spy": buy_and_hold_weights(close, config.data.benchmark),
        "spy_200d_trend": moving_average_weights(close, config.data.benchmark, 200),
        "classic_12m_dual_momentum": champion_weights,
        "dual_momentum_asset_trend_exit": guarded_dual_momentum_weights(
            close,
            config.data.tickers,
            benchmark=config.data.benchmark,
            top_n=config.strategy.top_n,
            individual_trend_days=200,
        ),
        "dual_momentum_market_guard": guarded_dual_momentum_weights(
            close,
            config.data.tickers,
            benchmark=config.data.benchmark,
            top_n=config.strategy.top_n,
            market_trend_days=200,
        ),
        "dual_momentum_full_guard": guarded_dual_momentum_weights(
            close,
            config.data.tickers,
            benchmark=config.data.benchmark,
            top_n=config.strategy.top_n,
            individual_trend_days=200,
            market_trend_days=200,
        ),
        "dual_momentum_panic_guard": panic_guarded_dual_momentum_weights(
            close,
            config.data.tickers,
            benchmark=config.data.benchmark,
            top_n=config.strategy.top_n,
            volatility_days=config.strategy.volatility_days,
            stress_volatility=config.strategy.stress_volatility,
            stress_drawdown=config.strategy.stress_drawdown,
        ),
    }
    results = {
        name: run_backtest(name, adjusted_open, weights, config.execution)
        for name, weights in comparisons.items()
    }
    _write_artifacts(prices, candidate, champion_weights, results, config, output_dir)
    return results


def _write_artifacts(
    prices: pd.DataFrame,
    candidate: StrategyRun,
    champion_weights: pd.DataFrame,
    results: dict[str, BacktestResult],
    config: AppConfig,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_rows: list[dict[str, object]] = []
    evaluation_start = prices.index[config.validation.minimum_history_days]
    split_ranges = {
        "full": (None, None),
        "development": (None, config.validation.development_end),
        "validation": (config.validation.validation_start, config.validation.validation_end),
        "recent_diagnostic": (config.validation.recent_diagnostic_start, None),
    }
    for name, result in results.items():
        for split, (start, end) in split_ranges.items():
            effective_start = max(pd.Timestamp(start), evaluation_start) if start else evaluation_start
            returns = result.returns.loc[effective_start:end]
            if len(returns.dropna()) < 2:
                continue
            row = performance_metrics(
                returns,
                result.equity.loc[effective_start:end],
                result.turnover.loc[effective_start:end],
                result.transaction_costs.loc[effective_start:end],
            )
            metrics_rows.append({"strategy": name, "split": split, **row})
    pd.DataFrame(metrics_rows).to_csv(output_dir / "metrics.csv", index=False)

    equity = pd.concat({name: result.equity for name, result in results.items()}, axis=1)
    equity.to_csv(output_dir / "equity.csv", index_label="date")
    champion_weights.to_csv(output_dir / "target_weights.csv", index_label="date")
    results["classic_12m_dual_momentum"].weights.to_csv(
        output_dir / "executed_weights.csv", index_label="date"
    )
    candidate.market_state.to_csv(output_dir / "market_state.csv", index_label="date")
    candidate.evidence.to_parquet(output_dir / "evidence.parquet", index=False)

    regimes = []
    for name, result in results.items():
        frame = regime_metrics(result.returns, result.equity).reset_index()
        frame.insert(0, "strategy", name)
        regimes.append(frame)
    pd.concat(regimes, ignore_index=True).to_csv(output_dir / "regime_metrics.csv", index=False)

    decisions = latest_champion_decisions(
        prices["Close"],
        champion_weights,
        results["classic_12m_dual_momentum"],
        config,
    )
    (output_dir / "latest_decisions.json").write_text(
        json.dumps(decisions, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "system": "robust_dual_momentum_v1",
        "champion": "classic_12m_dual_momentum",
        "capital_preservation_comparator": "dual_momentum_panic_guard",
        "rejected_fast_candidates": ["swing_momentum_v1", "swing_momentum_v2"],
        "research_status": "retrospective_candidate_not_live_approved",
        "execution_model": "close signal; next-session adjusted-open rebalance; open-to-open P&L",
        "tax_model": "ignored_retirement_account_first_pass",
        "parameter_source": "published_prior; v2 corrects v1 daily-resizing schedule defect",
        "holdout_status": "diagnostic_only_after_v1_result_was_opened",
        "automatic_order_placement": False,
        "data_start": str(prices.index.min().date()),
        "data_end": str(prices.index.max().date()),
        "config": {
            "data": asdict(config.data),
            "strategy": asdict(config.strategy),
            "execution": asdict(config.execution),
            "validation": asdict(config.validation),
        },
    }
    payload = json.dumps(manifest, sort_keys=True, default=list).encode()
    manifest["specification_sha256"] = hashlib.sha256(payload).hexdigest()
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    build_validation_artifacts(prices, config, results, output_dir)


def latest_decisions(
    strategy: StrategyRun,
    result: BacktestResult,
    config: AppConfig,
) -> dict[str, object]:
    latest_date = strategy.target_weights.dropna(how="all").index[-1]
    latest = strategy.target_weights.loc[latest_date]
    prior = result.weights.iloc[-1].reindex(latest.index).fillna(0.0)
    evidence = strategy.evidence.loc[
        (strategy.evidence["date"] == latest_date)
        & ((strategy.evidence["target_weight"] > 0) | (prior.to_dict() != {}))
    ].copy()
    rows = []
    for ticker in latest.index:
        target = float(latest[ticker])
        current = float(prior[ticker])
        change = target - current
        if abs(change) < config.execution.minimum_trade_weight and target == 0 and current == 0:
            continue
        match = evidence.loc[evidence["ticker"] == ticker]
        detail = match.iloc[0].to_dict() if not match.empty else {}
        action = "HOLD"
        if change >= config.execution.minimum_trade_weight:
            action = "BUY"
        elif change <= -config.execution.minimum_trade_weight:
            action = "SELL"
        rows.append(
            {
                "ticker": ticker,
                "action": action,
                "current_weight": current,
                "target_weight": target,
                "change_weight": change,
                "why": _why(action, detail),
                "score": _json_number(detail.get("score")),
                "rank": _json_number(detail.get("rank")),
                "volatility": _json_number(detail.get("volatility")),
                "exit_or_hold_reason": detail.get("decision_reason", "no_signal"),
            }
        )
    state = strategy.market_state.loc[latest_date]
    return {
        "as_of_close": str(latest_date.date()),
        "earliest_execution": "next regular session open",
        "mode": "research_only_human_execution_required",
        "market": {
            "risk_on": bool(state["risk_on"]),
            "stress": bool(state["stress"]),
            "benchmark_drawdown": _json_number(state["benchmark_drawdown"]),
            "benchmark_volatility": _json_number(state["benchmark_volatility"]),
            "gross_exposure_cap": _json_number(state["gross_exposure_cap"]),
        },
        "actions": rows,
    }


def latest_champion_decisions(
    close: pd.DataFrame,
    target_weights: pd.DataFrame,
    result: BacktestResult,
    config: AppConfig,
) -> dict[str, object]:
    latest_date = target_weights.index[-1]
    decision_dates = close.index[month_end_mask(close.index)]
    last_decision = decision_dates[decision_dates <= latest_date][-1]
    momentum = close.div(close.shift(252)).sub(1.0)
    ranks = momentum.rank(axis=1, ascending=False, method="first")
    target = target_weights.loc[latest_date]
    current = result.weights.iloc[-1].reindex(target.index).fillna(0.0)
    actions = []
    for ticker in target.index:
        target_value = float(target[ticker])
        current_value = float(current[ticker])
        change = target_value - current_value
        if target_value == 0.0 and current_value == 0.0:
            continue
        action = "HOLD"
        if change >= config.execution.minimum_trade_weight:
            action = "BUY"
        elif change <= -config.execution.minimum_trade_weight:
            action = "SELL"
        value = float(momentum.loc[last_decision, ticker])
        rank = float(ranks.loc[last_decision, ticker])
        why = (
            f"{ticker} ranked {rank:.0f} on positive trailing-12-month momentum "
            f"({value:.1%}) at the {last_decision.date()} monthly decision."
        )
        actions.append(
            {
                "ticker": ticker,
                "action": action,
                "current_weight": current_value,
                "target_weight": target_value,
                "change_weight": change,
                "why": why,
                "momentum_12m": value,
                "rank": rank,
                "exit_rule": "Exit at month-end if momentum is non-positive or rank falls outside top three.",
            }
        )
    next_review = (latest_date + pd.offsets.BMonthEnd(0)).date()
    return {
        "as_of_close": str(latest_date.date()),
        "last_monthly_decision": str(last_decision.date()),
        "next_scheduled_review_estimate": str(next_review),
        "earliest_execution": "next regular session open after a scheduled decision",
        "mode": "research_only_human_execution_required",
        "market": {
            "risk_on": True,
            "stress": False,
            "gross_exposure_cap": float(target.sum()),
            "note": "Champion uses asset-level absolute momentum; panic guard is a separate comparator.",
        },
        "actions": actions,
    }


def _why(action: str, detail: dict[str, object]) -> str:
    reason = str(detail.get("decision_reason", "no signal"))
    if action == "BUY":
        return "Selected by multi-horizon momentum rank; absolute trends and trailing-risk gate pass."
    if action == "SELL":
        return f"Target removed by protective or ranking rule: {reason.replace('_', ' ')}."
    return f"No material rebalance; state is {reason.replace('_', ' ')}."


def _json_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None
