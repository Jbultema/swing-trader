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

Each run also binds the exact downloaded price-file SHA-256 into the quality report and locked shadow. Stable strategy/configuration and full implementation-code hashes are recorded separately, so an audit can distinguish a new data date from an actual model change. The audit checks that provenance chain, cross-file gate consistency, research-only authority, and every shadow-record hash. The result—including the gate state, hypothetical holdings, and capital-preservation comparator—is written to the GitHub Actions run summary. Run the same check locally with `poetry run swing-trader audit`; integrity failures return a nonzero exit code, while an unavailable independent feed is clearly reported as `FAIL CLOSED`. Hosted runs classify that exact intact-but-unavailable state as a yellow warning so recurring provider limits do not create failure email, but any missing, malformed, tampered, or implementation-mismatched artifact remains a failed workflow. Either state authorizes no action, and `if: always()` still preserves the forensic artifact.

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
poetry run swing-trader data snapshot-stock-shares
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
candidates, and SPY: at most 21 daily-price requests. The benchmark-only market guard and a
capture-forward market-breadth guard are retained as diagnostic comparators rather than consuming
enough calls to make the primary workflow unreliable. The breadth guard requires the existing SPY
trend/volatility state plus 20-session mean advancing participation, with at least 95%
current-roster return coverage in every session. A 45%/55% hysteresis band prevents a noisy
majority threshold from repeatedly liquidating and repurchasing the portfolio.
A preregistered simple 12-1 individual-stock momentum control holds the ten highest current-roster
returns from t-252 through t-21 for at most 21 sessions, deliberately omitting the composite
confirmation rules. Short-volume proxy and exact share-turnover arms hold each signal's top-ten
large-cap screen for at most 5, 10, or 21 sessions. All use identical exits, next-open accounting,
sector caps, and 50 bp round-trip costs. These Yahoo-only arms remain ineligible for primary
performance and test whether the complex or faster rules add value over a transparent stock-level
momentum baseline.
Every stock arm also enforces an entry cap of three names per GICS sector. A fourth same-sector
candidate is shown as `SKIP`; it is not replaced by a weaker name, and its weight remains cash.
This bounds a ten-name sleeve at 30% initial sector exposure without manufacturing diversification.
Here `short_volume` is an immutable internal identifier for short-horizon price momentum plus
relative trading-volume participation. It is neither FINRA short-sale volume nor the academic
volume/shares-outstanding turnover characteristic. Alpha Vantage's endpoint was premium-only on
the probed free account. A separate Yahoo `Ticker.get_shares_full` collector now locks one
current-roster value per ticker for use only after capture; it never treats Yahoo's historical
timestamps as point-in-time backfill. The 2026-10-07 live snapshot passed its hash and universe
binding with 500 of 503 names usable (99.40%); ERIE, WAT, and WBD were stale under the frozen
130-day limit. Run `poetry run swing-trader data snapshot-stock-shares` and then
`poetry run swing-trader data verify-stock-shares` to reproduce the gate. The exact
`share_turnover_skip3` experiment independently takes the top quintile of prior-month return and
the top quintile of 18-session volume divided by captured shares, omitting the latest three
sessions, then selects the ten strongest intersections. It starts only from the capture-forward
policy lineage; no historical share values are inferred.
The shares denominator matches the published characteristic, but the portfolio is a constraint-
adapted test rather than a paper replication: it is long-only, equal weighted, current-S&P-500
based, sector capped, and uses explicit exits. The 21-session arm is the literature-aligned holding
reference; 5 and 10 sessions are preregistered falsification tests, not assumed improvements.
The evaluator reports where each arm's return occurred (overnight, explicit next-open cost, or
intraday), compares each 5/10-session arm with its own signal family's 21-session reference, starts
paired uncertainty after 21 sessions, and applies family-wide error control after 63 sessions; none
of those diagnostic results can qualify the primary record. The exact arms have no realized
prospective transition yet, so they currently provide candidates and explanations—not a
performance claim.
A failed shares-outstanding coverage or freshness gate cannot interrupt the primary consensus
lineage. The failed artifact remains hash-auditable, exact share-turnover arms accept no new names,
and any exact-arm holdings receive an explicit fail-safe exit for the next open. Their operational
P&L remains visible, but a transition whose prior signal gate failed is excluded from exact-signal
inference. Corrupt, future-dated, stale-capture, or universe-mismatched artifacts still fail the
whole run because their provenance cannot be trusted.
The default Yahoo share collector runs in a disposable child process with a ten-minute wall-clock
limit. Any unresolved names become explicit `collection_timeout` rows, so a hung provider request
can fail the exact-share gate without hanging the whole primary workflow.
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
are retained as explicit reasons. The close-based ATR trigger is the greater post-entry adjusted
open/close high-water mark minus three times the current adjusted 14-session ATR; a missing ATR
fails closed. The dashboard displays the entry basis, high-water mark, and both hard-loss and ATR
trigger prices for every held-name decision. These are next-open decision thresholds, not guaranteed
fill prices. No command places an order.

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

The `No-paid stock shadow` workflow runs at 06:30 UTC Tuesday through Saturday, after the prior
U.S. weekday's public daily bars have settled and after both UTC and U.S. Eastern midnight. Alpha
Vantage documents its 25-request free daily limit but not the reset boundary, so the schedule does
not assume an undocumented UTC reset. A push to `main` that changes the
decision path also runs the same job, providing a clean hosted initialization for each changed
policy when manual dispatch is not available. It restores the exact content-hashed lineage from
the durable evidence branch, retries a partial Yahoo cross-section collectively before making at
most ten residual single-symbol requests, and archives candidates,
validations, earnings inputs, decisions, and run diagnostics. The state lineage is keyed by both the
stock decision-policy hash and frozen config; a signal, gate, execution, or config change starts a
new paper sequence from cash rather than joining incomparable rules. The full package hash and a
separate evaluation-method hash remain recorded for forensic reproducibility, but dashboard and
diagnostic-collector changes no longer erase portfolio continuity. A holiday is a recorded no-op,
while a genuinely missed trading session fails closed for manual reconciliation.

The optional weekly Alpha Vantage cache is skipped with a visible warning when its repository
secret is absent; a configured provider call that fails still fails the workflow. The SEC access
check is a weekly/manual diagnostic because the official endpoint may reject GitHub-hosted network
traffic even though the parser and evidence path are healthy. A structured `failed` provider result
is retained as a warning, while a missing or malformed report remains a real workflow failure.

The dashboard's stock tab shows the latest state gate, decision-policy lineage status, market regime,
next-open targets, cash/equity/cost accounting, captured shares, share turnover, independent return
and turnover percentiles, per-arm signal availability, and every BUY/HOLD/SELL/SKIP reason. It
hides no failed gate and labels the benchmark guard, market-breadth guard, classic 12-1
stock-momentum, short-volume, and exact share-turnover arms as diagnostic comparators.

These current-universe snapshots may never be projected backward as historical membership. Before
any retrospective individual-stock experiment, normalized prices and point-in-time membership must
still pass `poetry run swing-trader data audit-stocks`. If open sources cannot reach the required
99% member-date and corporate-action coverage for a period, that period receives no performance
claim. The workaround is locked prospective evidence, not a survivor-only backtest.

An optional local-only feasibility audit can compare a long-format PIT membership/price release
with Tiingo's public supported-ticker catalog:

```bash
poetry run swing-trader data audit-tiingo-catalog --refresh
```

Inputs default to ignored `imports/sp500-data/` paths and the report to ignored
`reports/private/`. The result is only a catalog upper bound: Tiingo says the catalog includes
reserved symbols, and ticker reuse can map an old company to a different current security. The
command therefore never marks the history backtest-ready or action-authorized.

Official published portfolios provide a separate gross sanity check that does not bypass that gate:

```bash
poetry run swing-trader research published-momentum
```

This locks and scores the Fama-French daily broad, large-half, and largest-size-quintile
prior-return portfolios plus the market/risk-free series. It reports direct gross
winner-minus-loser characteristic spreads with 21-lag Newey-West statistics, rather than inferring
an effect from two standalone CAGRs. The report remains a published CRSP aggregate comparator:
constituent turnover, execution costs, and swing-trader exits cannot be inferred from the returns,
so it never authorizes a trade or substitutes for the prospective bot.

Verify the newest report and all seven cached source archives before presenting it:

```bash
poetry run swing-trader research verify-published-momentum
```

The stock dashboard shows only an integrity-passing report, flags older code or stale official
data, and emphasizes direct largest-size-quintile winner-minus-loser spreads rather than a
potentially market-driven winner-portfolio CAGR.

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
