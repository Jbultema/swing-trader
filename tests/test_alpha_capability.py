from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

from swing_trader.alpha_capability import (
    probe_alpha_shares_outstanding,
    verify_alpha_capability_snapshot,
)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_alpha_share_probe_records_premium_boundary_without_message_or_key(
    tmp_path: Path,
) -> None:
    provider = b'{"Information":"This endpoint is available to premium users."}'
    snapshot = probe_alpha_shares_outstanding(
        "secret",
        tmp_path / "output",
        quota_dir=tmp_path / "quota",
        opener=lambda *_args, **_kwargs: _Response(provider),
        now=datetime(2026, 10, 7, 22, tzinfo=UTC),
    )
    payload = json.loads(snapshot.path.read_text())
    ledger = json.loads((tmp_path / "quota/2026-10-07.json").read_text())

    assert snapshot.status == "premium_only"
    assert snapshot.usable_on_free_tier is False
    assert payload["provider_message_retained"] is False
    assert "premium users" not in snapshot.path.read_text()
    assert "secret" not in snapshot.path.read_text()
    assert ledger["reserved_calls"] == 1
    assert verify_alpha_capability_snapshot(snapshot.path)


def test_alpha_share_probe_recognizes_available_report_schema(tmp_path: Path) -> None:
    provider = json.dumps(
        {
            "symbol": "MSFT",
            "annualReports": [{"fiscalDateEnding": "2025-06-30"}],
            "quarterlyReports": [{"fiscalDateEnding": "2026-06-30"}],
        }
    ).encode()
    snapshot = probe_alpha_shares_outstanding(
        "secret",
        tmp_path / "output",
        quota_dir=tmp_path / "quota",
        opener=lambda *_args, **_kwargs: _Response(provider),
        now=datetime(2026, 10, 7, 22, tzinfo=UTC),
    )
    payload = json.loads(snapshot.path.read_text())

    assert snapshot.status == "available"
    assert snapshot.usable_on_free_tier is True
    assert payload["annual_rows"] == 1
    assert payload["quarterly_rows"] == 1
    assert verify_alpha_capability_snapshot(snapshot.path)
