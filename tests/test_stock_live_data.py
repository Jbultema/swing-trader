from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from swing_trader.stock_live_data import (
    _normalize_yahoo_batch,
    _safe_yahoo_end_date,
    audit_current_stock_price_snapshot,
    validate_current_stock_prices,
    write_current_stock_price_snapshot,
)


def test_stock_price_validation_quantifies_current_and_signal_history_coverage() -> None:
    dates = pd.bdate_range(end="2026-10-06", periods=260)
    frame = _prices(dates, ("A", "B", "SPY"))

    result = validate_current_stock_prices(
        frame,
        ("A", "B", "SPY"),
        now=datetime(2026, 10, 7, 20, tzinfo=UTC),
    )

    assert result.passed
    assert result.latest_close_fraction == 1.0
    assert result.history_252_fraction == 1.0
    assert result.latest_session == "2026-10-06"


def test_stock_price_validation_rejects_partial_session_and_missing_history() -> None:
    dates = pd.bdate_range(end="2026-10-07", periods=260)
    frame = _prices(dates, ("A", "B", "SPY"))
    frame.loc[dates[:20], (slice(None), "B")] = np.nan

    result = validate_current_stock_prices(
        frame,
        ("A", "B", "SPY"),
        now=datetime(2026, 10, 7, 16, tzinfo=UTC),
        minimum_history_252_fraction=1.0,
    )

    assert not result.passed
    assert result.partial_session_risk
    assert result.history_252_tickers == 2


def test_stock_price_validation_rejects_bad_ohlc() -> None:
    dates = pd.bdate_range(end="2026-10-06", periods=260)
    frame = _prices(dates, ("A", "SPY"))
    frame.loc[dates[-1], ("High", "A")] = 50.0

    result = validate_current_stock_prices(
        frame,
        ("A", "SPY"),
        now=datetime(2026, 10, 7, 20, tzinfo=UTC),
    )

    assert not result.passed
    assert result.invalid_ohlc_rows == 1


def test_yahoo_batch_normalization_restores_dotted_canonical_ticker() -> None:
    dates = pd.bdate_range("2026-10-01", periods=2)
    fields = [*list("OHLC"), "Volume"]
    field_names = {
        "O": "Open",
        "H": "High",
        "L": "Low",
        "C": "Close",
        "Volume": "Volume",
    }
    columns = pd.MultiIndex.from_product(
        [[field_names[value] for value in fields], ["BRK-B"]]
    )
    raw = pd.DataFrame(100.0, index=dates, columns=columns)

    normalized = _normalize_yahoo_batch(raw, ("BRK.B",), ["BRK-B"])

    assert normalized.columns.names == ["field", "ticker"]
    assert set(normalized.columns.get_level_values("ticker")) == {"BRK.B"}


def test_safe_download_end_excludes_an_in_progress_session() -> None:
    during_market = datetime(2026, 10, 7, 16, tzinfo=UTC)
    after_market = datetime(2026, 10, 7, 22, tzinfo=UTC)

    assert _safe_yahoo_end_date(during_market).isoformat() == "2026-10-07"
    assert _safe_yahoo_end_date(after_market).isoformat() == "2026-10-08"


def test_stock_price_snapshot_audit_binds_universe_and_detects_tampering(
    tmp_path: Path,
) -> None:
    dates = pd.bdate_range(end="2026-10-06", periods=260)
    frame = _prices(dates, ("A", "SPY"))
    universe_manifest_path = tmp_path / "universe.manifest.json"
    universe_manifest_path.write_text(json.dumps({"tabular_sha256": "universe"}))
    captured = datetime(2026, 10, 7, 20, tzinfo=UTC)
    snapshot = write_current_stock_price_snapshot(
        frame,
        ("A", "SPY"),
        tmp_path / "prices",
        universe_manifest_path=universe_manifest_path,
        universe_manifest={
            "tabular_sha256": "universe",
            "universe_role": "prospective_current_universe_only_not_historical_backfill",
        },
        benchmark="SPY",
        captured_at=captured,
        requested_start=date(2024, 1, 1),
        requested_end_exclusive=date(2026, 10, 7),
    )

    audit = audit_current_stock_price_snapshot(
        snapshot.manifest_path,
        universe_manifest_path=universe_manifest_path,
        now=captured,
    )
    assert audit.passed

    universe_manifest_path.write_text("changed")
    changed = audit_current_stock_price_snapshot(
        snapshot.manifest_path,
        universe_manifest_path=universe_manifest_path,
        now=captured,
    )
    assert not changed.integrity_passed
    assert "Universe manifest hash" in " ".join(changed.errors)


def _prices(dates: pd.DatetimeIndex, tickers: tuple[str, ...]) -> pd.DataFrame:
    close = pd.DataFrame(
        {ticker: np.linspace(100.0, 130.0, len(dates)) for ticker in tickers},
        index=dates,
    )
    fields = {
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": pd.DataFrame(1_000_000.0, index=dates, columns=tickers),
    }
    frame = pd.concat(fields, axis=1)
    frame.columns.names = ["field", "ticker"]
    return frame
