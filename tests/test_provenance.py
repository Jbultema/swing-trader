from __future__ import annotations

import pandas as pd

from swing_trader.provenance import file_sha256, tabular_sha256


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
