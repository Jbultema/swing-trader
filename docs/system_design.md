# System design

```text
public daily OHLCV -> validation + immutable snapshot
       -> independent adjusted-monthly reconciliation -> fail-closed data gate
       -> features at close
       -> monthly selection + explicit risk variants -> next-open target weights
       -> costed backtest + regime tests -> signed research artifacts
       -> explainable dashboard + human-executed ticket
```

The core is intentionally small. Research code creates immutable artifacts; the dashboard reads
them and may write only to a separate local execution journal. That journal records what a human
did; it cannot modify the frozen model record or become an order. This prevents a UI refresh from
silently changing historical results or a research score from becoming an execution.

## No-paid individual-stock path

```text
public current roster + stale-reference diagnostic
       -> immutable prospective-only universe snapshot
       -> completed-session adjusted OHLCV + 99% coverage gate
       -> capture-forward shares outstanding + 99% coverage/age gate
       -> three close-known momentum screens -> one frozen consensus
       -> exact return x share-turnover intersections (diagnostic arms only)
       -> public FINRA activity sidecar (diagnostic only; no ranking/state input)
       -> public SEC filing-event sidecar (diagnostic only; no sentiment/ranking/state input)
       -> free-quota independent checks for primary candidates/holdings only
       -> next-open primary targets + diagnostic market-guard comparator + explicit exits
       -> immutable prospective outcomes; human remains the only executor
       -> local recommendation/execution/valuation journal; no broker connection
```

The immutable policy shadow and the human execution book are deliberately different backends. The
policy shadow answers how the frozen model behaved with its cost assumptions. The local SQLite
journal answers what was recommended, what the user actually filled or skipped, how the real/paper
book diverged from target, and how recorded close equity compared with SPY. Every ticket binds the
source state, lineage, and policy hashes; every valuation binds its source state and gate status.
Long-only funding and position checks reject unfunded purchases and oversells. When the local
journal file exists, `stock-daily` marks every book automatically from the state-bound, integrity-
checked close panel; order placement and execution entry remain outside automation.

Dashboard refreshes run through a local SQLite-backed control plane rather than inside the
Streamlit request. Only three command families are allowed: stock daily refresh, locked-stock-state
evaluation, and historical ETF daily research. A detached worker records queued/running/completed/
failed state and writes a local log; the UI suppresses duplicates, prevents concurrent writers, and
polls the ledger. Job completion never bypasses the underlying state integrity, policy-lineage,
independent-data, prospective-maturity, or human-execution gates.

Every artifact is labeled `data_cost_policy=no_paid_sources`. Current membership is never projected
backward. A historical interval is eligible for performance reporting only if its point-in-time
member-date price and corporate-action coverage passes the same fail-closed audit. Otherwise it is
either a visibly biased diagnostic or omitted.

The primary arm holds at most ten names. Its held-name union with ten new consensus candidates and
SPY requires at most 21 Alpha Vantage daily calls. The market-guard arm cannot expand that quota:
it is labeled as a diagnostic comparator and is never counted as primary prospective performance.
All arms cap new entries at three names per GICS sector. Excess same-sector candidates are retained
in the explanation ledger as `SKIP` rows and their allocations remain cash; lower-ranked names are
not promoted merely to fill the portfolio.
The 5/10/21-session short-volume proxy and exact share-turnover arms are Yahoo-only diagnostics;
they cannot expand the free quota or qualify the primary record. All arms start from cash and must
advance through immediately consecutive recorded sessions. If scheduled closes were missed, the
daily runner inserts auditable, performance-ineligible bridge states from locked observed OHLCV;
only the prior frozen target may execute and every bridge close targets cash, so recovery cannot
manufacture historical signal decisions.
A failed but intact share-data gate cannot stop that sequence: the primary and proxy arms advance,
the exact arms prohibit entries and schedule next-open fail-safe exits, and the evaluator excludes
the affected exact-signal transition from inference. An unauditable or mismatched share artifact
still fails closed for every arm.
A decision-policy/config hash names each durable lineage. It covers universe and price handling,
independent gates, candidate construction, signals, exits, and paper accounting. The full package
hash is retained on every state and the evaluator has its own code hash, so UI or neutral sidecar
changes stay visible without restarting the portfolio. The hosted job restores only a matching
policy lineage, records holidays as no-ops before spending optional-provider calls, and fails on a
missed trading session. The current roster is requested in one bounded Yahoo batch when possible.
A partial newest row is retried as a collective missing-name set before at most ten residual
symbols receive individual recovery calls. This prevents a provider shard failure from becoming
hundreds of serial requests; the unchanged 99% close coverage gate still rejects an incomplete
cross-section.
Yahoo share collection is additionally isolated behind a ten-minute process deadline because the
underlying unofficial client can hang below Python's request boundary. Timed-out names remain
auditable unavailable rows; they cannot acquire an inferred denominator or block the primary arm.

The FINRA sidecar downloads only dates already proven to be completed price sessions. Each source
file must pass schema, trailer-count, uniqueness, and volume-consistency gates before the candidate
rows are retained. Its manifest binds the candidate, universe, price panel, and exact FINRA source
hashes while setting ranking, portfolio-state, directional-interpretation, and action authority to
false. This lets the project accumulate a clean public activity series before deciding whether a
preregistered interaction experiment is justified.

The SEC sidecar retrieves the official per-CIK submissions JSON only for the exact candidate and
benchmark symbols bound to the locked universe and price manifests. It uses EDGAR acceptance time,
not filing date, as the causal availability boundary; validates CIK/ticker identity and every
columnar-array length; retains exact source hashes; and labels filing counts as event metadata, not
sentiment. Local or hosted access failure is nonblocking diagnostic evidence and cannot silently
activate an alternate source.

Each arm also stores a self-financing paper account: fractional shares, cash, adjusted marks,
turnover, entry/exit costs, and total equity. Existing shares are not resized when rankings change.
Adjusted-share quantities are rebased against the prior locked close when a provider revises
history, preventing corporate-action revisions from becoming phantom P&L. The evaluator uses the
prior close's gate for the following realized transition and reports failed-gate returns only as
diagnostic evidence.

The scheduled workflow downloads data, reconciles completed-month returns against Alpha Vantage when its repository secret is configured, runs validation, locks a content-hashed shadow record, and uploads a read-only artifact. The same job is triggered by decision-path pushes to `main`, so a changed policy receives clean hosted evidence without depending on manual-dispatch authority. It has `contents: read` permission and no broker credentials. A missing or failed secondary feed leaves hypothetical research visible but invalidates every action. Human execution is a hard system boundary.

Workflow status separates system integrity from optional-provider availability. Missing, malformed,
tampered, or implementation-mismatched evidence remains a hard failure. An otherwise intact daily
bundle whose independent decision gate is unavailable is a warning and remains fail-closed. The
weekly Alpha cache skips with a warning only when its secret is absent; a configured refresh error
still fails. The SEC connectivity check is weekly/manual and treats a well-formed provider-access
failure as diagnostic evidence, but fails if its structured report is missing or malformed. Thus a
green workflow means the automation and evidence contract completed, not that a trade is authorized.

## Champion exit hierarchy

1. Absolute-momentum exit: trailing 12-month return becomes non-positive.
2. Scheduled rank exit: the asset leaves the top-three selection at month-end.
3. Cash: if fewer than three assets have positive momentum, the unfilled allocation stays in cash.

The separately reported panic-guard comparator exits all risk positions when 20-session benchmark volatility exceeds 35% or benchmark drawdown from its 200-session high exceeds 12%. It reduced historical drawdown modestly but gave up enough return that it did not become champion.

The rejected fast candidates also tested asset trend and trailing-risk exits. Those controls reacted more often but did not improve net risk-adjusted performance.

Signals are observed after the close and modeled no earlier than the next regular-session open. Overnight gaps remain real losses; no exit rule guarantees a price.
