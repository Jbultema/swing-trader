from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from time import sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from swing_trader.data import ALPHA_VANTAGE_URL
from swing_trader.stock_candidates import verify_candidate_snapshot

ALPHA_DAILY_FREE_CALL_LIMIT = 25


class AlphaValidationError(ValueError):
    """Raised when a free-tier independent validation snapshot is incomplete."""


@dataclass(frozen=True)
class AlphaCandidateValidation:
    status: str
    symbols_requested: int
    symbols_matched: int
    expected_session: str
    maximum_close_difference_fraction: float
    tolerance_fraction: float
    stale_symbols: tuple[str, ...]
    divergent_symbols: tuple[str, ...]
    missing_primary_symbols: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


ResponseOpener = Callable[..., object]


def validate_candidate_snapshot_with_alpha(
    candidate_path: Path,
    api_key: str,
    output_dir: Path,
    *,
    quota_dir: Path,
    daily_call_budget: int = 24,
    tolerance_fraction: float = 0.005,
    opener: ResponseOpener = urlopen,
    request_interval_seconds: float = 1.1,
    sleeper: Callable[[float], None] = sleep,
    now: datetime | None = None,
) -> Path:
    """Independently check only actionable candidates within Alpha's free quota.

    The snapshot intentionally validates the finite candidate/benchmark set rather
    than spending scarce free calls on names the strategy cannot hold tomorrow.
    """
    if not api_key.strip():
        raise AlphaValidationError("ALPHA_VANTAGE_API_KEY is empty.")
    if not verify_candidate_snapshot(candidate_path):
        raise AlphaValidationError("Candidate snapshot failed its content-hash check.")
    candidate = _read_json(candidate_path)
    raw_symbols = candidate.get("validation_symbols")
    primary_raw = candidate.get("primary_latest_close")
    if not isinstance(raw_symbols, list) or not isinstance(primary_raw, dict):
        raise AlphaValidationError("Candidate snapshot is missing validation inputs.")
    symbols = tuple(str(value) for value in raw_symbols)
    if len(symbols) > daily_call_budget:
        raise AlphaValidationError(
            f"Candidate validation needs {len(symbols)} calls but the reserved budget is "
            f"{daily_call_budget}."
        )
    if daily_call_budget > ALPHA_DAILY_FREE_CALL_LIMIT:
        raise ValueError("Alpha Vantage daily call budget cannot exceed the documented free limit.")
    checked_at = _as_utc(now or datetime.now(UTC))
    _reserve_calls(
        quota_dir,
        checked_at.date(),
        len(symbols),
        daily_limit=ALPHA_DAILY_FREE_CALL_LIMIT,
        purpose=f"candidate_validation:{candidate_path.name}",
    )

    series: dict[str, pd.DataFrame] = {}
    raw_hashes: dict[str, str] = {}
    for position, symbol in enumerate(symbols):
        if position and request_interval_seconds > 0:
            sleeper(request_interval_seconds)
        query = urlencode(
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "compact",
                "datatype": "csv",
                "apikey": api_key,
            }
        )
        request = Request(
            f"{ALPHA_VANTAGE_URL}?{query}",
            headers={"User-Agent": "swing-trader/0.1"},
        )
        try:
            with opener(request, timeout=30) as response:  # type: ignore[attr-defined]
                payload = response.read()  # type: ignore[attr-defined]
        except Exception:
            raise AlphaValidationError(f"Alpha Vantage request failed for {symbol}.") from None
        series[symbol] = parse_alpha_daily_csv(payload, symbol)
        raw_hashes[symbol] = hashlib.sha256(payload).hexdigest()

    expected_session = date.fromisoformat(str(candidate["as_of_session"]))
    primary = {str(key): float(value) for key, value in primary_raw.items()}
    rows: list[dict[str, object]] = []
    stale: list[str] = []
    divergent: list[str] = []
    missing_primary: list[str] = []
    maximum_difference = 0.0
    for symbol in symbols:
        frame = series[symbol]
        latest = pd.Timestamp(frame.index.max()).date()
        alpha_close = float(frame.loc[pd.Timestamp(latest), "close"])
        primary_close = primary.get(symbol)
        difference = None
        if primary_close is None:
            missing_primary.append(symbol)
        else:
            difference = abs(alpha_close - primary_close) / primary_close
            maximum_difference = max(maximum_difference, difference)
            if difference > tolerance_fraction:
                divergent.append(symbol)
        if latest != expected_session:
            stale.append(symbol)
        rows.append(
            {
                "symbol": symbol,
                "alpha_latest_session": latest.isoformat(),
                "alpha_close": alpha_close,
                "primary_close": primary_close,
                "absolute_close_difference_fraction": difference,
                "passed": (
                    latest == expected_session
                    and difference is not None
                    and difference <= tolerance_fraction
                ),
            }
        )
    passed = not stale and not divergent and not missing_primary and len(rows) == len(symbols)
    validation = AlphaCandidateValidation(
        status="passed" if passed else "failed",
        symbols_requested=len(symbols),
        symbols_matched=sum(bool(row["passed"]) for row in rows),
        expected_session=expected_session.isoformat(),
        maximum_close_difference_fraction=maximum_difference,
        tolerance_fraction=tolerance_fraction,
        stale_symbols=tuple(sorted(stale)),
        divergent_symbols=tuple(sorted(divergent)),
        missing_primary_symbols=tuple(sorted(missing_primary)),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = checked_at.strftime("%Y%m%dT%H%M%S%fZ")
    raw_fingerprint = hashlib.sha256(
        json.dumps(raw_hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = output_dir / f"alpha-candidates-{stamp}-{raw_fingerprint[:12]}.json"
    output = {
        "schema_version": 1,
        "provider": "Alpha Vantage",
        "data_cost_policy": "no_paid_sources",
        "endpoint": "TIME_SERIES_DAILY",
        "provider_tier": "free_25_calls_per_day",
        "role": "candidate_and_benchmark_independent_validation_only",
        "action_authorized": False,
        "checked_at_utc": checked_at.isoformat(),
        "candidate_snapshot": candidate_path.name,
        "candidate_record_sha256": candidate["record_sha256"],
        "raw_response_sha256": raw_hashes,
        "validation": validation.to_dict(),
        "symbol_checks": rows,
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    if not validation.passed:
        raise AlphaValidationError(
            f"Alpha candidate validation failed; evidence retained at {path}."
        )
    return path


def parse_alpha_daily_csv(payload: bytes, symbol: str) -> pd.DataFrame:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AlphaValidationError(f"Alpha Vantage returned non-UTF-8 data for {symbol}.") from exc
    if text.lstrip().startswith("{"):
        raise AlphaValidationError(
            f"Alpha Vantage returned a quota, entitlement, or provider error for {symbol}."
        )
    reader = csv.DictReader(io.StringIO(text))
    expected = ("timestamp", "open", "high", "low", "close", "volume")
    if tuple(reader.fieldnames or ()) != expected:
        raise AlphaValidationError(f"Alpha Vantage daily schema changed for {symbol}.")
    raw = pd.DataFrame(reader)
    if raw.empty:
        raise AlphaValidationError(f"Alpha Vantage returned no daily rows for {symbol}.")
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], errors="coerce")
    for field in expected[1:]:
        raw[field] = pd.to_numeric(raw[field], errors="coerce")
    if raw[list(expected)].isna().any().any():
        raise AlphaValidationError(f"Alpha Vantage returned malformed daily rows for {symbol}.")
    frame = raw.set_index("timestamp").sort_index()
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise AlphaValidationError(f"Alpha Vantage daily dates are invalid for {symbol}.")
    return frame


def _reserve_calls(
    quota_dir: Path,
    day: date,
    calls: int,
    *,
    daily_limit: int,
    purpose: str,
) -> Path:
    if calls < 0:
        raise ValueError("Reserved calls cannot be negative.")
    quota_dir.mkdir(parents=True, exist_ok=True)
    path = quota_dir / f"{day.isoformat()}.json"
    if path.exists():
        payload = _read_json(path)
    else:
        payload = {
            "date": day.isoformat(),
            "provider": "Alpha Vantage",
            "documented_free_daily_limit": daily_limit,
            "assumption": "ledger_cannot_observe_calls_made_outside_this_project",
            "reservations": [],
        }
    reservations = payload.get("reservations")
    if not isinstance(reservations, list):
        raise AlphaValidationError("Alpha quota ledger has an invalid schema.")
    used = sum(int(item["calls"]) for item in reservations if isinstance(item, dict))
    if used + calls > daily_limit:
        raise AlphaValidationError(
            f"Alpha quota ledger has {daily_limit - used} calls left; {calls} requested."
        )
    reservations.append(
        {
            "recorded_at_utc": datetime.now(UTC).isoformat(),
            "calls": calls,
            "purpose": purpose,
        }
    )
    payload["reserved_calls"] = used + calls
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AlphaValidationError(f"Expected JSON object: {path}")
    return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
