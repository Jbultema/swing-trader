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
    long_only_time_series_momentum_weights,
    month_end_mask,
    moving_average_weights,
    panic_guarded_dual_momentum_weights,
    run_backtest,
)
from swing_trader.config import AppConfig
from swing_trader.metrics import performance_metrics, regime_metrics
from swing_trader.provenance import implementation_sha256
from swing_trader.strategy import StrategyRun, build_strategy
from swing_trader.validation import build_validation_artifacts


def run_research(
    prices: pd.DataFrame,
    config: AppConfig,
    output_dir: Path,
    *,
    data_quality: dict[str, object] | None = None,
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
        "long_only_time_series_momentum": long_only_time_series_momentum_weights(
            close,
            config.data.tickers,
        ),
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
    _write_artifacts(
        prices,
        candidate,
        champion_weights,
        comparisons["dual_momentum_panic_guard"],
        results,
        config,
        output_dir,
        data_quality=data_quality,
    )
    return results


def _write_artifacts(
    prices: pd.DataFrame,
    candidate: StrategyRun,
    champion_weights: pd.DataFrame,
    panic_weights: pd.DataFrame,
    results: dict[str, BacktestResult],
    config: AppConfig,
    output_dir: Path,
    *,
    data_quality: dict[str, object] | None,
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
            effective_start = (
                max(pd.Timestamp(start), evaluation_start) if start else evaluation_start
            )
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

    quality = data_quality or {
        "status": "missing_data_quality_artifact",
        "decision_data_gate_passed": False,
    }
    (output_dir / "data_quality.json").write_text(
        json.dumps(quality, indent=2) + "\n", encoding="utf-8"
    )
    decisions = latest_champion_decisions(
        prices["Close"],
        champion_weights,
        panic_weights,
        results["classic_12m_dual_momentum"],
        config,
        data_quality=quality,
    )
    (output_dir / "latest_decisions.json").write_text(
        json.dumps(decisions, indent=2) + "\n", encoding="utf-8"
    )
    specification = {
        "system": "robust_dual_momentum_v1",
        "champion": "classic_12m_dual_momentum",
        "capital_preservation_comparator": "dual_momentum_panic_guard",
        "rejected_fast_candidates": ["swing_momentum_v1", "swing_momentum_v2"],
        "execution_model": "close signal; next-session adjusted-open rebalance; open-to-open P&L",
        "tax_model": "ignored_retirement_account_first_pass",
        "parameter_source": "published_prior; v2 corrects v1 daily-resizing schedule defect",
        "config": {
            "data": asdict(config.data),
            "strategy": asdict(config.strategy),
            "execution": asdict(config.execution),
            "validation": asdict(config.validation),
        },
    }
    specification_payload = json.dumps(specification, sort_keys=True, default=list).encode()
    manifest = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        **specification,
        "research_status": "retrospective_candidate_not_live_approved",
        "holdout_status": "diagnostic_only_after_v1_result_was_opened",
        "automatic_order_placement": False,
        "decision_data_gate_passed": bool(quality.get("decision_data_gate_passed", False)),
        "data_quality_status": quality.get("status", "unknown"),
        "data_start": str(prices.index.min().date()),
        "data_end": str(prices.index.max().date()),
        "specification": specification,
        "specification_sha256": hashlib.sha256(specification_payload).hexdigest(),
        "implementation_sha256": implementation_sha256(),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    build_validation_artifacts(prices, config, results, output_dir)


def latest_champion_decisions(
    close: pd.DataFrame,
    target_weights: pd.DataFrame,
    panic_weights: pd.DataFrame,
    result: BacktestResult,
    config: AppConfig,
    *,
    data_quality: dict[str, object],
) -> dict[str, object]:
    latest_date = target_weights.index[-1]
    decision_dates = close.index[month_end_mask(close.index)]
    last_decision = decision_dates[decision_dates <= latest_date][-1]
    momentum = close.div(close.shift(252)).sub(1.0)
    ranks = momentum.rank(axis=1, ascending=False, method="first")
    target = target_weights.loc[latest_date]
    panic_target = panic_weights.loc[latest_date]
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
        why = _decision_reason(
            ticker=ticker,
            target_weight=target_value,
            current_weight=current_value,
            momentum=value,
            rank=rank,
            top_n=config.strategy.top_n,
            decision_date=str(last_decision.date()),
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
                "exit_rule": (
                    "Exit at month-end if momentum is non-positive or rank falls outside "
                    f"the top {config.strategy.top_n}."
                ),
            }
        )
    next_review = (latest_date + pd.offsets.BMonthEnd(0)).date()
    benchmark = close[config.data.benchmark]
    benchmark_returns = benchmark.pct_change(fill_method=None)
    benchmark_volatility = float(
        benchmark_returns.rolling(config.strategy.volatility_days).std().iloc[-1] * (252**0.5)
    )
    benchmark_drawdown = float(
        benchmark.iloc[-1] / benchmark.rolling(config.strategy.trend_days).max().iloc[-1] - 1.0
    )
    panic_triggered = float(panic_target.sum()) < float(target.sum()) - 1e-12
    gate_passed = bool(data_quality.get("decision_data_gate_passed", False))
    return {
        "as_of_close": str(latest_date.date()),
        "last_monthly_decision": str(last_decision.date()),
        "next_scheduled_review_estimate": str(next_review),
        "earliest_execution": "next regular session open after a scheduled decision",
        "mode": (
            "research_only_human_execution_required"
            if gate_passed
            else "research_only_data_unreconciled_no_action"
        ),
        "action_authorized": False,
        "data_reconciled": gate_passed,
        "data_quality_status": data_quality.get("status", "unknown"),
        "market": {
            "allocation_gate": "positive_asset_level_12_month_momentum",
            "gross_exposure_cap": float(target.sum()),
            "note": "Champion uses asset-level absolute momentum; panic guard is a separate comparator.",
        },
        "capital_preservation_overlay": {
            "triggered": panic_triggered,
            "hypothetical_action": "EXIT_TO_CASH" if panic_triggered else "HOLD",
            "target_gross_exposure": float(panic_target.sum()),
            "benchmark_volatility": benchmark_volatility,
            "volatility_trigger": config.strategy.stress_volatility,
            "benchmark_drawdown_from_200d_high": benchmark_drawdown,
            "drawdown_trigger": config.strategy.stress_drawdown,
            "authority": "comparator_only_not_champion",
        },
        "hypothetical_actions": actions,
    }


def _decision_reason(
    *,
    ticker: str,
    target_weight: float,
    current_weight: float,
    momentum: float,
    rank: float,
    top_n: int,
    decision_date: str,
) -> str:
    if target_weight > 0.0:
        return (
            f"{ticker} ranked {rank:.0f} with positive trailing-12-month momentum "
            f"({momentum:.1%}) at the {decision_date} monthly decision."
        )
    if current_weight > 0.0 and (pd.isna(momentum) or momentum <= 0.0):
        value = "unavailable" if pd.isna(momentum) else f"{momentum:.1%}"
        return (
            f"Exit {ticker}: trailing-12-month momentum was non-positive or unavailable "
            f"({value}) at the {decision_date} monthly decision."
        )
    if current_weight > 0.0:
        return (
            f"Exit {ticker}: rank {rank:.0f} was outside the top {top_n} at the "
            f"{decision_date} monthly decision despite {momentum:.1%} momentum."
        )
    return f"{ticker} has zero current and target weight; no action is required."
