from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from swing_trader.prospective import evaluate_prospective_records
from swing_trader.shadow import record_shadow_snapshot


def test_prospective_evaluation_deduplicates_and_excludes_failed_gates(
    tmp_path: Path,
) -> None:
    dates = pd.bdate_range("2026-01-02", periods=90)
    prices = pd.DataFrame(
        {
            ("Open", "A"): 100.0 * np.power(1.01, np.arange(len(dates))),
            ("Open", "SPY"): 100.0 * np.power(1.005, np.arange(len(dates))),
        },
        index=dates,
    )
    prices.columns = pd.MultiIndex.from_tuples(prices.columns, names=["field", "ticker"])
    reports = tmp_path / "latest"
    records = tmp_path / "records"
    reports.mkdir()

    _record(
        reports,
        records,
        created_at="2026-01-05T23:00:00+00:00",
        as_of="2026-01-05",
        monthly_decision="2026-01-05",
        gate=True,
    )
    _record(
        reports,
        records,
        created_at="2026-01-06T23:00:00+00:00",
        as_of="2026-01-06",
        monthly_decision="2026-01-05",
        gate=True,
    )
    _record(
        reports,
        records,
        created_at="2026-02-03T23:00:00+00:00",
        as_of="2026-02-03",
        monthly_decision="2026-02-02",
        gate=False,
    )

    result = evaluate_prospective_records(records, prices)

    assert result["records_seen"] == 3
    assert result["unique_schema_v3_decisions"] == 2
    summary = result["eligible_summary"]
    assert summary["eligible_unique_decisions"] == 1
    assert summary["by_horizon"]["5"]["matured_decisions"] == 1
    first = result["outcomes"][0]
    expected = (1.01**5 - 1.0) - 0.001
    assert first["horizons"]["5"]["model_net_return"] == pytest.approx(expected)


def _record(
    reports: Path,
    records: Path,
    *,
    created_at: str,
    as_of: str,
    monthly_decision: str,
    gate: bool,
) -> None:
    specification = {
        "system": "candidate",
        "config": {
            "data": {"benchmark": "SPY"},
            "execution": {"transaction_cost_bps": 10.0},
        },
    }
    specification_hash = hashlib.sha256(
        json.dumps(specification, sort_keys=True, default=list).encode()
    ).hexdigest()
    _write(
        reports / "manifest.json",
        {
            "created_at_utc": created_at,
            "system": "candidate",
            "specification": specification,
            "specification_sha256": specification_hash,
            "implementation_sha256": "implementation-v1",
            "research_status": "research_only",
        },
    )
    _write(
        reports / "latest_decisions.json",
        {
            "as_of_close": as_of,
            "last_monthly_decision": monthly_decision,
            "action_authorized": False,
            "hypothetical_actions": [
                {
                    "ticker": "A",
                    "current_weight": 0.0,
                    "target_weight": 1.0,
                }
            ],
        },
    )
    _write(
        reports / "data_quality.json",
        {"status": "passed" if gate else "failed", "decision_data_gate_passed": gate},
    )
    record_shadow_snapshot(reports, records)


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
