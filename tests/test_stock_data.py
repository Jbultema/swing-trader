from __future__ import annotations

import pandas as pd

from swing_trader.stock_data import audit_stock_coverage, write_stock_coverage_audit


def test_stock_coverage_audit_exposes_missing_historical_member() -> None:
    dates = pd.bdate_range("2024-01-02", periods=5)
    membership = pd.DataFrame({"LIVE": True, "DELISTED": True}, index=dates)
    close = pd.DataFrame({"LIVE": range(100, 105)}, index=dates)

    audit = audit_stock_coverage(close, membership, minimum_history_sessions=2)

    assert audit.status == "failed"
    assert audit.missing_tickers == ("DELISTED",)
    assert audit.expected_member_observations == 10
    assert audit.covered_member_observations == 5
    assert audit.member_observation_coverage == 0.5


def test_stock_coverage_audit_requires_signal_warmup() -> None:
    dates = pd.bdate_range("2024-01-02", periods=5)
    membership = pd.DataFrame({"A": True}, index=dates)
    close = pd.DataFrame({"A": range(100, 105)}, index=dates)

    audit = audit_stock_coverage(
        close,
        membership,
        minimum_history_sessions=3,
        minimum_warmup_coverage=0.5,
    )

    assert audit.status == "passed"
    assert audit.member_observation_coverage == 1.0
    assert audit.eligible_after_warmup_observations == 3
    assert audit.warmup_coverage == 0.6


def test_stock_coverage_audit_writes_machine_readable_evidence(tmp_path) -> None:
    dates = pd.bdate_range("2024-01-02", periods=3)
    membership = pd.DataFrame({"A": True}, index=dates)
    close = pd.DataFrame({"A": [100.0, 101.0, 102.0]}, index=dates)
    output = tmp_path / "audit.json"

    result = write_stock_coverage_audit(
        close,
        membership,
        output,
        minimum_history_sessions=1,
    )

    assert result.status == "passed"
    assert '"status": "passed"' in output.read_text()


def test_stock_coverage_warmup_counts_pre_membership_price_history() -> None:
    dates = pd.bdate_range("2024-01-02", periods=5)
    membership = pd.DataFrame({"A": [True, True]}, index=dates[-2:])
    close = pd.DataFrame({"A": range(100, 105)}, index=dates)

    audit = audit_stock_coverage(
        close,
        membership,
        minimum_history_sessions=3,
        minimum_warmup_coverage=1.0,
    )

    assert audit.status == "passed"
    assert audit.warmup_coverage == 1.0
