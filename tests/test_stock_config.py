from __future__ import annotations

from pathlib import Path

from swing_trader.stock_config import load_stock_experiment_config


def test_repository_stock_experiment_registry_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]

    config = load_stock_experiment_config(root / "config/stock_experiments.toml")

    assert config.universe.include_delisted is True
    assert config.execution.round_trip_cost_bps == (20.0, 50.0, 100.0)
    assert config.execution.maximum_adv_participation == 0.01
    assert config.validation.bootstrap_samples == 2_000
    assert config.validation.mean_block_sessions == 21
    assert config.validation.pbo_partitions == 8
    assert config.validation.fdr_level == 0.05
    assert config.signals.families == (
        "short_volume",
        "smooth_momentum",
        "volume_breakout",
    )
