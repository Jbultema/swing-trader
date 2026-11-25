from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from swing_trader.provenance import file_sha256, implementation_sha256, tabular_sha256
from swing_trader.stock_candidates import verify_candidate_snapshot
from swing_trader.stock_shadow_state import (
    StockShadowStateError,
    _advance_arm,
    _consensus_rank,
    load_stock_shadow_config,
    verify_stock_shadow_state,
)
from swing_trader.stock_signals import ExitPolicy, build_stock_features


class StockChallengerReplayError(ValueError):
    """Raised when the diagnostic replay cannot reproduce its source record."""


def write_turnover_band_replay(
    lineage_dir: Path,
    *,
    price_manifest_path: Path,
    universe_manifest_path: Path,
    config_path: Path,
    output_dir: Path,
    now: datetime | None = None,
) -> Path:
    """Replay a wider consensus hold band against an already observed lineage.

    This is deliberately post-selection diagnostic evidence. It must never be
    treated as prospective performance or spliced into the source lineage.
    """
    states = _load_states(lineage_dir / "states")
    terminal_state = states[-1][1]
    _require_terminal_binding(
        terminal_state,
        price_manifest_path,
        universe_manifest_path,
    )
    prices = _read_price_panel(price_manifest_path)
    universe = _read_universe(universe_manifest_path)
    current_tickers = set(universe["ticker"].astype(str))
    sector_by_ticker = dict(
        zip(
            universe["ticker"].astype(str),
            universe["gics_sector"].astype(str),
            strict=True,
        )
    )
    membership = pd.DataFrame(False, index=prices.index, columns=prices["Close"].columns)
    membership.loc[:, membership.columns.intersection(sorted(current_tickers))] = True
    features = build_stock_features(prices, membership)
    config, _ = load_stock_shadow_config(config_path)

    policies = {
        "primary_replay": ExitPolicy(
            hard_stop_fraction=config.hard_stop_fraction,
            atr_multiple=config.atr_multiple,
            maximum_holding_sessions=config.maximum_holding_sessions,
            rank_exit_multiple=config.rank_exit_multiple,
        ),
        "turnover_controlled_10_30": ExitPolicy(
            hard_stop_fraction=config.hard_stop_fraction,
            atr_multiple=config.atr_multiple,
            maximum_holding_sessions=config.maximum_holding_sessions,
            rank_exit_multiple=config.turnover_controlled_rank_exit_multiple,
        ),
        "rank_band_2_5x": ExitPolicy(
            hard_stop_fraction=config.hard_stop_fraction,
            atr_multiple=config.atr_multiple,
            maximum_holding_sessions=config.maximum_holding_sessions,
            rank_exit_multiple=2.5,
        ),
        "rank_band_3_5x": ExitPolicy(
            hard_stop_fraction=config.hard_stop_fraction,
            atr_multiple=config.atr_multiple,
            maximum_holding_sessions=config.maximum_holding_sessions,
            rank_exit_multiple=3.5,
        ),
        "rank_band_4x": ExitPolicy(
            hard_stop_fraction=config.hard_stop_fraction,
            atr_multiple=config.atr_multiple,
            maximum_holding_sessions=config.maximum_holding_sessions,
            rank_exit_multiple=4.0,
        ),
        "rank_band_5x": ExitPolicy(
            hard_stop_fraction=config.hard_stop_fraction,
            atr_multiple=config.atr_multiple,
            maximum_holding_sessions=config.maximum_holding_sessions,
            rank_exit_multiple=5.0,
        ),
        "rank_exit_disabled": ExitPolicy(
            hard_stop_fraction=config.hard_stop_fraction,
            atr_multiple=config.atr_multiple,
            maximum_holding_sessions=config.maximum_holding_sessions,
            rank_exit_multiple=config.rank_exit_multiple,
            use_rank_decay=False,
        ),
    }
    prior: dict[str, dict[str, object] | None] = dict.fromkeys(policies)
    execution_counts = dict.fromkeys(policies, 0)
    daily: list[dict[str, object]] = []
    primary_reproduction_errors: list[float] = []

    for state_path, state in states:
        session = pd.Timestamp(str(state["as_of_session"]))
        if session not in prices.index:
            raise StockChallengerReplayError(
                f"Replay price panel is missing source session {session.date()}."
            )
        inputs = state.get("inputs")
        if not isinstance(inputs, dict):
            raise StockChallengerReplayError(f"State has no bound inputs: {state_path.name}")
        candidate_path = lineage_dir / "candidates" / str(inputs["candidate_snapshot"])
        if not candidate_path.is_file() or not verify_candidate_snapshot(candidate_path):
            raise StockChallengerReplayError(
                f"Candidate snapshot is missing or invalid: {candidate_path.name}"
            )
        candidate = _read_json(candidate_path)
        if candidate.get("record_sha256") != inputs.get("candidate_record_sha256"):
            raise StockChallengerReplayError(
                f"State does not bind candidate record {candidate_path.name}."
            )
        candidates = _candidate_tickers(candidate, config.maximum_positions)
        day = features.xs(session, level="date")
        rank = _consensus_rank(day)
        blackout = _recorded_entry_blackout(state)
        row: dict[str, object] = {"session": session.date().isoformat()}
        for name, policy in policies.items():
            try:
                arm = _advance_arm(
                    name,
                    prior[name],
                    session,
                    prices,
                    day,
                    rank,
                    candidates,
                    current_tickers,
                    blackout,
                    guarded=False,
                    market_risk_on=True,
                    config=config,
                    policy=policy,
                    sector_by_ticker=sector_by_ticker,
                )
            except StockShadowStateError as exc:
                raise StockChallengerReplayError(
                    f"Replay failed at {session.date()} for {name}: {exc}"
                ) from exc
            prior[name] = arm
            executions = arm["executions_at_open"]
            account = arm["paper_account_at_close"]
            assert isinstance(executions, list)
            assert isinstance(account, dict)
            execution_counts[name] += len(executions)
            row[f"{name}_equity"] = float(account["total_equity"])
            row[f"{name}_cumulative_cost"] = float(account["cumulative_trading_cost"])
            row[f"{name}_executions"] = len(executions)
        canonical_account = state["arms"]["consensus"]["paper_account_at_close"]
        canonical_equity = float(canonical_account["total_equity"])
        row["canonical_primary_equity"] = canonical_equity
        error = abs(float(row["primary_replay_equity"]) - canonical_equity)
        primary_reproduction_errors.append(error)
        row["primary_reproduction_absolute_error"] = error
        row["source_state"] = state_path.name
        daily.append(row)

    primary = _terminal_account(prior["primary_replay"])
    challenger = _terminal_account(prior["turnover_controlled_10_30"])
    rank_exit_ablation = _terminal_account(prior["rank_exit_disabled"])
    canonical = terminal_state["arms"]["consensus"]["paper_account_at_close"]
    maximum_reproduction_error = max(primary_reproduction_errors)
    if maximum_reproduction_error > 1e-12:
        raise StockChallengerReplayError(
            "Primary replay did not reproduce the canonical lineage exactly; "
            f"maximum equity error was {maximum_reproduction_error:.12g}."
        )

    primary_return = float(primary["total_equity"]) - 1.0
    challenger_return = float(challenger["total_equity"]) - 1.0
    primary_cost = float(primary["cumulative_trading_cost"])
    challenger_cost = float(challenger["cumulative_trading_cost"])
    rank_exit_ablation_return = float(rank_exit_ablation["total_equity"]) - 1.0
    rank_exit_ablation_cost = float(rank_exit_ablation["cumulative_trading_cost"])
    recorded_at = (now or datetime.now(UTC)).astimezone(UTC)
    payload: dict[str, object] = {
        "schema_version": 1,
        "record_type": "post_launch_turnover_band_replay",
        "research_status": "diagnostic_post_selection_not_prospective",
        "action_authorized": False,
        "recorded_at_utc": recorded_at.isoformat(),
        "source_lineage_id": terminal_state["lineage_id"],
        "source_state_record_sha256": [state["record_sha256"] for _, state in states],
        "sessions": len(states),
        "first_session": states[0][1]["as_of_session"],
        "last_session": terminal_state["as_of_session"],
        "source_files": {
            "price_manifest": price_manifest_path.name,
            "price_manifest_sha256": file_sha256(price_manifest_path),
            "universe_manifest": universe_manifest_path.name,
            "universe_manifest_sha256": file_sha256(universe_manifest_path),
            "config": config_path.name,
            "config_sha256": file_sha256(config_path),
        },
        "implementation_sha256": implementation_sha256(),
        "method": {
            "common": (
                "exact frozen daily consensus candidate records, next-open execution, "
                "unchanged hard-loss, ATR-trail, trend, short-momentum, maximum-hold, "
                "sector-cap, and 50bp round-trip cost rules"
            ),
            "primary": {
                "entry_rank_threshold": config.maximum_positions,
                "hold_rank_threshold": config.maximum_positions * config.rank_exit_multiple,
                "rank_exit_multiple": config.rank_exit_multiple,
            },
            "challenger": {
                "entry_rank_threshold": config.maximum_positions,
                "hold_rank_threshold": (
                    config.maximum_positions * config.turnover_controlled_rank_exit_multiple
                ),
                "rank_exit_multiple": config.turnover_controlled_rank_exit_multiple,
            },
            "rank_exit_ablation": {
                "entry_rank_threshold": config.maximum_positions,
                "hold_rank_threshold": None,
                "rank_exit_enabled": False,
                "maximum_holding_sessions": config.maximum_holding_sessions,
            },
        },
        "primary_reproduction": {
            "passed": True,
            "maximum_absolute_equity_error": maximum_reproduction_error,
            "canonical_terminal_equity": float(canonical["total_equity"]),
            "replayed_terminal_equity": float(primary["total_equity"]),
            "canonical_cumulative_cost": float(canonical["cumulative_trading_cost"]),
            "replayed_cumulative_cost": primary_cost,
        },
        "comparison": {
            "primary_net_return": primary_return,
            "challenger_net_return": challenger_return,
            "challenger_return_difference_percentage_points": (challenger_return - primary_return)
            * 100.0,
            "primary_cumulative_modeled_cost": primary_cost,
            "challenger_cumulative_modeled_cost": challenger_cost,
            "modeled_cost_reduction": primary_cost - challenger_cost,
            "modeled_cost_reduction_fraction": (
                (primary_cost - challenger_cost) / primary_cost if primary_cost > 0.0 else None
            ),
            "primary_executions": execution_counts["primary_replay"],
            "challenger_executions": execution_counts["turnover_controlled_10_30"],
            "execution_reduction": (
                execution_counts["primary_replay"] - execution_counts["turnover_controlled_10_30"]
            ),
        },
        "rank_exit_ablation_comparison": {
            "primary_net_return": primary_return,
            "rank_exit_ablation_net_return": rank_exit_ablation_return,
            "return_difference_percentage_points": (rank_exit_ablation_return - primary_return)
            * 100.0,
            "primary_cumulative_modeled_cost": primary_cost,
            "rank_exit_ablation_cumulative_modeled_cost": rank_exit_ablation_cost,
            "modeled_cost_reduction": primary_cost - rank_exit_ablation_cost,
            "modeled_cost_reduction_fraction": (
                (primary_cost - rank_exit_ablation_cost) / primary_cost
                if primary_cost > 0.0
                else None
            ),
            "primary_executions": execution_counts["primary_replay"],
            "rank_exit_ablation_executions": execution_counts["rank_exit_disabled"],
            "execution_reduction": (
                execution_counts["primary_replay"] - execution_counts["rank_exit_disabled"]
            ),
        },
        "retention_band_sensitivity": _sensitivity_rows(
            policies,
            prior,
            execution_counts,
            maximum_positions=config.maximum_positions,
        ),
        "daily": daily,
        "limitations": [
            "The challenger was specified after inspecting the source lineage and is not a holdout.",
            "Retention-band sensitivity and the rank-exit ablation were inspected after launch and cannot select a winner.",
            "The terminal adjusted-price snapshot and terminal current roster were used to recompute features; this is not point-in-time historical-universe evidence.",
            "Exact reproduction of the primary account validates this launch-window replay only; it does not establish future excess return.",
            "The challenger remains diagnostic, long-only, paper-only, and incapable of placing orders.",
        ],
    }
    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    record_hash = hashlib.sha256(canonical_payload).hexdigest()
    payload["record_sha256"] = record_hash
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (
        f"turnover-band-replay-{states[0][1]['as_of_session'].replace('-', '')}-"
        f"{terminal_state['as_of_session'].replace('-', '')}-{record_hash[:12]}.json"
    )
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def verify_turnover_band_replay(path: Path) -> bool:
    payload = _read_json(path)
    expected = payload.pop("record_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return isinstance(expected, str) and hashlib.sha256(canonical).hexdigest() == expected


def _load_states(state_dir: Path) -> list[tuple[Path, dict[str, object]]]:
    paths = sorted(state_dir.glob("state-*.json")) if state_dir.exists() else []
    if len(paths) < 2:
        raise StockChallengerReplayError("A turnover replay requires at least two source states.")
    states: list[tuple[Path, dict[str, object]]] = []
    lineage_ids: set[str] = set()
    for path in paths:
        if not verify_stock_shadow_state(path):
            raise StockChallengerReplayError(f"Source state failed its content hash: {path.name}")
        payload = _read_json(path)
        lineage_ids.add(str(payload.get("lineage_id")))
        states.append((path, payload))
    if len(lineage_ids) != 1:
        raise StockChallengerReplayError("Source states do not share one lineage ID.")
    return states


def _require_terminal_binding(
    terminal_state: dict[str, object],
    price_manifest_path: Path,
    universe_manifest_path: Path,
) -> None:
    inputs = terminal_state.get("inputs")
    if not isinstance(inputs, dict):
        raise StockChallengerReplayError("Terminal state has no bound inputs.")
    expected = {
        price_manifest_path: (
            inputs.get("price_manifest"),
            inputs.get("price_manifest_sha256"),
        ),
        universe_manifest_path: (
            inputs.get("universe_manifest"),
            inputs.get("universe_manifest_sha256"),
        ),
    }
    for path, (name, digest) in expected.items():
        if path.name != name or not path.is_file() or file_sha256(path) != digest:
            raise StockChallengerReplayError(f"Terminal state does not bind exact manifest {path}.")


def _read_price_panel(manifest_path: Path) -> pd.DataFrame:
    manifest = _read_json(manifest_path)
    path = manifest_path.parent / str(manifest.get("data_file"))
    if not path.is_file() or file_sha256(path) != manifest.get("data_file_sha256"):
        raise StockChallengerReplayError("Replay price parquet failed its file hash.")
    prices = pd.read_parquet(path)
    prices.columns = pd.MultiIndex.from_tuples(
        prices.columns,
        names=["field", "ticker"],
    )
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index)).tz_localize(None)
    if tabular_sha256(prices) != manifest.get("tabular_sha256"):
        raise StockChallengerReplayError("Replay price panel failed its tabular hash.")
    return prices


def _read_universe(manifest_path: Path) -> pd.DataFrame:
    manifest = _read_json(manifest_path)
    path = manifest_path.parent / str(manifest.get("data_file"))
    if not path.is_file() or file_sha256(path) != manifest.get("data_file_sha256"):
        raise StockChallengerReplayError("Replay universe parquet failed its file hash.")
    universe = pd.read_parquet(path)
    if tabular_sha256(universe) != manifest.get("tabular_sha256"):
        raise StockChallengerReplayError("Replay universe failed its tabular hash.")
    required = {"ticker", "gics_sector"}
    if not required.issubset(universe.columns):
        raise StockChallengerReplayError("Replay universe is missing required columns.")
    return universe


def _candidate_tickers(candidate: dict[str, object], maximum: int) -> list[str]:
    rows = candidate.get("consensus_candidates")
    if not isinstance(rows, list):
        raise StockChallengerReplayError("Candidate record has no consensus rows.")
    ordered = sorted(
        (row for row in rows if isinstance(row, dict) and row.get("ticker")),
        key=lambda row: int(row["rank"]),
    )
    tickers = [str(row["ticker"]) for row in ordered[:maximum]]
    if not tickers or len(tickers) != len(set(tickers)):
        raise StockChallengerReplayError("Candidate record has an invalid consensus list.")
    return tickers


def _recorded_entry_blackout(state: dict[str, object]) -> set[str]:
    decisions = state["arms"]["consensus"].get("decisions", [])
    return {
        str(row["ticker"])
        for row in decisions
        if isinstance(row, dict)
        and row.get("action") == "SKIP"
        and "scheduled_earnings_entry_blackout" in row.get("reasons", [])
    }


def _sensitivity_rows(
    policies: dict[str, ExitPolicy],
    terminal_arms: dict[str, dict[str, object] | None],
    execution_counts: dict[str, int],
    *,
    maximum_positions: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name, policy in policies.items():
        account = _terminal_account(terminal_arms[name])
        hold_rank = maximum_positions * policy.rank_exit_multiple if policy.use_rank_decay else None
        rows.append(
            {
                "variant": name,
                "rank_exit_enabled": policy.use_rank_decay,
                "rank_exit_multiple": (
                    policy.rank_exit_multiple if policy.use_rank_decay else None
                ),
                "hold_rank_threshold": hold_rank,
                "net_return": float(account["total_equity"]) - 1.0,
                "cumulative_modeled_cost": float(account["cumulative_trading_cost"]),
                "executions": execution_counts[name],
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["hold_rank_threshold"] is None,
            float(row["hold_rank_threshold"] or 0.0),
        ),
    )


def _terminal_account(arm: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(arm, dict) or not isinstance(arm.get("paper_account_at_close"), dict):
        raise StockChallengerReplayError("Replay did not produce a terminal paper account.")
    return arm["paper_account_at_close"]


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise StockChallengerReplayError(f"Expected a JSON object: {path}")
    return value
