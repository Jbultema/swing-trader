# Swing Trader

An independent, long-only swing/momentum research and decision-support system for retirement-style accounts. It downloads daily adjusted OHLCV data, produces next-session human-executed trade tickets, explains every hold/buy/sell decision, and compares the candidate with simple benchmarks.

This is research software, not investment advice. It never connects to a broker or places an order.

## Current design

- Liquid equity, sector, international, gold, and Treasury ETFs avoid a hindsight-selected single-stock universe in the first research stage.
- The champion ranks trailing 12-month returns, requires positive absolute momentum, holds the top three equal-weight, and rebalances monthly.
- A capital-preservation comparator adds daily panic exits; it is reported separately rather than silently mixed into the champion.
- Faster weekly multi-horizon, volatility-sized, trailing-exit candidates remain visible as rejected experiments.
- No-technology, no-sector, broad-asset, and 17 leave-one-asset-out diagnostics expose dependence on overlapping funds and current winners without retrospectively replacing the frozen champion.
- A close-derived signal is modeled at the next adjusted open; performance accrues open-to-open. One-way turnover costs 10 basis points.
- Cash is an intentional position. The system does not short, use options, use derivatives, or borrow.
- Unallocated capital accrues the adjusted return of the configured Treasury-bill ETF; holdings drift between actual rebalance events instead of receiving free daily rebalancing.
- The dashboard shows the recommendation, the evidence behind it, risk-off reasons, historical comparisons, and methodology status.
- Individual-stock research is a separate track with causal momentum features, explicit exit
  reasons, point-in-time coverage gates, and immutable prospective screens. It uses no paid data
  and is not yet an actionable model.

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

GitHub Actions runs the same research-only snapshot after U.S. market hours on weekdays, locks a hashed non-overwriting shadow record, retains the full evidence bundle for 90 days, and appends the validated record to the repository's durable `shadow-evidence` branch. Local daily runs accumulate records under `reports/shadow`. A delayed or failed workflow places no order and creates no fallback trade; the last verified snapshot remains the only valid input.

Each hosted run also scores previously locked, unique monthly decisions at 5-, 21-, and 63-session horizons once those future opens exist. Repeated daily holds are deduplicated, frozen turnover costs are applied, and failed-gate records are excluded from performance summaries. Point the same evaluator at a local archive with `poetry run swing-trader shadow evaluate --records evidence/records`.

Each run also binds the exact downloaded price-file SHA-256 into the quality report and locked shadow. Stable strategy/configuration and full implementation-code hashes are recorded separately, so an audit can distinguish a new data date from an actual model change. The audit checks that provenance chain, cross-file gate consistency, research-only authority, and every shadow-record hash. The result—including the gate state, hypothetical holdings, and capital-preservation comparator—is written to the GitHub Actions run summary. Run the same check locally with `poetry run swing-trader audit`; integrity failures return a nonzero exit code, while an unavailable independent feed is clearly reported as `FAIL CLOSED`. Hosted runs add `--require-data-gate`, so a missing or divergent independent feed creates a visible failed workflow while `if: always()` still preserves its forensic artifact.

## Evidence boundaries

The primary market-data connector uses the open-source `yfinance` client and an unofficial public endpoint. Every download is cached with a timestamp and SHA-256 hash. The action layer independently reconciles the latest 12 completed monthly total returns against Alpha Vantage's documented adjusted-monthly API and fails closed when the key is absent, the endpoint errors, history is stale, or any ticker differs by more than 50 basis points.

Set a personal API key in the ignored local `.env` file or as the repository's `ALPHA_VANTAGE_API_KEY` Actions secret. Explicit process environment variables take precedence over `.env`:

```bash
cp .env.example .env
# edit .env, then:
poetry run swing-trader daily
```

The operating path has no Trading Economics, Bloomberg, Sharadar, or other paid-data connector.
Alpha Vantage is used only within its documented free allowance; the system still records a
research-only state when that allowance is unavailable, but excludes it from primary performance.

The no-paid individual-stock path locks every input before it can observe the next session:

```bash
poetry run swing-trader data snapshot-stock-universe
poetry run swing-trader data snapshot-stock-prices
poetry run swing-trader shadow screen-stocks
poetry run swing-trader data snapshot-finra-activity
poetry run swing-trader data snapshot-sec-events
poetry run swing-trader shadow validate-stock-candidates
poetry run swing-trader data snapshot-earnings --horizon 3month
poetry run swing-trader shadow record-stocks
```

The universe combines the currently observed public S&P 500 table with strict schema checks and a
non-authoritative `pitindex` comparison. The adjusted price panel comes from Yahoo through
`yfinance`, excludes a still-open market session, and requires at least 99% current-close coverage.
The candidate screen forms one top-ten consensus from the three frozen signal families. Alpha
Vantage's free quota checks only the primary consensus portfolio's existing holdings, its new
candidates, and SPY: at most 21 daily-price requests. The market-guard arm is retained as a
diagnostic comparator rather than consuming enough calls to make the primary workflow unreliable.
A local ledger permits at most 24 of the documented 25 daily calls, leaving room for the one-call
bulk earnings calendar. Missing, stale, divergent, or quota-limited validation still produces an
immutable diagnostic state but makes it ineligible for primary prospective performance.

The public, keyless FINRA sidecar locks 21 completed sessions of off-exchange short-sale-volume
context for the same shortlist. It validates each full source file before filtering and binds exact
source hashes to the candidate, universe, and price manifests. These values are not short interest,
carry no bullish or bearish label, and are explicitly excluded from ranking and portfolio state.
Run `poetry run swing-trader data verify-finra-activity` to audit the latest standalone snapshot.

The official, keyless SEC sidecar locks acceptance-time metadata for 8-K, 10-Q, 10-K, Form 4,
6-K, and beneficial-ownership filings for the same candidate shortlist. It validates issuer/ticker
identity and the complete recent-filing schema, excludes filings accepted after capture, preserves
source hashes and filing links, and is likewise prohibited from changing ranks or positions. Run
`poetry run swing-trader data verify-sec-events` to audit a snapshot. If an execution network is
blocked by SEC access controls, `data probe-sec-access` preserves a machine-readable failure record
and the daily stock workflow continues with an explicit unavailable diagnostic rather than an
unofficial mirror.

`record-stocks` starts from cash, records decisions made after the completed close, and applies them
no earlier than the next regular-session open. It refuses to fill a missing paper session with the
current roster. Hard-loss, ATR-trailing, trend, short-momentum, rank-decay, and 21-session time exits
are retained as explicit reasons. No command places an order.

Each arm carries a self-financing fractional-share paper account. New positions use available cash,
surviving positions are not resized for free, and the frozen 50 bp round-trip assumption is charged
as 25 bp at entry and 25 bp at exit. If Yahoo revises adjusted history after a distribution or
split, shares are rebased to the prior state's locked close mark so a provider revision cannot create
a fake gain. The immutable evaluator attributes each close-to-close return to the prior decision's
data gate and compares the same eligible sessions with a costed SPY buy-and-hold path. Rolling
5-, 21-, and 63-session policy windows are reported only when every intervening decision gate and
valuation passed.

No prospective performance headline is considered mature until the lineage contains at least 126
eligible sessions, 30 completed exits, and 10 risk-off sessions. At 21 eligible paired sessions the
report begins a 2,000-sample paired stationary-bootstrap interval, but meeting any monitoring
threshold still does not authorize a trade.

The `No-paid stock shadow` workflow runs at 00:30 UTC Tuesday through Saturday, corresponding to
the prior U.S. weekday close. It restores the exact content-hashed lineage from the durable evidence
branch, retries transient Yahoo misses twice as single-symbol requests, and archives candidates,
validations, earnings inputs, decisions, and run diagnostics. The state lineage is keyed by both the
stock decision-policy hash and frozen config; a signal, gate, execution, or config change starts a
new paper sequence from cash rather than joining incomparable rules. The full package hash and a
separate evaluation-method hash remain recorded for forensic reproducibility, but dashboard and
diagnostic-collector changes no longer erase portfolio continuity. A holiday is a recorded no-op,
while a genuinely missed trading session fails closed for manual reconciliation.

The dashboard's stock tab shows the latest state gate, decision-policy lineage status, market regime,
next-open targets, cash/equity/cost accounting, and every BUY/HOLD/SELL/SKIP reason. It hides no
failed gate and labels the market-guard arm as a diagnostic comparator.

These current-universe snapshots may never be projected backward as historical membership. Before
any retrospective individual-stock experiment, normalized prices and point-in-time membership must
still pass `poetry run swing-trader data audit-stocks`. If open sources cannot reach the required
99% member-date and corporate-action coverage for a period, that period receives no performance
claim. The workaround is locked prospective evidence, not a survivor-only backtest.

Official published portfolios provide a separate gross sanity check that does not bypass that gate:

```bash
poetry run swing-trader research published-momentum
```

This locks and scores the Fama-French daily broad and size-by-prior-return portfolios plus the
market/risk-free series. The report is explicitly a published CRSP-decile comparator: daily
reconstitution, constituent turnover, execution costs, and swing-trader exits cannot be inferred
from the aggregate returns, so it never authorizes a trade or substitutes for the prospective bot.

The stock report also writes the complete variant registry, expanding walk-forward path,
stationary-bootstrap uncertainty, family-wide and false-discovery-adjusted tests, approximate PBO,
hostile-regime results, and split-specific ticker P&L concentration. Statistical significance is
reported as a fragility diagnostic and never treated as trading authorization.

The dashboard exposes a separate read-only stock-research tab only when a retrospective bundle passes artifact,
implementation, input-binding, and research-authority checks. It shows the latest retrospective
BUY/HOLD/SELL/SKIP reasons, comparator metrics, multiple-testing evidence, hostile regimes,
execution capacity, and winner concentration; it does not relabel the result as a current trade.

Prospective operation can also lock the next Alpha Vantage earnings calendar before decisions:

```bash
poetry run swing-trader data snapshot-earnings --horizon 3month
```

These immutable snapshots are event-risk inputs only. They may block a new position before a
scheduled report and produce an explicit `scheduled_earnings_entry_blackout` explanation, but they
do not force an existing holding to exit and are never used to manufacture historical test data.

Alpha Vantage documentation: <https://www.alphavantage.co/documentation/>. Reconciliation materially improves error detection, but neither provider is a broker-grade execution quote. Broker-side price checks and prospective shadow evidence remain required before live use.

The ETF connector spaces requests and reuses a content-hashed monthly snapshot for up to eight days.
A Sunday workflow spends its 17 free calls when the stock workflow is idle; weekday calls are
reserved for the stock shortlist and earnings calendar. A cache that is stale, modified, or missing
the newest completed month fails the ETF decision gate rather than triggering an unplanned weekday
quota burst.

The first v1 run opened the 2024-present labeled holdout and exposed a daily-resizing defect that violated the weekly-rebalance specification. V2 fixes that defect, so the interval is now diagnostic rather than sealed evidence. No retrospective test can substitute for locked prospective shadow decisions.

See [Research basis](docs/research_basis.md), [stock-momentum research](docs/stock_momentum_research.md), [validation protocol](docs/validation_protocol.md), [system design](docs/system_design.md), and the [open-data plan](docs/open_data_plan.md).

For a local account-specific but still non-executable share estimate, copy `config/portfolio.example.toml` to the ignored `config/portfolio.toml`, restrict its menu to the actual plan, and run `poetry run swing-trader ticket preview`. See [retirement-account readiness](docs/account_readiness.md).
