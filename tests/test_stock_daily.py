from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from swing_trader.stock_daily import (
    StockDailyError,
    _record_or_reuse_finra_activity,
    _require_consecutive_session,
    _stage_archived_states,
)


def test_missing_finra_retry_inputs_remain_nonblocking(tmp_path: Path) -> None:
    status, path, diagnostic = _record_or_reuse_finra_activity(
        tmp_path / "missing-candidate.json",
        tmp_path / "missing-universe.json",
        tmp_path / "missing-prices.json",
        tmp_path / "finra",
        pd.Timestamp("2026-10-07T20:00:00Z").to_pydatetime(),
    )

    assert status == "failed_nonblocking_experimental"
    assert path is None
    assert diagnostic is not None
    assert diagnostic["step"] == "finra_activity"


def test_archived_state_staging_verifies_hashes_and_prior_chain(tmp_path: Path) -> None:
    source = tmp_path / "archive"
    destination = tmp_path / "local"
    source.mkdir()
    first = _write_state(
        source / "state-20261005-a.json",
        {
            "initialization": True,
            "as_of_session": "2026-10-05",
            "previous_record": None,
            "previous_record_sha256": None,
        },
    )
    first_payload = json.loads(first.read_text())
    second = _write_state(
        source / "state-20261006-b.json",
        {
            "initialization": False,
            "as_of_session": "2026-10-06",
            "previous_record": first.name,
            "previous_record_sha256": first_payload["record_sha256"],
        },
    )

    _stage_archived_states(source, destination)

    assert sorted(path.name for path in destination.glob("*.json")) == [
        first.name,
        second.name,
    ]

    payload = json.loads(second.read_text())
    payload["previous_record_sha256"] = "wrong"
    second.write_text(json.dumps(payload))
    with pytest.raises(StockDailyError, match="hash check"):
        _stage_archived_states(source, tmp_path / "other")


def test_daily_transition_accepts_next_session_and_rejects_a_gap(tmp_path: Path) -> None:
    sessions = pd.bdate_range("2026-10-05", periods=3)
    prices = pd.DataFrame({"placeholder": [1.0, 2.0, 3.0]}, index=sessions)
    data_path = tmp_path / "prices.parquet"
    prices.to_parquet(data_path)
    manifest_path = tmp_path / "prices.manifest.json"
    manifest_path.write_text(json.dumps({"data_file": data_path.name}))
    previous = {"as_of_session": sessions[0].date().isoformat()}

    _require_consecutive_session(previous, manifest_path, sessions[1].date().isoformat())
    with pytest.raises(StockDailyError, match="session was missed"):
        _require_consecutive_session(previous, manifest_path, sessions[2].date().isoformat())


def _write_state(path: Path, fields: dict[str, object]) -> Path:
    payload = {
        "schema_version": 1,
        "record_type": "prospective_stock_shadow_state",
        **fields,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
