from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from swing_trader.backtest import (
    BacktestResult,
    UnmodeledExecutionError,
    buy_and_hold_weights,
    run_backtest,
)
from swing_trader.config import ExecutionConfig
from swing_trader.execution import analyze_execution_capacity
from swing_trader.metrics import performance_metrics, regime_metrics
from swing_trader.provenance import file_sha256, implementation_sha256, tabular_sha256
from swing_trader.stock_comparators import (
    classic_stock_momentum_weights,
    point_in_time_equal_weight_weights,
)
from swing_trader.stock_config import StockExperimentConfig
from swing_trader.stock_data import StockCoverageAudit, audit_stock_coverage
from swing_trader.stock_signals import (
    StockFeatureConfig,
    build_stock_features,
    exit_policy_for_family,
)
from swing_trader.stock_strategy import StockStrategyPlan, build_stock_strategy_plan
from swing_trader.stock_validation import (
    WalkForwardSelection,
    approximate_combinatorial_pbo,
    asset_pnl_concentration,
    expanding_walk_forward_selection,
    median_annual_excess_return,
    paired_stationary_bootstrap,
    stationary_bootstrap_family_validation,
)


@dataclass(frozen=True)
class StockResearchRun:
    results: dict[str, BacktestResult]
    plans: dict[str, StockStrategyPlan]
    coverage: StockCoverageAudit
    selected_variant: str
    walk_forward: WalkForwardSelection
    failures: pd.DataFrame


def run_stock_research(
    ohlcv: pd.DataFrame,
    membership: pd.DataFrame,
    benchmark: pd.DataFrame,
    cash_returns: pd.Series,
    config: StockExperimentConfig,
    output_dir: Path,
    *,
    terminal_return_overrides: pd.DataFrame | None = None,
) -> StockResearchRun:
    """Run the preregistered stock family without silently dropping invalid paths."""
    input_fingerprints = {
        "ohlcv": tabular_sha256(ohlcv),
        "membership": tabular_sha256(membership),
        "benchmark": tabular_sha256(benchmark),
        "cash_returns": tabular_sha256(cash_returns),
        "terminal_return_overrides": (
            tabular_sha256(terminal_return_overrides)
            if terminal_return_overrides is not None
            else None
        ),
    }
    close = ohlcv["Close"].astype(float)
    opens = ohlcv["Open"].astype(float)
    volume = ohlcv["Volume"].astype(float)
    research_membership = membership.reindex(index=close.index, columns=close.columns).fillna(False)
    floor = pd.Timestamp(config.universe.membership_floor)
    research_membership.loc[research_membership.index < floor] = False
    coverage = audit_stock_coverage(
        close,
        research_membership.loc[floor:],
        minimum_history_sessions=config.signals.formation_days,
    )
    if coverage.status != "passed":
        raise ValueError(
            "Stock coverage gate failed; performance generation is prohibited: "
            f"{coverage.to_dict()}"
        )

    features = build_stock_features(
        ohlcv,
        research_membership,
        StockFeatureConfig(
            formation_days=config.signals.formation_days,
            skip_days=config.signals.skip_days,
            minimum_price=config.universe.minimum_price,
            minimum_median_dollar_volume=config.universe.minimum_median_dollar_volume,
        ),
    )
    plans, registry = _build_variant_plans(features, opens, config)
    cash_returns = cash_returns.reindex(opens.index)
    primary_round_trip_bps = float(np.median(config.execution.round_trip_cost_bps))
    stock_execution = ExecutionConfig(
        initial_capital=config.execution.initial_capital,
        transaction_cost_bps=primary_round_trip_bps / 2.0,
        minimum_trade_weight=0.0,
    )

    results: dict[str, BacktestResult] = {}
    failure_rows: list[dict[str, str]] = []
    capacity_rows: list[dict[str, object]] = []
    for name, plan in plans.items():
        try:
            result = run_backtest(
                name,
                opens,
                plan.target_weights,
                stock_execution,
                cash_returns=cash_returns,
                terminal_return_overrides=terminal_return_overrides,
            )
        except UnmodeledExecutionError as exc:
            failure_rows.append({"variant": name, "failure": str(exc)})
            continue
        results[name] = result
        capacity = analyze_execution_capacity(
            result,
            close,
            volume,
            initial_capital=config.execution.initial_capital,
            adv_sessions=config.execution.adv_sessions,
            maximum_adv_participation=config.execution.maximum_adv_participation,
        )
        capacity_rows.append({"variant": name, **capacity.report.to_dict()})

    if not results:
        raise ValueError("Every stock variant failed the execution/corporate-action gate.")

    comparator_results = _run_comparators(
        opens,
        close,
        research_membership,
        benchmark,
        cash_returns,
        stock_execution,
        config,
        terminal_return_overrides,
    )
    results.update(comparator_results)
    passing_capacity = {str(row["variant"]) for row in capacity_rows if row["status"] == "passed"}
    variant_names = sorted(set(plans) & set(results) & passing_capacity)
    if not variant_names:
        raise ValueError("No stock variant passed the execution-capacity gate.")
    variant_returns = pd.concat(
        {name: results[name].returns for name in variant_names}, axis=1
    ).loc[floor:]
    benchmark_returns = results["spy"].returns.reindex(variant_returns.index)
    selection_returns = variant_returns.loc[: config.validation.selection_end]
    selection_benchmark = benchmark_returns.loc[selection_returns.index]
    selection_scores = median_annual_excess_return(selection_returns, selection_benchmark)
    selected_variant = str(selection_scores.sort_index().idxmax())

    final_year = int(variant_returns.index.max().year)
    walk_forward = expanding_walk_forward_selection(
        variant_returns,
        benchmark_returns,
        first_test_year=pd.Timestamp(config.validation.validation_start).year,
        last_test_year=final_year,
        minimum_train_sessions=5 * 252,
    )
    failures = pd.DataFrame(failure_rows, columns=["variant", "failure"])
    _write_stock_artifacts(
        output_dir,
        config,
        coverage,
        results,
        plans,
        registry,
        pd.DataFrame(capacity_rows),
        failures,
        selection_scores,
        selected_variant,
        walk_forward,
        opens,
        cash_returns,
        terminal_return_overrides,
        input_fingerprints,
    )
    return StockResearchRun(
        results=results,
        plans=plans,
        coverage=coverage,
        selected_variant=selected_variant,
        walk_forward=walk_forward,
        failures=failures,
    )


def _build_variant_plans(
    features: pd.DataFrame,
    opens: pd.DataFrame,
    config: StockExperimentConfig,
) -> tuple[dict[str, StockStrategyPlan], pd.DataFrame]:
    plans: dict[str, StockStrategyPlan] = {}
    registry_rows: list[dict[str, object]] = []
    base_atr = min(config.exits.atr_multiples, key=lambda value: abs(value - 3.0))
    specifications: list[tuple[str, int, float]] = []
    for holding in config.signals.holding_sessions:
        specifications.append(("combined", holding, base_atr))
    for exit_family in config.exits.families:
        if exit_family != "combined":
            specifications.append((exit_family, config.exits.maximum_holding_sessions, base_atr))
    for atr in config.exits.atr_multiples:
        if atr != base_atr:
            specifications.append(("combined", config.exits.maximum_holding_sessions, atr))

    for signal_family in config.signals.families:
        for exit_family, holding, atr in specifications:
            name = f"{signal_family}__{exit_family}__hold{holding}__atr{atr:g}"
            policy = exit_policy_for_family(
                exit_family,
                hard_stop_fraction=config.exits.hard_stop_fraction,
                atr_multiple=atr,
                maximum_holding_sessions=holding,
                rank_exit_multiple=config.exits.rank_exit_multiple,
            )
            plans[name] = build_stock_strategy_plan(
                features,
                opens,
                signal_family,
                maximum_positions=config.execution.maximum_positions,
                maximum_position_weight=config.execution.maximum_position_weight,
                policy=policy,
            )
            registry_rows.append(
                {
                    "variant": name,
                    "signal_family": signal_family,
                    "exit_family": exit_family,
                    "maximum_holding_sessions": holding,
                    "atr_multiple": atr,
                }
            )
    return plans, pd.DataFrame(registry_rows)


def _run_comparators(
    opens: pd.DataFrame,
    close: pd.DataFrame,
    membership: pd.DataFrame,
    benchmark: pd.DataFrame,
    cash_returns: pd.Series,
    execution: ExecutionConfig,
    config: StockExperimentConfig,
    terminal_returns: pd.DataFrame | None,
) -> dict[str, BacktestResult]:
    equal_weight = point_in_time_equal_weight_weights(membership)
    classic = classic_stock_momentum_weights(
        close,
        membership,
        top_n=config.execution.maximum_positions,
        formation_days=config.signals.formation_days,
        skip_days=config.signals.skip_days,
    )
    results = {
        "equal_weight_universe": run_backtest(
            "equal_weight_universe",
            opens,
            equal_weight,
            execution,
            cash_returns=cash_returns,
            terminal_return_overrides=terminal_returns,
        ),
        "classic_12_1_momentum": run_backtest(
            "classic_12_1_momentum",
            opens,
            classic,
            execution,
            cash_returns=cash_returns,
            terminal_return_overrides=terminal_returns,
        ),
    }
    market = config.benchmarks.market
    benchmark_open = benchmark["Open"].reindex(opens.index).rename(market).to_frame()
    benchmark_close = benchmark["Close"].reindex(opens.index).rename(market).to_frame()
    results["spy"] = run_backtest(
        "spy",
        benchmark_open,
        buy_and_hold_weights(benchmark_close, market),
        ExecutionConfig(
            initial_capital=execution.initial_capital,
            transaction_cost_bps=1.0,
            minimum_trade_weight=0.0,
        ),
    )
    return results


def _write_stock_artifacts(
    output_dir: Path,
    config: StockExperimentConfig,
    coverage: StockCoverageAudit,
    results: dict[str, BacktestResult],
    plans: dict[str, StockStrategyPlan],
    registry: pd.DataFrame,
    capacity: pd.DataFrame,
    failures: pd.DataFrame,
    selection_scores: pd.Series,
    selected_variant: str,
    walk_forward: WalkForwardSelection,
    opens: pd.DataFrame,
    cash_returns: pd.Series,
    terminal_returns: pd.DataFrame | None,
    input_fingerprints: dict[str, str | None],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    splits = {
        "selection": (None, config.validation.selection_end),
        "validation": (config.validation.validation_start, config.validation.validation_end),
        "sealed_test": (config.validation.sealed_test_start, None),
    }
    metric_rows: list[dict[str, object]] = []
    for name, result in results.items():
        for split, (start, end) in splits.items():
            sample = result.returns.loc[start:end].dropna()
            if len(sample) < 2:
                continue
            metric_rows.append(
                {
                    "strategy": name,
                    "split": split,
                    **performance_metrics(
                        sample,
                        result.equity.loc[sample.index],
                        result.turnover.loc[sample.index],
                        result.transaction_costs.loc[sample.index],
                    ),
                }
            )
    pd.DataFrame(metric_rows).to_csv(output_dir / "metrics.csv", index=False)
    registry.to_csv(output_dir / "variant_registry.csv", index=False)
    capacity.to_csv(output_dir / "capacity.csv", index=False)
    failures.to_csv(output_dir / "failed_variants.csv", index=False)
    selection_scores.rename("median_annual_excess_return").to_csv(
        output_dir / "selection_scores.csv", index_label="variant"
    )
    walk_forward.folds.to_csv(output_dir / "walk_forward_folds.csv", index=False)
    pd.concat(
        [
            walk_forward.selected_returns,
            walk_forward.selected_variants,
        ],
        axis=1,
    ).to_parquet(output_dir / "walk_forward_path.parquet")
    plans[selected_variant].decisions.to_parquet(output_dir / "selected_decisions.parquet")
    (output_dir / "coverage.json").write_text(
        json.dumps(coverage.to_dict(), indent=2) + "\n", encoding="utf-8"
    )

    selected_cost_rows: list[dict[str, object]] = []
    for round_trip_bps in config.execution.round_trip_cost_bps:
        result = run_backtest(
            selected_variant,
            opens,
            plans[selected_variant].target_weights,
            ExecutionConfig(
                initial_capital=config.execution.initial_capital,
                transaction_cost_bps=round_trip_bps / 2.0,
                minimum_trade_weight=0.0,
            ),
            cash_returns=cash_returns,
            terminal_return_overrides=terminal_returns,
        )
        sample = result.returns.loc[config.validation.sealed_test_start :].dropna()
        if len(sample) >= 2:
            selected_cost_rows.append(
                {
                    "round_trip_cost_bps": round_trip_bps,
                    **performance_metrics(
                        sample,
                        result.equity.loc[sample.index],
                        result.turnover.loc[sample.index],
                        result.transaction_costs.loc[sample.index],
                    ),
                }
            )
    pd.DataFrame(selected_cost_rows).to_csv(
        output_dir / "selected_cost_sensitivity.csv", index=False
    )
    candidate_names = selection_scores.index.astype(str).tolist()
    candidate_returns = pd.concat({name: results[name].returns for name in candidate_names}, axis=1)
    benchmark_returns = results["spy"].returns.reindex(candidate_returns.index)
    selection_returns = candidate_returns.loc[: config.validation.selection_end]
    selection_benchmark = benchmark_returns.loc[selection_returns.index]
    family_validation = stationary_bootstrap_family_validation(
        selection_returns,
        selection_benchmark,
        mean_block_sessions=config.validation.mean_block_sessions,
        samples=config.validation.bootstrap_samples,
        fdr_level=config.validation.fdr_level,
    )
    family_validation.variants.to_csv(output_dir / "multiple_testing.csv")
    pbo = approximate_combinatorial_pbo(
        selection_returns,
        partitions=config.validation.pbo_partitions,
    )
    walk_forward_bootstrap = paired_stationary_bootstrap(
        walk_forward.selected_returns,
        benchmark_returns.reindex(walk_forward.selected_returns.index),
        mean_block_sessions=config.validation.mean_block_sessions,
        samples=config.validation.bootstrap_samples,
    )
    concentration_frames: list[pd.DataFrame] = []
    concentration_summary: dict[str, object] = {}
    for split, (start, end) in splits.items():
        concentration = asset_pnl_concentration(
            results[selected_variant],
            opens,
            initial_capital=config.execution.initial_capital,
            start=start,
            end=end,
            terminal_return_overrides=terminal_returns,
        )
        concentration_summary[split] = concentration.summary
        frame = concentration.assets.reset_index()
        frame.insert(0, "split", split)
        concentration_frames.append(frame)
    pd.concat(concentration_frames, ignore_index=True).to_csv(
        output_dir / "asset_concentration.csv", index=False
    )
    statistical_validation = {
        "selection_family": family_validation.summary,
        "approximate_combinatorial_pbo": pbo,
        "walk_forward_vs_spy": walk_forward_bootstrap,
        "selected_variant_asset_concentration": concentration_summary,
        "selection_period_only_for_family_tests": (f"through {config.validation.selection_end}"),
        "interpretation": (
            "Multiplicity tests use the complete capacity-passing preregistered family. "
            "The paired interval uses stitched expanding walk-forward returns. Neither result "
            "authorizes trading without prospective evidence."
        ),
    }
    (output_dir / "statistical_validation.json").write_text(
        json.dumps(statistical_validation, indent=2) + "\n", encoding="utf-8"
    )

    regime_rows: list[pd.DataFrame] = []
    for name in [selected_variant, *config.validation.required_comparators]:
        result = results[name]
        regimes = regime_metrics(result.returns, result.equity)
        if regimes.empty:
            continue
        regimes = regimes.reset_index()
        regimes.insert(0, "strategy", name)
        regime_rows.append(regimes)
    regime_output = (
        pd.concat(regime_rows, ignore_index=True)
        if regime_rows
        else pd.DataFrame(
            columns=[
                "strategy",
                "regime",
                "start",
                "end",
                "total_return",
                "annualized_volatility",
                "max_drawdown",
                "worst_day",
            ]
        )
    )
    regime_output.to_csv(output_dir / "hostile_regimes.csv", index=False)

    artifact_sha256 = {
        path.name: file_sha256(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "manifest.json"
    }
    manifest = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "research_status": "retrospective_candidate_not_live_approved",
        "automatic_order_placement": False,
        "selected_on": f"data through {config.validation.selection_end}",
        "selected_variant": selected_variant,
        "validation_period": (
            f"{config.validation.validation_start} through {config.validation.validation_end}"
        ),
        "sealed_test_start": config.validation.sealed_test_start,
        "configuration": asdict(config),
        "coverage_gate": coverage.to_dict(),
        "failed_variant_count": len(failures),
        "selected_capacity_gate": capacity.loc[capacity["variant"] == selected_variant].to_dict(
            orient="records"
        ),
        "required_comparators": list(config.validation.required_comparators),
        "statistical_validation": statistical_validation,
        "input_fingerprints": input_fingerprints,
        "artifact_sha256": artifact_sha256,
        "implementation_sha256": implementation_sha256(),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
