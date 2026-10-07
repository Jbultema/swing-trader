from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from swing_trader.stock_candidates import screen_latest_candidates, verify_candidate_snapshot


def test_candidate_screen_prioritizes_cross_family_agreement_for_free_validation() -> None:
    date = pd.Timestamp("2026-10-06")
    index = pd.MultiIndex.from_product(
        [[date], ["A", "B", "C"]], names=["date", "ticker"]
    )
    features = pd.DataFrame(
        {
            "eligible": True,
            "trend_positive": True,
            "close": [110.0, 105.0, 100.0],
            "return_21d": [0.3, 0.2, 0.1],
            "return_63d": [0.3, 0.2, 0.1],
            "return_12_1": [0.3, 0.2, 0.1],
            "proximity_52w_high": [1.0, 0.9, 0.8],
            "volume_ratio_20_126": [2.0, 1.5, 1.0],
            "atr_fraction_14d": 0.02,
            "score_short_volume": [0.9, 0.8, 0.1],
            "score_smooth_momentum": [0.95, 0.2, 0.8],
            "score_volume_breakout": [0.9, 0.8, 0.1],
        },
        index=index,
    )

    rows, consensus, validation = screen_latest_candidates(
        features,
        date,
        families=("short_volume", "smooth_momentum", "volume_breakout"),
        top_n=2,
        validation_symbol_limit=3,
    )

    assert len(rows) == 6
    assert validation == ["A", "B", "SPY"]
    assert [row["ticker"] for row in consensus] == ["A", "B"]
    assert consensus[0]["agreement_count"] == 3
    assert all(row["why"] for row in rows)


def test_candidate_snapshot_hash_detects_tampering(tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "as_of_session": "2026-10-06",
        "validation_symbols": ["A", "SPY"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["record_sha256"] = hashlib.sha256(canonical).hexdigest()
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(payload))

    assert verify_candidate_snapshot(path)
    payload["as_of_session"] = "2026-10-07"
    path.write_text(json.dumps(payload))
    assert not verify_candidate_snapshot(path)
