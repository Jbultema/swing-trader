from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from swing_trader.ticket import PortfolioConfigError, build_trade_preview


def test_trade_preview_respects_sleeve_and_never_authorizes_execution(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.toml"
    portfolio.write_text(
        """
[account]
account_type = "roth_ira"
cash = 100.0
fractional_shares = false
minimum_trade_dollars = 1.0
allowed_tickers = ["A", "SPY"]

[holdings]
SPY = 9.0
""".strip()
        + "\n",
        encoding="utf-8",
    )
    preview = build_trade_preview(_prices(), _decisions(target="A", reconciled=False), portfolio)

    assert preview["status"] == "INVALID_UNRECONCILED_RESEARCH_PREVIEW"
    assert preview["execution_authorized"] is False
    assert preview["automatic_order_placement"] is False
    actions = {row["ticker"]: row for row in preview["actions"]}
    assert actions["A"]["side"] == "BUY"
    assert actions["A"]["estimated_share_change"] == 10.0
    assert actions["SPY"]["side"] == "SELL"
    assert actions["SPY"]["estimated_share_change"] == -9.0
    assert preview["estimated_ending_cash"] == pytest.approx(0.0)


def test_trade_preview_refuses_unavailable_target_without_substitution(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.toml"
    portfolio.write_text(
        """
[account]
account_type = "401k"
cash = 1000.0
allowed_tickers = ["SPY"]

[holdings]
SPY = 0.0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PortfolioConfigError, match="no substitution"):
        build_trade_preview(_prices(), _decisions(target="A", reconciled=True), portfolio)


def _prices() -> pd.DataFrame:
    frame = pd.DataFrame(
        {("Close", "A"): [100.0], ("Close", "SPY"): [100.0]},
        index=pd.DatetimeIndex(["2026-01-05"]),
    )
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["field", "ticker"])
    return frame


def _decisions(*, target: str, reconciled: bool) -> dict[str, object]:
    return {
        "as_of_close": "2026-01-05",
        "data_reconciled": reconciled,
        "hypothetical_actions": [
            {
                "ticker": target,
                "target_weight": 1.0,
                "why": "Selected by the frozen strategy.",
            }
        ],
    }
