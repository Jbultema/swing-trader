from __future__ import annotations

import json
import math
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


class PortfolioConfigError(ValueError):
    """Raised when a retirement-sleeve preview would violate its declared boundary."""


def build_trade_preview(
    prices: pd.DataFrame,
    decisions: dict[str, object],
    portfolio_path: Path,
) -> dict[str, object]:
    """Convert target weights to non-executable estimated share changes."""
    with portfolio_path.open("rb") as handle:
        config = tomllib.load(handle)
    account = config.get("account")
    holdings = config.get("holdings", {})
    if not isinstance(account, dict) or not isinstance(holdings, dict):
        raise PortfolioConfigError("Portfolio file requires [account] and [holdings] tables.")
    account_type = str(account.get("account_type", "")).lower()
    if account_type not in {"401k", "roth_ira", "traditional_ira"}:
        raise PortfolioConfigError("account_type must be 401k, roth_ira, or traditional_ira.")
    allowed_raw = account.get("allowed_tickers")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise PortfolioConfigError(
            "account.allowed_tickers must explicitly define the managed menu."
        )
    allowed = tuple(str(ticker).upper() for ticker in allowed_raw)
    if len(allowed) != len(set(allowed)):
        raise PortfolioConfigError("account.allowed_tickers contains duplicates.")
    cash = float(account.get("cash", 0.0))
    minimum_trade = float(account.get("minimum_trade_dollars", 25.0))
    fractional = bool(account.get("fractional_shares", False))
    if cash < 0.0 or minimum_trade < 0.0:
        raise PortfolioConfigError("cash and minimum_trade_dollars must be non-negative.")
    shares = {str(ticker).upper(): float(value) for ticker, value in holdings.items()}
    if any(value < 0.0 for value in shares.values()):
        raise PortfolioConfigError("Long-only sleeves cannot contain negative shares.")
    unmanaged = set(shares) - set(allowed)
    if unmanaged:
        raise PortfolioConfigError(
            f"Holdings outside the explicitly managed menu: {sorted(unmanaged)}"
        )

    close = prices["Close"].iloc[-1]
    missing_prices = set(allowed) - set(close.dropna().index)
    if missing_prices:
        raise PortfolioConfigError(f"Missing latest estimates for: {sorted(missing_prices)}")
    actions = decisions.get("hypothetical_actions", [])
    if not isinstance(actions, list):
        raise PortfolioConfigError("Decision artifact has invalid hypothetical_actions.")
    action_rows = {
        str(row["ticker"]): row for row in actions if isinstance(row, dict) and "ticker" in row
    }
    target_weights = {
        ticker: float(row.get("target_weight", 0.0)) for ticker, row in action_rows.items()
    }
    unavailable = {
        ticker for ticker, weight in target_weights.items() if weight > 0 and ticker not in allowed
    }
    if unavailable:
        raise PortfolioConfigError(
            "Model targets are unavailable in this account menu; no substitution is allowed: "
            f"{sorted(unavailable)}"
        )
    if (
        any(weight < 0.0 for weight in target_weights.values())
        or sum(target_weights.values()) > 1.0 + 1e-9
    ):
        raise PortfolioConfigError("Decision target violates long-only fully-funded constraints.")

    sleeve_value = cash + sum(shares.get(ticker, 0.0) * float(close[ticker]) for ticker in allowed)
    if sleeve_value <= 0.0:
        raise PortfolioConfigError("Managed sleeve value must be positive.")
    rows: list[dict[str, object]] = []
    estimated_trade_cash = 0.0
    for ticker in allowed:
        price = float(close[ticker])
        current_shares = shares.get(ticker, 0.0)
        target_weight = target_weights.get(ticker, 0.0)
        raw_target_shares = sleeve_value * target_weight / price
        target_shares = (
            math.floor(raw_target_shares * 1_000.0) / 1_000.0
            if fractional
            else float(math.floor(raw_target_shares))
        )
        change = target_shares - current_shares
        notional = change * price
        if abs(notional) < minimum_trade:
            change = 0.0
            target_shares = current_shares
            notional = 0.0
        if abs(change) < 1e-12 and current_shares == 0.0 and target_weight == 0.0:
            continue
        side = "HOLD" if abs(change) < 1e-12 else ("BUY" if change > 0.0 else "SELL")
        source = action_rows.get(ticker)
        why = (
            str(source.get("why", "Selected by the frozen strategy."))
            if source
            else "Not selected by the current frozen strategy; target weight is zero."
        )
        rows.append(
            {
                "ticker": ticker,
                "side": side,
                "current_shares": current_shares,
                "estimated_target_shares": target_shares,
                "estimated_share_change": change,
                "target_weight": target_weight,
                "latest_research_close": price,
                "estimated_trade_notional": notional,
                "why": why,
            }
        )
        estimated_trade_cash -= notional

    reconciled = bool(decisions.get("data_reconciled", False))
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": (
            "NON_EXECUTABLE_RECONCILED_PREVIEW"
            if reconciled
            else "INVALID_UNRECONCILED_RESEARCH_PREVIEW"
        ),
        "execution_authorized": False,
        "automatic_order_placement": False,
        "account_type": account_type,
        "as_of_close": decisions.get("as_of_close"),
        "data_reconciled": reconciled,
        "estimated_sleeve_value": sleeve_value,
        "starting_cash": cash,
        "estimated_cash_change": estimated_trade_cash,
        "estimated_ending_cash": cash + estimated_trade_cash,
        "fractional_shares": fractional,
        "minimum_trade_dollars": minimum_trade,
        "actions": rows,
        "warnings": [
            "Research adjusted closes are estimates, not executable quotes.",
            "Verify prices, plan trading restrictions, fund availability, and available cash manually.",
            "This file cannot authorize or place an order.",
        ],
    }


def write_trade_preview(
    prices: pd.DataFrame,
    decisions_path: Path,
    portfolio_path: Path,
    output_path: Path,
) -> dict[str, object]:
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    if not isinstance(decisions, dict):
        raise PortfolioConfigError("Decision artifact must be a JSON object.")
    preview = build_trade_preview(prices, decisions, portfolio_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(preview, indent=2) + "\n", encoding="utf-8")
    return preview
