from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer

from swing_trader.config import AppConfig, load_config
from swing_trader.data import (
    MarketDataError,
    download_alpha_vantage_monthly,
    download_prices,
    load_prices,
    reconcile_monthly_adjusted,
)
from swing_trader.research import run_research

app = typer.Typer(no_args_is_help=True)
data_app = typer.Typer(no_args_is_help=True)
research_app = typer.Typer(no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


@data_app.command("update")
def data_update(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/default.toml"),
    output: Annotated[Path, typer.Option("--output")] = Path("data/raw/prices.parquet"),
) -> None:
    config = load_config(config_path)
    frame, quality = _refresh_data_bundle(config, output)
    typer.echo(f"Saved {len(frame):,} rows through {frame.index.max().date()} to {output}")
    typer.echo(f"Decision data gate: {quality['status']}")


@research_app.command("run")
def research_run(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/default.toml"),
    prices_path: Annotated[Path, typer.Option("--prices")] = Path("data/raw/prices.parquet"),
    output: Annotated[Path, typer.Option("--output")] = Path("reports/latest"),
    data_quality_path: Annotated[Path, typer.Option("--data-quality")] = Path(
        "data/raw/data_quality.json"
    ),
) -> None:
    config = load_config(config_path)
    prices = load_prices(prices_path)
    quality = _load_data_quality(data_quality_path)
    results = run_research(prices, config, output, data_quality=quality)
    typer.echo(f"Wrote {len(results)} strategy results to {output}")


@app.command("daily")
def daily(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/default.toml"),
) -> None:
    root = _root()
    config = load_config(config_path)
    prices_path = root / "data/raw/prices.parquet"
    _, quality = _refresh_data_bundle(config, prices_path)
    run_research(
        load_prices(prices_path),
        config,
        root / "reports/latest",
        data_quality=quality,
    )
    typer.echo("Daily research snapshot complete; no orders were placed.")


@app.command("dashboard")
def dashboard() -> None:
    subprocess.run(
        ["streamlit", "run", str(_root() / "src/swing_trader/dashboard.py")], check=True
    )


def _refresh_data_bundle(
    config: AppConfig, prices_path: Path
) -> tuple[pd.DataFrame, dict[str, object]]:
    primary = download_prices(config.data.tickers, config.data.start, prices_path)
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    secondary = None
    error = None
    if key:
        try:
            secondary = download_alpha_vantage_monthly(
                config.data.tickers,
                key,
                prices_path.parent / "alpha_vantage_monthly.parquet",
            )
        except MarketDataError as exc:
            error = str(exc)
    else:
        error = "ALPHA_VANTAGE_API_KEY is not configured."
    quality = reconcile_monthly_adjusted(
        primary,
        secondary,
        config.data.tickers,
        months=config.data.reconciliation_months,
        return_tolerance=config.data.monthly_return_tolerance,
        max_primary_age_calendar_days=config.data.max_primary_age_calendar_days,
        secondary_error=error,
    )
    (prices_path.parent / "data_quality.json").write_text(
        json.dumps(quality, indent=2) + "\n", encoding="utf-8"
    )
    return primary, quality


def _load_data_quality(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "status": "missing_data_quality_artifact",
            "decision_data_gate_passed": False,
        }
    return json.loads(path.read_text(encoding="utf-8"))
