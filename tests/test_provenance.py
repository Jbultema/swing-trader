from __future__ import annotations

from pathlib import Path

import pandas as pd

from swing_trader.provenance import (
    PUBLISHED_COMPARATOR_SOURCE_FILES,
    STOCK_EVALUATION_SOURCE_FILES,
    STOCK_POLICY_SOURCE_FILES,
    file_sha256,
    published_comparator_sha256,
    stock_evaluation_sha256,
    stock_policy_sha256,
    tabular_sha256,
)


def test_tabular_fingerprint_changes_with_values_labels_and_types() -> None:
    frame = pd.DataFrame(
        {"A": [1.0, 2.0], "B": [3.0, float("nan")]},
        index=pd.DatetimeIndex(["2025-01-02", "2025-01-03"], name="date"),
    )
    baseline = tabular_sha256(frame)

    assert tabular_sha256(frame.copy()) == baseline
    changed_value = frame.copy()
    changed_value.iloc[0, 0] = 1.1
    assert tabular_sha256(changed_value) != baseline
    assert tabular_sha256(frame.rename(columns={"A": "C"})) != baseline
    assert tabular_sha256(frame.astype("float32")) != baseline


def test_file_fingerprint_changes_with_bytes(tmp_path) -> None:
    path = tmp_path / "evidence.txt"
    path.write_text("first\n", encoding="utf-8")
    baseline = file_sha256(path)

    path.write_text("second\n", encoding="utf-8")

    assert file_sha256(path) != baseline


def test_stock_policy_hash_ignores_dashboard_and_diagnostic_collectors(
    tmp_path: Path,
) -> None:
    for name in {*STOCK_POLICY_SOURCE_FILES, *STOCK_EVALUATION_SOURCE_FILES}:
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    for name in ("dashboard.py", "finra_activity.py", "sec_filing_events.py"):
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    policy = stock_policy_sha256(tmp_path)
    evaluation = stock_evaluation_sha256(tmp_path)

    (tmp_path / "dashboard.py").write_text("# changed UI\n", encoding="utf-8")
    (tmp_path / "finra_activity.py").write_text("# changed diagnostic\n", encoding="utf-8")
    (tmp_path / "sec_filing_events.py").write_text("# changed diagnostic\n", encoding="utf-8")

    assert stock_policy_sha256(tmp_path) == policy
    assert stock_evaluation_sha256(tmp_path) == evaluation


def test_stock_policy_and_evaluation_hashes_change_only_for_their_domains(
    tmp_path: Path,
) -> None:
    for name in {*STOCK_POLICY_SOURCE_FILES, *STOCK_EVALUATION_SOURCE_FILES}:
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    policy = stock_policy_sha256(tmp_path)
    evaluation = stock_evaluation_sha256(tmp_path)

    (tmp_path / "stock_signals.py").write_text("# changed signal\n", encoding="utf-8")
    assert stock_policy_sha256(tmp_path) != policy
    assert stock_evaluation_sha256(tmp_path) == evaluation

    (tmp_path / "stock_prospective.py").write_text("# changed evaluator\n", encoding="utf-8")
    assert stock_evaluation_sha256(tmp_path) != evaluation


def test_published_comparator_hash_ignores_dashboard_but_tracks_method_code(
    tmp_path: Path,
) -> None:
    for name in PUBLISHED_COMPARATOR_SOURCE_FILES:
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    (tmp_path / "dashboard.py").write_text("# dashboard\n", encoding="utf-8")
    baseline = published_comparator_sha256(tmp_path)

    (tmp_path / "dashboard.py").write_text("# changed dashboard\n", encoding="utf-8")
    assert published_comparator_sha256(tmp_path) == baseline

    (tmp_path / "published_momentum.py").write_text("# changed method\n", encoding="utf-8")
    assert published_comparator_sha256(tmp_path) != baseline
