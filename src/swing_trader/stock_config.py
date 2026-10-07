from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class StockUniverseConfig:
    name: str
    membership_floor: str
    minimum_price: float
    minimum_median_dollar_volume: float
    include_delisted: bool


@dataclass(frozen=True)
class StockExecutionConfig:
    signal_at: str
    execute_at: str
    round_trip_cost_bps: tuple[float, ...]
    maximum_positions: int
    maximum_position_weight: float
    maximum_adv_participation: float
    adv_sessions: int
    initial_capital: float


@dataclass(frozen=True)
class StockSignalsConfig:
    families: tuple[str, ...]
    formation_days: int
    skip_days: int
    holding_sessions: tuple[int, ...]


@dataclass(frozen=True)
class StockExitsConfig:
    families: tuple[str, ...]
    hard_stop_fraction: float
    atr_multiples: tuple[float, ...]
    rank_exit_multiple: float
    maximum_holding_sessions: int


@dataclass(frozen=True)
class StockValidationConfig:
    selection_end: str
    validation_start: str
    validation_end: str
    sealed_test_start: str
    primary_metric: str
    required_comparators: tuple[str, ...]
    bootstrap_samples: int
    mean_block_sessions: int
    pbo_partitions: int
    fdr_level: float


@dataclass(frozen=True)
class StockBenchmarkConfig:
    market: str
    cash_series: str


@dataclass(frozen=True)
class StockExperimentConfig:
    universe: StockUniverseConfig
    execution: StockExecutionConfig
    signals: StockSignalsConfig
    exits: StockExitsConfig
    validation: StockValidationConfig
    benchmarks: StockBenchmarkConfig


def load_stock_experiment_config(path: Path | str) -> StockExperimentConfig:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    execution = raw["execution"] | {
        "round_trip_cost_bps": tuple(
            float(value) for value in raw["execution"]["round_trip_cost_bps"]
        )
    }
    signals = raw["signals"] | {
        "families": tuple(raw["signals"]["families"]),
        "holding_sessions": tuple(int(value) for value in raw["signals"]["holding_sessions"]),
    }
    exits = raw["exits"] | {
        "families": tuple(raw["exits"]["families"]),
        "atr_multiples": tuple(float(value) for value in raw["exits"]["atr_multiples"]),
    }
    validation = raw["validation"] | {
        "required_comparators": tuple(raw["validation"]["required_comparators"])
    }
    config = StockExperimentConfig(
        universe=StockUniverseConfig(**raw["universe"]),
        execution=StockExecutionConfig(**execution),
        signals=StockSignalsConfig(**signals),
        exits=StockExitsConfig(**exits),
        validation=StockValidationConfig(**validation),
        benchmarks=StockBenchmarkConfig(**raw["benchmarks"]),
    )
    _validate_stock_config(config)
    return config


def _validate_stock_config(config: StockExperimentConfig) -> None:
    if not config.universe.include_delisted:
        raise ValueError("Stock research requires delisted securities.")
    if config.execution.signal_at != "regular_session_close":
        raise ValueError("Only regular-session-close stock signals are supported.")
    if config.execution.execute_at != "next_regular_session_open":
        raise ValueError("Only next-regular-session-open execution is supported.")
    if config.execution.maximum_positions < 1:
        raise ValueError("maximum_positions must be positive.")
    if config.execution.maximum_positions * config.execution.maximum_position_weight > 1.0:
        raise ValueError("Maximum stock weights exceed 100% gross exposure.")
    if any(value <= 0.0 for value in config.execution.round_trip_cost_bps):
        raise ValueError("All round-trip cost tiers must be positive.")
    if not config.signals.families or not config.exits.families:
        raise ValueError("At least one signal and exit family is required.")
    selection_end = date.fromisoformat(config.validation.selection_end)
    validation_start = date.fromisoformat(config.validation.validation_start)
    validation_end = date.fromisoformat(config.validation.validation_end)
    sealed_start = date.fromisoformat(config.validation.sealed_test_start)
    if not selection_end < validation_start <= validation_end < sealed_start:
        raise ValueError("Stock selection, validation, and sealed-test dates must not overlap.")
    if config.validation.bootstrap_samples < 100:
        raise ValueError("Stock validation requires at least 100 bootstrap samples.")
    if config.validation.mean_block_sessions < 1:
        raise ValueError("Stock validation mean_block_sessions must be positive.")
    if config.validation.pbo_partitions < 4 or config.validation.pbo_partitions % 2:
        raise ValueError("Stock validation pbo_partitions must be even and at least four.")
    if not 0.0 < config.validation.fdr_level < 1.0:
        raise ValueError("Stock validation fdr_level must be in (0, 1).")
    if config.benchmarks.cash_series != "FRED_DGS3MO":
        raise ValueError("The implemented causal stock cash series is FRED_DGS3MO.")
