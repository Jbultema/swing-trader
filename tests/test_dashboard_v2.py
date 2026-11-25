from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "src/swing_trader/dashboard.py"


def test_dashboard_v2_has_operating_routes_and_forward_book() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "Swing Trader V2" in source
    assert '"Today", "Forward Test", "Performance", "Risk", "Research & Evidence"' in source
    assert "stock_decision_headline" in source
    assert "ForwardJournal" in source
    assert "Lock current recommendation tickets" in source
    assert "Record execution" in source
    assert "Record this session's close valuation" in source
    assert "Refresh center" in source
    assert "Run stock daily refresh" in source
    assert "Re-evaluate forward evidence" in source
    assert "Refresh historical ETF research" in source
    assert "RefreshJobStore" in source
    assert '@st.fragment(run_every="3s")' in source


def test_dashboard_v2_does_not_render_raw_json_as_the_product() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    assert "st.tabs(" not in source
    assert "st.json(" not in source
    assert "Download exact machine-readable evidence" in source
    assert "download_button" in source
