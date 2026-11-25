from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from swing_trader.source_qualification import (
    verify_source_qualification_report,
    write_source_qualification_report,
)


def test_qualification_fails_closed_without_live_stock_source(tmp_path: Path) -> None:
    result = write_source_qualification_report(
        tmp_path / "reports",
        alpha_key_configured=True,
        now=datetime(2026, 10, 22, 15, tzinfo=UTC),
    )
    payload = json.loads(result.path.read_text())
    sources = {row["source_id"]: row for row in payload["sources"]}

    assert result.broader_strategy_work_authorized is False
    assert result.prospective_feature_work_authorized is False
    assert result.authorized_experiment_families == ()
    assert result.eligible_new_stock_selection_source_ids == ()
    assert result.passed_source_ids == ("fred_dgs3mo",)
    assert sources["sec_edgar_as_filed_xbrl"]["status"] == "blocked"
    assert sources["alpha_vantage_free"]["status"] == "context_only"
    assert sources["alpha_vantage_free"]["passes_all_required_criteria"] is False
    assert sources["alpaca_basic_sip"]["criteria"]["open_data_policy_compatible"] is False
    assert payload["existing_lineage_changed"] is False
    assert payload["action_authorized"] is False
    assert verify_source_qualification_report(result.path)


def test_live_sec_probe_unlocks_only_prospective_fundamental_collection(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "sec.json"
    probe.write_text(
        json.dumps(
            {
                "status": "passed",
                "checked_at_utc": "2026-10-22T15:00:00+00:00",
                "source_sha256": "abc",
            }
        )
    )

    result = write_source_qualification_report(
        tmp_path / "reports",
        sec_probe_path=probe,
        now=datetime(2026, 10, 22, 15, tzinfo=UTC),
    )
    payload = json.loads(result.path.read_text())
    sources = {row["source_id"]: row for row in payload["sources"]}

    assert result.prospective_feature_work_authorized is True
    assert result.broader_strategy_work_authorized is False
    assert result.authorized_experiment_families == ()
    assert result.eligible_new_stock_selection_source_ids == ("sec_edgar_as_filed_xbrl",)
    assert sources["sec_edgar_as_filed_xbrl"]["approved_uses"] == [
        "as_filed_fundamental_research",
        "prospective_filing_features",
    ]
    assert "stock_prices_and_identity" not in {
        sources[source_id]["intended_role"]
        for source_id in result.eligible_new_stock_selection_source_ids
    }
    assert payload["qualified_stock_price_source_ids"] == []


def test_stale_passing_sec_probe_cannot_unlock_strategy(tmp_path: Path) -> None:
    probe = tmp_path / "sec.json"
    probe.write_text(
        json.dumps(
            {
                "status": "passed",
                "checked_at_utc": "2026-10-20T15:00:00+00:00",
            }
        )
    )

    result = write_source_qualification_report(
        tmp_path / "reports",
        sec_probe_path=probe,
        now=datetime(2026, 10, 22, 15, tzinfo=UTC),
    )
    payload = json.loads(result.path.read_text())
    sec = next(row for row in payload["sources"] if row["source_id"] == "sec_edgar_as_filed_xbrl")

    assert sec["status"] == "blocked"
    assert sec["evidence"]["runtime_probe_fresh"] is False
    assert result.broader_strategy_work_authorized is False


def test_prospective_yahoo_snapshot_does_not_unlock_backtest(tmp_path: Path) -> None:
    manifest = tmp_path / "prices.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "price_data_gate_passed": True,
                "captured_at_utc": "2026-10-22T15:00:00+00:00",
                "validation": {"status": "passed", "latest_session": "2026-10-21"},
            }
        )
    )

    result = write_source_qualification_report(
        tmp_path / "reports",
        latest_price_manifest_path=manifest,
        now=datetime(2026, 10, 22, 15, tzinfo=UTC),
    )
    payload = json.loads(result.path.read_text())
    yahoo = next(row for row in payload["sources"] if row["source_id"] == "yahoo_via_yfinance")

    assert yahoo["status"] == "prospective_only"
    assert yahoo["approved_uses"] == ["immutable_capture_forward_price_snapshot"]
    assert yahoo["passes_all_required_criteria"] is False
    assert result.broader_strategy_work_authorized is False


def test_qualification_hash_detects_tampering(tmp_path: Path) -> None:
    result = write_source_qualification_report(
        tmp_path / "reports",
        now=datetime(2026, 10, 22, 15, tzinfo=UTC),
    )
    payload = json.loads(result.path.read_text())
    payload["broader_strategy_work_authorized"] = True
    result.path.write_text(json.dumps(payload))

    assert not verify_source_qualification_report(result.path)
