from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

from swing_trader.config import load_config
from swing_trader.data import download_prices, load_prices
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
    frame = download_prices(config.data.tickers, config.data.start, output)
    typer.echo(f"Saved {len(frame):,} rows through {frame.index.max().date()} to {output}")


@research_app.command("run")
def research_run(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/default.toml"),
    prices_path: Annotated[Path, typer.Option("--prices")] = Path("data/raw/prices.parquet"),
    output: Annotated[Path, typer.Option("--output")] = Path("reports/latest"),
) -> None:
    config = load_config(config_path)
    prices = load_prices(prices_path)
    results = run_research(prices, config, output)
    typer.echo(f"Wrote {len(results)} strategy results to {output}")


@app.command("daily")
def daily(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/default.toml"),
) -> None:
    root = _root()
    config = load_config(config_path)
    prices_path = root / "data/raw/prices.parquet"
    download_prices(config.data.tickers, config.data.start, prices_path)
    run_research(load_prices(prices_path), config, root / "reports/latest")
    typer.echo("Daily research snapshot complete; no orders were placed.")


@app.command("dashboard")
def dashboard() -> None:
    subprocess.run(
        ["streamlit", "run", str(_root() / "src/swing_trader/dashboard.py")], check=True
    )
