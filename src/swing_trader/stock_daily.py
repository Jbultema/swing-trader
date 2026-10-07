from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from swing_trader.alpha_validation import (
    AlphaValidationError,
    latest_alpha_validation_for_candidate,
    validate_candidate_snapshot_with_alpha,
)
from swing_trader.data import MarketDataError
from swing_trader.events import download_alpha_earnings_calendar
from swing_trader.finra_activity import (
    FinraActivityError,
    download_candidate_finra_activity,
    finra_activity_for_candidate,
)
from swing_trader.provenance import file_sha256
from swing_trader.sec_filing_events import (
    DEFAULT_SEC_USER_AGENT,
    SecFilingEventError,
    download_candidate_sec_events,
    sec_events_for_candidate,
)
from swing_trader.stock_candidates import record_current_stock_candidates
from swing_trader.stock_live_data import download_current_stock_prices
from swing_trader.stock_prospective import write_stock_shadow_evaluation
from swing_trader.stock_shadow_state import (
    held_tickers_from_latest_state,
    latest_stock_shadow_state,
    record_stock_shadow_state,
    stock_shadow_lineage_dir,
    stock_shadow_lineage_id,
    verify_stock_shadow_state,
)
from swing_trader.stock_universe import download_current_sp500_snapshot


class StockDailyError(ValueError):
    """Raised when an automated stock shadow cannot advance honestly."""


@dataclass(frozen=True)
class StockDailyResult:
    status: str
    lineage_id: str
    session: str
    state_path: Path | None
    run_record_path: Path
    alpha_status: str
    earnings_status: str
    finra_status: str
    sec_status: str
    evaluation_path: Path | None


def run_stock_shadow_daily(
    root: Path,
    *,
    archive_root: Path | None = None,
    api_key: str = "",
    sec_user_agent: str = DEFAULT_SEC_USER_AGENT,
    now: datetime | None = None,
) -> StockDailyResult:
    """Refresh public inputs and advance one no-paid, non-executable paper state."""
    recorded_at = _as_utc(now or datetime.now(UTC))
    config_path = root / "config/stock_shadow.toml"
    lineage_id = stock_shadow_lineage_id(config_path)
    lineage_dir = stock_shadow_lineage_dir(
        root / "reports/stock-shadow/lineages",
        config_path,
    )
    state_dir = lineage_dir / "states"
    if archive_root is not None:
        _stage_archived_states(
            archive_root / "lineages" / lineage_id / "states",
            state_dir,
        )
    held = held_tickers_from_latest_state(state_dir)

    universe = download_current_sp500_snapshot(
        root / "data/stock-shadow/universe",
        captured_at=recorded_at,
    )
    prices = download_current_stock_prices(
        universe.manifest_path,
        root / "data/stock-shadow/prices",
        extra_tickers=held,
        now=recorded_at,
    )
    if prices.validation.latest_session is None:
        raise StockDailyError("Current price snapshot has no completed session.")
    session = prices.validation.latest_session
    previous_path = latest_stock_shadow_state(state_dir)
    if previous_path is not None:
        previous = _read_json(previous_path)
        prior_session = str(previous.get("as_of_session"))
        if prior_session == session:
            finra_status, finra_path, finra_diagnostic = _recover_finra_for_existing_state(
                root,
                lineage_dir,
                previous,
                recorded_at,
            )
            sec_status, sec_path, sec_diagnostic = _recover_sec_for_existing_state(
                root,
                lineage_dir,
                previous,
                recorded_at,
                sec_user_agent,
            )
            diagnostics = [
                value for value in (finra_diagnostic, sec_diagnostic) if value is not None
            ]
            run_record = _write_run_record(
                lineage_dir,
                recorded_at,
                {
                    "status": "no_new_completed_session",
                    "lineage_id": lineage_id,
                    "session": session,
                    "previous_state": previous_path.name,
                    "finra_status": finra_status,
                    "finra_activity": None if finra_path is None else finra_path.name,
                    "finra_activity_file_sha256": (
                        None if finra_path is None else file_sha256(finra_path)
                    ),
                    "sec_status": sec_status,
                    "sec_events": None if sec_path is None else sec_path.name,
                    "sec_events_file_sha256": (
                        None if sec_path is None else file_sha256(sec_path)
                    ),
                    "diagnostics": diagnostics,
                    "data_cost_policy": "no_paid_sources",
                    "action_authorized": False,
                },
            )
            return StockDailyResult(
                "no_new_completed_session",
                lineage_id,
                session,
                None,
                run_record,
                "not_called",
                "not_called",
                finra_status,
                sec_status,
                None,
            )
        _require_consecutive_session(previous, prices.manifest_path, session)

    candidate = record_current_stock_candidates(
        universe.manifest_path,
        prices.manifest_path,
        lineage_dir / "candidates",
        required_validation_symbols=held,
        now=recorded_at,
    )
    finra_status, finra_path, finra_diagnostic = _record_or_reuse_finra_activity(
        candidate.path,
        universe.manifest_path,
        prices.manifest_path,
        lineage_dir / "finra-activity",
        recorded_at,
    )
    sec_status, sec_path, sec_diagnostic = _record_or_reuse_sec_events(
        candidate.path,
        universe.manifest_path,
        prices.manifest_path,
        lineage_dir / "sec-events",
        recorded_at,
        sec_user_agent,
    )
    earnings_path: Path | None = None
    earnings_status = "key_not_configured"
    alpha_status = "key_not_configured"
    diagnostics: list[dict[str, str]] = [
        value for value in (finra_diagnostic, sec_diagnostic) if value is not None
    ]
    if api_key.strip():
        try:
            earnings_path = download_alpha_earnings_calendar(
                api_key,
                lineage_dir / "earnings",
                horizon="3month",
                now=recorded_at,
            )
            earnings_status = "passed"
        except (MarketDataError, OSError, ValueError) as exc:
            earnings_status = "failed"
            diagnostics.append({"step": "earnings_calendar", "error": str(exc)})

        try:
            validate_candidate_snapshot_with_alpha(
                candidate.path,
                api_key,
                lineage_dir / "alpha-validation",
                quota_dir=root / "data/stock-shadow/provider-quota/alpha-vantage",
                now=recorded_at,
            )
            alpha_status = "passed"
        except AlphaValidationError as exc:
            alpha_status = "failed"
            diagnostics.append({"step": "candidate_price_validation", "error": str(exc)})

    alpha_path = latest_alpha_validation_for_candidate(
        lineage_dir / "alpha-validation",
        candidate.path,
    )
    state = record_stock_shadow_state(
        candidate.path,
        universe.manifest_path,
        prices.manifest_path,
        config_path,
        state_dir,
        alpha_validation_path=alpha_path,
        earnings_path=earnings_path,
        now=recorded_at,
    )
    evaluation_path = write_stock_shadow_evaluation(
        state_dir,
        lineage_dir / "evaluations",
        now=recorded_at,
    )
    run_record = _write_run_record(
        lineage_dir,
        recorded_at,
        {
            "status": "state_recorded",
            "lineage_id": lineage_id,
            "session": session,
            "state": state.path.name,
            "state_record_sha256": state.record_sha256,
            "evaluation": evaluation_path.name,
            "evaluation_file_sha256": file_sha256(evaluation_path),
            "candidate": candidate.path.name,
            "candidate_record_sha256": candidate.record_sha256,
            "alpha_status": alpha_status,
            "earnings_status": earnings_status,
            "finra_status": finra_status,
            "finra_activity": None if finra_path is None else finra_path.name,
            "finra_activity_file_sha256": (
                None if finra_path is None else file_sha256(finra_path)
            ),
            "sec_status": sec_status,
            "sec_events": None if sec_path is None else sec_path.name,
            "sec_events_file_sha256": None if sec_path is None else file_sha256(sec_path),
            "diagnostics": diagnostics,
            "data_cost_policy": "no_paid_sources",
            "action_authorized": False,
        },
    )
    return StockDailyResult(
        "state_recorded",
        lineage_id,
        session,
        state.path,
        run_record,
        alpha_status,
        earnings_status,
        finra_status,
        sec_status,
        evaluation_path,
    )


def _stage_archived_states(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    paths = sorted(source.glob("state-*.json"))
    if not paths:
        return
    destination.mkdir(parents=True, exist_ok=True)
    previous_name: str | None = None
    previous_hash: str | None = None
    for position, path in enumerate(paths):
        if not verify_stock_shadow_state(path):
            raise StockDailyError(f"Archived stock state failed its hash check: {path.name}")
        payload = _read_json(path)
        if position == 0:
            if payload.get("initialization") is not True:
                raise StockDailyError("Archived stock lineage does not begin with initialization.")
        elif (
            payload.get("previous_record") != previous_name
            or payload.get("previous_record_sha256") != previous_hash
        ):
            raise StockDailyError("Archived stock state lineage has a broken prior-record chain.")
        target = destination / path.name
        if target.exists() and file_sha256(target) != file_sha256(path):
            raise StockDailyError(f"Local and archived states disagree: {path.name}")
        if not target.exists():
            shutil.copy2(path, target)
        previous_name = path.name
        previous_hash = str(payload["record_sha256"])


def _record_or_reuse_finra_activity(
    candidate_path: Path,
    universe_manifest_path: Path,
    price_manifest_path: Path,
    output_dir: Path,
    recorded_at: datetime,
) -> tuple[str, Path | None, dict[str, str] | None]:
    try:
        existing = finra_activity_for_candidate(output_dir, candidate_path)
        if existing is not None:
            return "already_passed_experimental_context_only", existing, None
        snapshot = download_candidate_finra_activity(
            candidate_path,
            universe_manifest_path,
            price_manifest_path,
            output_dir,
            now=recorded_at,
        )
        return "passed_experimental_context_only", snapshot.manifest_path, None
    except (FinraActivityError, OSError, ValueError) as exc:
        return (
            "failed_nonblocking_experimental",
            None,
            {"step": "finra_activity", "error": str(exc)},
        )


def _recover_finra_for_existing_state(
    root: Path,
    lineage_dir: Path,
    state: dict[str, object],
    recorded_at: datetime,
) -> tuple[str, Path | None, dict[str, str] | None]:
    inputs = state.get("inputs")
    if not isinstance(inputs, dict):
        return (
            "failed_nonblocking_experimental",
            None,
            {"step": "finra_activity", "error": "Existing state has no input bindings."},
        )
    try:
        candidate_path = lineage_dir / "candidates" / str(inputs["candidate_snapshot"])
        universe_path = (
            root / "data/stock-shadow/universe" / str(inputs["universe_manifest"])
        )
        price_path = root / "data/stock-shadow/prices" / str(inputs["price_manifest"])
    except KeyError as exc:
        return (
            "failed_nonblocking_experimental",
            None,
            {"step": "finra_activity", "error": f"Existing state input is missing: {exc}"},
        )
    return _record_or_reuse_finra_activity(
        candidate_path,
        universe_path,
        price_path,
        lineage_dir / "finra-activity",
        recorded_at,
    )


def _record_or_reuse_sec_events(
    candidate_path: Path,
    universe_manifest_path: Path,
    price_manifest_path: Path,
    output_dir: Path,
    recorded_at: datetime,
    user_agent: str,
) -> tuple[str, Path | None, dict[str, str] | None]:
    try:
        existing = sec_events_for_candidate(output_dir, candidate_path)
        if existing is not None:
            return "already_passed_experimental_context_only", existing, None
        snapshot = download_candidate_sec_events(
            candidate_path,
            universe_manifest_path,
            price_manifest_path,
            output_dir,
            user_agent=user_agent,
            now=recorded_at,
        )
        return "passed_experimental_context_only", snapshot.manifest_path, None
    except (SecFilingEventError, OSError, ValueError) as exc:
        return (
            "failed_nonblocking_experimental",
            None,
            {"step": "sec_filing_events", "error": str(exc)},
        )


def _recover_sec_for_existing_state(
    root: Path,
    lineage_dir: Path,
    state: dict[str, object],
    recorded_at: datetime,
    user_agent: str,
) -> tuple[str, Path | None, dict[str, str] | None]:
    inputs = state.get("inputs")
    if not isinstance(inputs, dict):
        return (
            "failed_nonblocking_experimental",
            None,
            {"step": "sec_filing_events", "error": "Existing state has no input bindings."},
        )
    try:
        candidate_path = lineage_dir / "candidates" / str(inputs["candidate_snapshot"])
        universe_path = (
            root / "data/stock-shadow/universe" / str(inputs["universe_manifest"])
        )
        price_path = root / "data/stock-shadow/prices" / str(inputs["price_manifest"])
    except KeyError as exc:
        return (
            "failed_nonblocking_experimental",
            None,
            {"step": "sec_filing_events", "error": f"Existing state input is missing: {exc}"},
        )
    return _record_or_reuse_sec_events(
        candidate_path,
        universe_path,
        price_path,
        lineage_dir / "sec-events",
        recorded_at,
        user_agent,
    )


def _require_consecutive_session(
    previous: dict[str, object],
    price_manifest_path: Path,
    current_session: str,
) -> None:
    manifest = _read_json(price_manifest_path)
    data_path = price_manifest_path.parent / str(manifest["data_file"])
    sessions = pd.DatetimeIndex(pd.read_parquet(data_path, columns=[]).index)
    prior = pd.Timestamp(previous["as_of_session"])
    current = pd.Timestamp(current_session)
    if prior not in sessions or current not in sessions:
        raise StockDailyError("Stock-state sessions are absent from the current price snapshot.")
    if int(sessions.get_loc(current)) - int(sessions.get_loc(prior)) != 1:
        raise StockDailyError(
            "A completed stock session was missed; manual reconciliation or an explicit new "
            "lineage is required."
        )


def _write_run_record(
    lineage_dir: Path,
    recorded_at: datetime,
    fields: dict[str, object],
) -> Path:
    payload = {
        "schema_version": 1,
        "record_type": "stock_shadow_automation_run",
        "recorded_at_utc": recorded_at.isoformat(),
        **fields,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    record_hash = hashlib.sha256(canonical).hexdigest()
    payload["record_sha256"] = record_hash
    output_dir = lineage_dir / "runs"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = recorded_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"run-{stamp}-{record_hash[:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise StockDailyError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
