from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
from dotenv import load_dotenv

from swing_trader.alpha_validation import (
    latest_alpha_validation_for_candidate,
    validate_candidate_snapshot_with_alpha,
)
from swing_trader.audit import audit_operational_artifacts
from swing_trader.cash import download_fred_cash_returns, load_cash_returns
from swing_trader.config import AppConfig, load_config
from swing_trader.data import (
    MarketDataError,
    download_alpha_vantage_monthly,
    download_prices,
    load_cached_alpha_vantage_monthly,
    load_prices,
    reconcile_monthly_adjusted,
)
from swing_trader.events import download_alpha_earnings_calendar, latest_earnings_snapshot
from swing_trader.prospective import write_prospective_evaluation
from swing_trader.research import run_research
from swing_trader.shadow import record_shadow_snapshot
from swing_trader.stock_audit import audit_stock_research_bundle
from swing_trader.stock_candidates import latest_candidate_snapshot, record_current_stock_candidates
from swing_trader.stock_config import load_stock_experiment_config
from swing_trader.stock_daily import run_stock_shadow_daily
from swing_trader.stock_data import write_stock_coverage_audit
from swing_trader.stock_live_data import (
    audit_current_stock_price_snapshot,
    download_current_stock_prices,
    latest_stock_price_manifest,
)
from swing_trader.stock_research import run_stock_research
from swing_trader.stock_shadow_state import (
    held_tickers_from_latest_state,
    record_stock_shadow_state,
    stock_shadow_lineage_dir,
    verify_stock_shadow_state,
)
from swing_trader.stock_universe import (
    audit_current_sp500_snapshot,
    download_current_sp500_snapshot,
    latest_current_sp500_manifest,
)
from swing_trader.ticket import write_trade_preview

app = typer.Typer(no_args_is_help=True)
data_app = typer.Typer(no_args_is_help=True)
research_app = typer.Typer(no_args_is_help=True)
shadow_app = typer.Typer(no_args_is_help=True)
ticket_app = typer.Typer(no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")
app.add_typer(shadow_app, name="shadow")
app.add_typer(ticket_app, name="ticket")


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_local_environment(root: Path | None = None) -> None:
    """Load ignored local credentials without overriding explicit process secrets."""
    load_dotenv((root or _root()) / ".env", override=False)


@data_app.command("update")
def data_update(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/default.toml"),
    output: Annotated[Path, typer.Option("--output")] = Path("data/raw/prices.parquet"),
) -> None:
    config = load_config(config_path)
    frame, quality = _refresh_data_bundle(config, output)
    typer.echo(f"Saved {len(frame):,} rows through {frame.index.max().date()} to {output}")
    typer.echo(f"Decision data gate: {quality['status']}")


@data_app.command("audit-stocks")
def data_audit_stocks(
    prices_path: Annotated[Path, typer.Option("--prices")] = Path("data/stock/ohlcv.parquet"),
    membership_path: Annotated[Path, typer.Option("--membership")] = Path(
        "data/stock/membership.parquet"
    ),
    output: Annotated[Path, typer.Option("--output")] = Path("reports/stock/coverage_audit.json"),
    minimum_history_sessions: Annotated[int, typer.Option("--minimum-history-sessions")] = 252,
) -> None:
    """Audit a normalized stock panel before any strategy is backtested."""
    prices = pd.read_parquet(prices_path)
    close = prices["Close"] if isinstance(prices.columns, pd.MultiIndex) else prices
    membership = pd.read_parquet(membership_path)
    result = write_stock_coverage_audit(
        close,
        membership,
        output,
        minimum_history_sessions=minimum_history_sessions,
    )
    typer.echo(
        f"Stock coverage gate: {result.status}; "
        f"{result.member_observation_coverage:.2%} member-date coverage; "
        f"{len(result.missing_tickers)} missing tickers."
    )
    if result.status != "passed":
        raise typer.Exit(code=2)


@data_app.command("update-cash")
def data_update_cash(
    output: Annotated[Path, typer.Option("--output")] = Path(
        "data/stock/fred_3m_cash_returns.parquet"
    ),
) -> None:
    """Cache causal three-month Treasury returns from the official FRED series."""
    returns = download_fred_cash_returns(output)
    typer.echo(
        f"Saved {len(returns):,} cash-return observations through "
        f"{returns.index.max().date()} to {output}."
    )


@data_app.command("refresh-alpha-monthly-cache")
def data_refresh_alpha_monthly_cache(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/default.toml"),
    output: Annotated[Path, typer.Option("--output")] = Path(
        "data/raw/alpha_vantage_monthly.parquet"
    ),
) -> None:
    """Spend the free ETF reconciliation calls once weekly, outside stock sessions."""
    _load_local_environment()
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    config = load_config(config_path)
    frame = download_alpha_vantage_monthly(config.data.tickers, key, output)
    typer.echo(
        f"Locked weekly free-tier monthly cache for {len(frame.columns)} symbols at {output}."
    )


@data_app.command("snapshot-earnings")
def data_snapshot_earnings(
    output: Annotated[Path, typer.Option("--output")] = Path("data/events/earnings"),
    horizon: Annotated[str, typer.Option("--horizon")] = "3month",
) -> None:
    """Lock a prospective earnings calendar; never use it as a historical backfill."""
    _load_local_environment()
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    path = download_alpha_earnings_calendar(key, output, horizon=horizon)
    typer.echo(f"Locked prospective earnings snapshot {path.name}; no order was placed.")


@data_app.command("snapshot-stock-universe")
def data_snapshot_stock_universe(
    output: Annotated[Path, typer.Option("--output")] = Path("data/stock-shadow/universe"),
) -> None:
    """Lock the currently observed S&P 500 roster for prospective use only."""
    snapshot = download_current_sp500_snapshot(output)
    typer.echo(
        f"Locked {snapshot.rows} current constituents in {snapshot.data_path.name}; "
        "prospective use only, no historical backfill and no order was placed."
    )


@data_app.command("verify-stock-universe")
def data_verify_stock_universe(
    snapshots: Annotated[Path, typer.Option("--snapshots")] = Path(
        "data/stock-shadow/universe"
    ),
    max_age_hours: Annotated[float, typer.Option("--max-age-hours")] = 48.0,
) -> None:
    """Fail closed if the latest current-universe snapshot is stale or modified."""
    manifest = latest_current_sp500_manifest(snapshots)
    result = audit_current_sp500_snapshot(manifest, max_age_hours=max_age_hours)
    typer.echo(json.dumps(result.to_dict(), indent=2))
    if not result.passed:
        raise typer.Exit(code=2)


@data_app.command("snapshot-stock-prices")
def data_snapshot_stock_prices(
    universe_snapshots: Annotated[Path, typer.Option("--universe-snapshots")] = Path(
        "data/stock-shadow/universe"
    ),
    output: Annotated[Path, typer.Option("--output")] = Path("data/stock-shadow/prices"),
    lookback_calendar_days: Annotated[int, typer.Option("--lookback-calendar-days")] = 800,
) -> None:
    """Lock current-roster adjusted OHLCV after all prospective data gates pass."""
    universe_manifest = latest_current_sp500_manifest(universe_snapshots)
    snapshot = download_current_stock_prices(
        universe_manifest,
        output,
        lookback_calendar_days=lookback_calendar_days,
    )
    typer.echo(
        f"Locked {snapshot.rows} sessions for {snapshot.tickers} available symbols; "
        f"latest session {snapshot.validation.latest_session}; no order was placed."
    )


@data_app.command("verify-stock-prices")
def data_verify_stock_prices(
    universe_snapshots: Annotated[Path, typer.Option("--universe-snapshots")] = Path(
        "data/stock-shadow/universe"
    ),
    price_snapshots: Annotated[Path, typer.Option("--price-snapshots")] = Path(
        "data/stock-shadow/prices"
    ),
    max_age_hours: Annotated[float, typer.Option("--max-age-hours")] = 48.0,
) -> None:
    """Fail closed if the latest prospective price snapshot is stale or modified."""
    result = audit_current_stock_price_snapshot(
        latest_stock_price_manifest(price_snapshots),
        universe_manifest_path=latest_current_sp500_manifest(universe_snapshots),
        max_age_hours=max_age_hours,
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))
    if not result.passed:
        raise typer.Exit(code=2)


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


@research_app.command("stocks")
def research_stocks(
    config_path: Annotated[Path, typer.Option("--config")] = Path("config/stock_experiments.toml"),
    stock_dir: Annotated[Path, typer.Option("--stock-data")] = Path("data/stock"),
    benchmark_prices_path: Annotated[Path, typer.Option("--benchmark-prices")] = Path(
        "data/raw/prices.parquet"
    ),
    cash_returns_path: Annotated[Path, typer.Option("--cash-returns")] = Path(
        "data/stock/fred_3m_cash_returns.parquet"
    ),
    output: Annotated[Path, typer.Option("--output")] = Path("reports/stock/latest"),
) -> None:
    """Run the preregistered stock research after every data gate passes."""
    config = load_stock_experiment_config(config_path)
    ohlcv = pd.read_parquet(stock_dir / "ohlcv.parquet")
    membership = pd.read_parquet(stock_dir / "membership.parquet")
    benchmark_prices = load_prices(benchmark_prices_path)
    market = config.benchmarks.market
    benchmark = pd.DataFrame(
        {
            "Open": benchmark_prices["Open"][market],
            "Close": benchmark_prices["Close"][market],
        }
    )
    terminal_path = stock_dir / "terminal_returns.parquet"
    terminal = pd.read_parquet(terminal_path) if terminal_path.exists() else None
    result = run_stock_research(
        ohlcv,
        membership,
        benchmark,
        load_cash_returns(cash_returns_path),
        config,
        output,
        terminal_return_overrides=terminal,
    )
    typer.echo(
        f"Stock research selected {result.selected_variant} on the selection period; "
        f"wrote retrospective evidence to {output}. No order was placed."
    )


@research_app.command("verify-stocks")
def research_verify_stocks(
    report_dir: Annotated[Path, typer.Option("--reports")] = Path("reports/stock/latest"),
) -> None:
    """Fail closed if a stock evidence bundle is missing, stale, or modified."""
    result = audit_stock_research_bundle(report_dir)
    typer.echo(json.dumps(result.to_dict(), indent=2))
    if not result.integrity_passed:
        raise typer.Exit(code=2)


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


@app.command("stock-daily")
def stock_daily(
    archive_root: Annotated[Path | None, typer.Option("--archive-root")] = None,
) -> None:
    """Run the no-paid stock shadow after the close; never connect to a broker."""
    root = _root()
    _load_local_environment(root)
    result = run_stock_shadow_daily(
        root,
        archive_root=archive_root,
        api_key=os.getenv("ALPHA_VANTAGE_API_KEY", "").strip(),
    )
    typer.echo(
        f"Stock shadow {result.status} for {result.session} in {result.lineage_id}; "
        f"Alpha={result.alpha_status}, earnings={result.earnings_status}; no order was placed."
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


@shadow_app.command("screen-stocks")
def shadow_screen_stocks(
    universe_snapshots: Annotated[Path, typer.Option("--universe-snapshots")] = Path(
        "data/stock-shadow/universe"
    ),
    price_snapshots: Annotated[Path, typer.Option("--price-snapshots")] = Path(
        "data/stock-shadow/prices"
    ),
    output: Annotated[Path, typer.Option("--output")] = Path(
        "reports/stock-shadow/candidates"
    ),
    state_root: Annotated[Path, typer.Option("--state-root")] = Path(
        "reports/stock-shadow/lineages"
    ),
    config_path: Annotated[Path, typer.Option("--config")] = Path(
        "config/stock_shadow.toml"
    ),
) -> None:
    """Lock explainable stock candidates from fresh close-known inputs; never place orders."""
    result = record_current_stock_candidates(
        latest_current_sp500_manifest(universe_snapshots),
        latest_stock_price_manifest(price_snapshots),
        output,
        required_validation_symbols=held_tickers_from_latest_state(
            stock_shadow_lineage_dir(state_root, config_path) / "states",
            arm_names=("consensus",),
        ),
    )
    typer.echo(
        f"Locked {result.family_candidates} unique candidates as of {result.as_of_session}; "
        f"{result.validation_symbols} symbols require independent validation; no order was placed."
    )


@shadow_app.command("validate-stock-candidates")
def shadow_validate_stock_candidates(
    candidates: Annotated[Path, typer.Option("--candidates")] = Path(
        "reports/stock-shadow/candidates"
    ),
    output: Annotated[Path, typer.Option("--output")] = Path(
        "data/stock-shadow/alpha-validation"
    ),
    quota_dir: Annotated[Path, typer.Option("--quota-dir")] = Path(
        "data/stock-shadow/provider-quota/alpha-vantage"
    ),
) -> None:
    """Use the free Alpha quota only on names that could be held or bought."""
    _load_local_environment()
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    path = validate_candidate_snapshot_with_alpha(
        latest_candidate_snapshot(candidates),
        key,
        output,
        quota_dir=quota_dir,
    )
    typer.echo(f"Locked independent candidate validation {path.name}; no order was placed.")


@shadow_app.command("record-stocks")
def shadow_record_stocks(
    universe_snapshots: Annotated[Path, typer.Option("--universe-snapshots")] = Path(
        "data/stock-shadow/universe"
    ),
    price_snapshots: Annotated[Path, typer.Option("--price-snapshots")] = Path(
        "data/stock-shadow/prices"
    ),
    candidates: Annotated[Path, typer.Option("--candidates")] = Path(
        "reports/stock-shadow/candidates"
    ),
    alpha_validations: Annotated[Path, typer.Option("--alpha-validations")] = Path(
        "data/stock-shadow/alpha-validation"
    ),
    earnings_snapshots: Annotated[Path, typer.Option("--earnings-snapshots")] = Path(
        "data/events/earnings"
    ),
    config_path: Annotated[Path, typer.Option("--config")] = Path(
        "config/stock_shadow.toml"
    ),
    output_root: Annotated[Path, typer.Option("--output-root")] = Path(
        "reports/stock-shadow/lineages"
    ),
) -> None:
    """Advance the immutable next-open stock paper state; never place orders."""
    candidate = latest_candidate_snapshot(candidates)
    state_dir = stock_shadow_lineage_dir(output_root, config_path) / "states"
    result = record_stock_shadow_state(
        candidate,
        latest_current_sp500_manifest(universe_snapshots),
        latest_stock_price_manifest(price_snapshots),
        config_path,
        state_dir,
        alpha_validation_path=latest_alpha_validation_for_candidate(
            alpha_validations,
            candidate,
        ),
        earnings_path=latest_earnings_snapshot(earnings_snapshots),
    )
    typer.echo(
        f"Locked {'initial' if result.initialization else 'advanced'} stock state "
        f"for {result.as_of_session}: {result.targets}; no order was placed."
    )


@shadow_app.command("verify-stock-state")
def shadow_verify_stock_state(
    state_path: Annotated[Path, typer.Option("--state")],
) -> None:
    """Verify an immutable stock paper-state content hash."""
    passed = verify_stock_shadow_state(state_path)
    typer.echo(json.dumps({"path": str(state_path), "content_hash_passed": passed}, indent=2))
    if not passed:
        raise typer.Exit(code=2)


@shadow_app.command("evaluate")
def shadow_evaluate(
    record_dir: Annotated[Path, typer.Option("--records")] = Path("evidence/records"),
    prices_path: Annotated[Path, typer.Option("--prices")] = Path("data/raw/prices.parquet"),
    output: Annotated[Path, typer.Option("--output")] = Path(
        "reports/latest/prospective_evaluation.json"
    ),
) -> None:
    """Score matured, deduplicated prospective records without opening future data early."""
    result = write_prospective_evaluation(record_dir, load_prices(prices_path), output)
    summary = result["eligible_summary"]
    assert isinstance(summary, dict)
    typer.echo(
        f"Evaluated {result['unique_schema_v3_decisions']} unique schema-v3 decisions; "
        f"{summary['eligible_unique_decisions']} passed their original data gate."
    )


@ticket_app.command("preview")
def ticket_preview(
    portfolio: Annotated[Path, typer.Option("--portfolio")] = Path("config/portfolio.toml"),
    prices_path: Annotated[Path, typer.Option("--prices")] = Path("data/raw/prices.parquet"),
    decisions_path: Annotated[Path, typer.Option("--decisions")] = Path(
        "reports/latest/latest_decisions.json"
    ),
    output: Annotated[Path, typer.Option("--output")] = Path("reports/private/trade_preview.json"),
) -> None:
    """Build a local, non-executable retirement-sleeve share preview."""
    preview = write_trade_preview(load_prices(prices_path), decisions_path, portfolio, output)
    typer.echo(f"Wrote {preview['status']} to {output}; no order was placed or authorized.")


def _refresh_data_bundle(
    config: AppConfig, prices_path: Path
) -> tuple[pd.DataFrame, dict[str, object]]:
    _load_local_environment()
    download_tickers = tuple(dict.fromkeys((*config.data.tickers, config.data.cash_proxy)))
    primary = download_prices(download_tickers, config.data.start, prices_path)
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    secondary = None
    error = None
    if key:
        secondary_path = prices_path.parent / "alpha_vantage_monthly.parquet"
        try:
            secondary = load_cached_alpha_vantage_monthly(secondary_path, config.data.tickers)
            if secondary is None:
                secondary = download_alpha_vantage_monthly(
                    config.data.tickers,
                    key,
                    secondary_path,
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
