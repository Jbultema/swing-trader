# Swing Trader

An independent, long-only swing/momentum research and decision-support system for retirement-style accounts. It downloads daily adjusted OHLCV data, produces next-session human-executed trade tickets, explains every hold/buy/sell decision, and compares the candidate with simple benchmarks.

This is research software, not investment advice. It never connects to a broker or places an order.

## Current design

- Liquid equity, sector, international, gold, and Treasury ETFs avoid a hindsight-selected single-stock universe in the first research stage.
- The champion ranks trailing 12-month returns, requires positive absolute momentum, holds the top three equal-weight, and rebalances monthly.
- A capital-preservation comparator adds daily panic exits; it is reported separately rather than silently mixed into the champion.
- Faster weekly multi-horizon, volatility-sized, trailing-exit candidates remain visible as rejected experiments.
- A close-derived signal is modeled at the next adjusted open; performance accrues open-to-open. One-way turnover costs 10 basis points.
- Cash is an intentional position. The system does not short, use options, use derivatives, or borrow.
- The dashboard shows the recommendation, the evidence behind it, risk-off reasons, historical comparisons, and methodology status.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/swing-trader data update
.venv/bin/swing-trader research run
.venv/bin/swing-trader dashboard
```

Run the complete daily research snapshot with:

```bash
.venv/bin/swing-trader daily
```

GitHub Actions runs the same research-only snapshot after U.S. market hours on weekdays, locks a hashed non-overwriting shadow record, and retains the evidence bundle for 90 days. Local daily runs accumulate the same records under `reports/shadow`. A delayed or failed workflow places no order and creates no fallback trade; the last verified snapshot remains the only valid input.

Each run also binds the exact downloaded price-file SHA-256 into the quality report and locked shadow, then audits that provenance chain, the strategy-specification hash, cross-file gate consistency, research-only authority, and every shadow-record hash. The result—including the gate state, hypothetical holdings, and capital-preservation comparator—is written to the GitHub Actions run summary. Run the same check locally with `poetry run swing-trader audit`; integrity failures return a nonzero exit code, while an unavailable independent feed is clearly reported as `FAIL CLOSED`. Hosted runs add `--require-data-gate`, so a missing or divergent independent feed creates a visible failed workflow while `if: always()` still preserves its forensic artifact.

## Evidence boundaries

The primary market-data connector uses the open-source `yfinance` client and an unofficial public endpoint. Every download is cached with a timestamp and SHA-256 hash. The action layer independently reconciles the latest 12 completed monthly total returns against Alpha Vantage's documented adjusted-monthly API and fails closed when the key is absent, the endpoint errors, history is stale, or any ticker differs by more than 50 basis points.

Set a personal API key locally or as the repository's `ALPHA_VANTAGE_API_KEY` Actions secret:

```bash
export ALPHA_VANTAGE_API_KEY="..."
poetry run swing-trader daily
```

Alpha Vantage documentation: <https://www.alphavantage.co/documentation/>. Reconciliation materially improves error detection, but neither provider is a broker-grade execution quote. Broker-side price checks and prospective shadow evidence remain required before live use.

The first v1 run opened the 2024-present labeled holdout and exposed a daily-resizing defect that violated the weekly-rebalance specification. V2 fixes that defect, so the interval is now diagnostic rather than sealed evidence. No retrospective test can substitute for locked prospective shadow decisions.

See [Research basis](docs/research_basis.md), [validation protocol](docs/validation_protocol.md), and [system design](docs/system_design.md).
