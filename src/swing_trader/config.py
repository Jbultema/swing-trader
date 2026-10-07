from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataConfig:
    start: str
    benchmark: str
    cash_proxy: str
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class StrategyConfig:
    top_n: int
    rank_buffer: int
    fast_days: int
    medium_days: int
    slow_days: int
    skip_days: int
    trend_days: int
    fast_trend_days: int
    volatility_days: int
    atr_days: int
    trailing_high_days: int
    atr_multiple: float
    target_volatility: float
    max_asset_weight: float
    stress_volatility: float
    stress_drawdown: float
    risk_off_exposure: float
    rebalance_weekday: int


@dataclass(frozen=True)
class ExecutionConfig:
    initial_capital: float
    transaction_cost_bps: float
    minimum_trade_weight: float


@dataclass(frozen=True)
class ValidationConfig:
    development_end: str
    validation_start: str
    validation_end: str
    recent_diagnostic_start: str
    minimum_history_days: int


@dataclass(frozen=True)
class AppConfig:
    data: DataConfig
    strategy: StrategyConfig
    execution: ExecutionConfig
    validation: ValidationConfig


def load_config(path: Path | str) -> AppConfig:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    return AppConfig(
        data=DataConfig(**raw["data"] | {"tickers": tuple(raw["data"]["tickers"])}),
        strategy=StrategyConfig(**raw["strategy"]),
        execution=ExecutionConfig(**raw["execution"]),
        validation=ValidationConfig(**raw["validation"]),
    )
