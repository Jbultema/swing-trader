from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from swing_trader.stock_config import load_stock_experiment_config
from swing_trader.stock_research import run_stock_research


def test_stock_research_writes_preregistered_walk_forward_artifacts(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    base = load_stock_experiment_config(root / "config/stock_experiments.toml")
    dates = pd.bdate_range("2004-01-02", "2014-12-31")
    floor = dates[300]
    config = replace(
        base,
        universe=replace(base.universe, membership_floor=str(floor.date())),
        signals=replace(
            base.signals,
            families=("short_volume",),
            holding_sessions=(21,),
        ),
        exits=replace(base.exits, families=("combined",), atr_multiples=(3.0,)),
        validation=replace(
            base.validation,
            selection_end="2009-12-31",
            validation_start="2010-01-01",
            validation_end="2012-12-31",
            sealed_test_start="2013-01-01",
        ),
    )
    close = pd.DataFrame(
        {
            "A": [50.0 + 0.03 * value for value in range(len(dates))],
            "B": [70.0 + 0.02 * value for value in range(len(dates))],
        },
        index=dates,
    )
    ohlcv = pd.concat(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": pd.DataFrame(1_000_000.0, index=dates, columns=close.columns),
        },
        axis=1,
    )
    ohlcv.columns.names = ["field", "ticker"]
    membership = pd.DataFrame(True, index=dates, columns=close.columns)
    benchmark_close = pd.Series([100.0 + 0.015 * value for value in range(len(dates))], index=dates)
    benchmark = pd.DataFrame({"Open": benchmark_close, "Close": benchmark_close})
    cash_returns = pd.Series(0.00001, index=dates)

    run = run_stock_research(
        ohlcv,
        membership,
        benchmark,
        cash_returns,
        config,
        tmp_path,
    )

    assert run.coverage.status == "passed"
    assert run.selected_variant.startswith("short_volume__combined")
    assert (tmp_path / "walk_forward_folds.csv").exists()
    assert (tmp_path / "selected_decisions.parquet").exists()
    assert (tmp_path / "manifest.json").exists()
