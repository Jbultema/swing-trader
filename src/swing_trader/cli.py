from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer

from swing_trader.audit import audit_operational_artifacts
from swing_trader.config import AppConfig, load_config
from swing_trader.data import (
    MarketDataError,
    download_alpha_vantage_monthly,
    download_prices,
    load_prices,
    reconcile_monthly_adjusted,
)
from swing_trader.research import run_research
from swing_trader.shadow import record_shadow_snapshot

app = typer.Typer(no_args_is_help=True)
data_app = typer.Typer(no_args_is_help=True)
research_app = typer.Typer(no_args_is_help=True)
shadow_app = typer.Typer(no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")
app.add_typer(shadow_app, name="shadow")


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
    shadow_path = record_shadow_snapshot(root / "reports/latest", root / "reports/shadow")
    typer.echo(
        f"Daily research snapshot complete; locked {shadow_path.name}; no orders were placed."
    )


@app.command("dashboard")
def dashboard() -> None:
    subprocess.run(["streamlit", "run", str(_root() / "src/swing_trader/dashboard.py")], check=True)


@app.command("audit")
def audit(
    report_dir: Annotated[Path, typer.Option("--reports")] = Path("reports/latest"),
    shadow_dir: Annotated[Path, typer.Option("--shadows")] = Path("reports/shadow"),
    prices_path: Annotated[Path, typer.Option("--prices")] = Path("data/raw/prices.parquet"),
    prices_manifest_path: Annotated[Path, typer.Option("--prices-manifest")] = Path(
        "data/raw/prices.manifest.json"
    ),
    require_data_gate: Annotated[
        bool, typer.Option("--require-data-gate/--allow-failed-data-gate")
    ] = False,
) -> None:
    """Verify the latest decision bundle and every immutable shadow record."""
    result = audit_operational_artifacts(
        report_dir,
        shadow_dir,
        prices_path=prices_path,
        prices_manifest_path=prices_manifest_path,
    )
    typer.echo(result.markdown(), nl=False)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(result.markdown())
    if os.getenv("GITHUB_ACTIONS") == "true" and not result.data_gate_passed:
        typer.echo(
            f"::warning title=Decision data gate failed::{result.data_quality_status}; no action is authorized."
        )
    if not result.integrity_passed:
        raise typer.Exit(code=1)
    if require_data_gate and not result.data_gate_passed:
        raise typer.Exit(code=2)


@shadow_app.command("record")
def shadow_record(
    report_dir: Annotated[Path, typer.Option("--reports")] = Path("reports/latest"),
    shadow_dir: Annotated[Path, typer.Option("--output")] = Path("reports/shadow"),
) -> None:
    output = record_shadow_snapshot(report_dir, shadow_dir)
    typer.echo(f"Locked prospective snapshot: {output}")


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
    primary_manifest = json.loads(prices_path.with_suffix(".manifest.json").read_text())
    quality["primary_snapshot_sha256"] = primary_manifest["sha256"]
    quality["primary_snapshot_downloaded_at_utc"] = primary_manifest["downloaded_at_utc"]
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
