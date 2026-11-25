"""Independent close-known exports for common-clock portfolio research."""

from pathlib import Path

import numpy as np
import pandas as pd

from swing_trader.backtest import classic_dual_momentum_weights, panic_guarded_dual_momentum_weights
from swing_trader.config import load_config
from swing_trader.strategy import build_strategy

SYSTEM = "swing-trader"


def research_targets(ohlcv, config):
    close = ohlcv["Close"]
    champion = classic_dual_momentum_weights(close, config.data.tickers, config.strategy.top_n)
    panic = panic_guarded_dual_momentum_weights(
        close,
        config.data.tickers,
        benchmark=config.data.benchmark,
        top_n=config.strategy.top_n,
        volatility_days=config.strategy.volatility_days,
        stress_volatility=config.strategy.stress_volatility,
        stress_drawdown=config.strategy.stress_drawdown,
    )
    fast = build_strategy(ohlcv, config.strategy, config.data.benchmark).target_weights
    # Keep the frozen champion intact; test hysteretic protection separately.
    benchmark = close[config.data.benchmark]
    stress = benchmark.div(benchmark.rolling(63).max()).sub(1).le(-0.10)
    repaired = benchmark.gt(benchmark.rolling(21).mean()) & benchmark.pct_change(
        5, fill_method=None
    ).gt(0)
    protected = champion.copy()
    active, good = False, 0
    for date in close.index:
        if stress.loc[date]:
            active, good = True, 0
        elif active:
            good = good + 1 if repaired.loc[date] else 0
            if good >= 5:
                active = False
        if active:
            protected.loc[date] *= 0.5
    start = close.index[config.validation.minimum_history_days]
    return {
        name: (w, start, role)
        for name, w, role in [
            ("swing_primary", champion, "primary"),
            ("swing_panic_comparator", panic, "comparator"),
            ("swing_persistent_recovery", protected, "challenger"),
            ("swing_fast_rejected", fast, "rejected_diagnostic"),
        ]
    }


def export_bundle(output):
    import hashlib
    import json

    config = load_config("config/default.toml")
    source = Path("data/raw/prices.parquet")
    metadata = json.loads(source.with_suffix(".manifest.json").read_text())
    expected = metadata.get("sha256")
    if not expected or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError("Native price manifest mismatch")
    prices = pd.read_parquet(source)
    cash = prices["Close"][config.data.cash_proxy].pct_change(fill_method=None).fillna(0)
    return _publish(
        output,
        prices["Close"],
        research_targets(prices, config),
        cash,
        [source, source.with_suffix(".manifest.json"), Path("config/default.toml")],
        "native close-known targets, next-adjusted-open fills and open-to-open accounting",
        [
            "The frozen champion is monthly 12-month momentum, not a fast swing strategy.",
            "Faster rejected candidate remains diagnostic, never the default.",
            "New persistent recovery is research-only.",
            "Common-clock results do not replace native results.",
        ],
        "swing_primary",
    )


def _publish(output, prices, targets, cash, source_inputs, native_execution, notes, primary):
    import hashlib
    import json
    from datetime import UTC, datetime
    from importlib.metadata import version

    output.mkdir(parents=True, exist_ok=False)
    if (
        not isinstance(prices.index, pd.DatetimeIndex)
        or prices.index.hasnans or prices.index.has_duplicates
        or not prices.index.is_monotonic_increasing or not prices.columns.is_unique
        or np.isinf(prices.to_numpy(dtype=float)).any() or prices.le(0).any().any()
    ):
        raise ValueError("Invalid source session index")
    if (
        not cash.index.equals(prices.index) or not np.isfinite(cash).all()
        or cash.le(-1).any()
    ):
        raise ValueError("Cash observations must be finite and aligned")
    prices.to_parquet(output / "prices.parquet")
    cash.reindex(prices.index).fillna(0).rename("cash_return").to_frame().to_parquet(
        output / "cash_returns.parquet"
    )
    experts = []
    for name, (weights, evaluation_start, role) in targets.items():
        if (
            not weights.index.equals(prices.index) or not weights.columns.is_unique
            or not set(weights) <= set(prices)
            or not np.isfinite(weights.to_numpy(dtype=float)).all()
            or weights.lt(-1e-12).any().any()
            or weights.sum(axis=1).gt(1 + 1e-8).any()
        ):
            raise ValueError(f"Invalid native targets: {name}")
        if (weights.gt(1e-12) & prices.reindex(columns=weights.columns).isna()).any().any():
            raise ValueError(f"Unpriced native target: {name}")
        filename = f"{name}.parquet"
        weights.to_parquet(output / filename)
        experts.append(
            {
                "id": name,
                "weights_file": filename,
                "evaluation_start": str(pd.Timestamp(evaluation_start).date()),
                "role": role,
                "evidence_status": "retrospective_research",
                "target_semantics": "close_known_risky_budgets",
            }
        )
    artifacts = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.iterdir())
        if p.is_file()
    }
    code = hashlib.sha256()
    for p in sorted(Path("src").rglob("*.py")):
        code.update(str(p).encode())
        code.update(p.read_bytes())
    source_inputs = [*source_inputs, *[
        p for p in (Path("pyproject.toml"), Path("poetry.lock"))
        if p.is_file() and p not in source_inputs
    ]]
    manifest = {
        "schema_version": 1,
        "runtime_versions": {name: version(name) for name in ("numpy", "pandas", "pyarrow", "exchange-calendars")},
        "system": SYSTEM,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "data_end": str(prices.index.max().date()),
        "primary_expert": primary,
        "native_execution": native_execution,
        "authority": "research_only",
        "automatic_promotion_allowed": False,
        "code_sha256": code.hexdigest(),
        "experts": experts,
        "source_inputs": [
            {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in source_inputs
        ],
        "artifacts": artifacts,
        "notes": notes,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return output / "manifest.json"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Research-only Portfolio Lab specialist export")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(export_bundle(args.output))


if __name__ == "__main__":
    main()
