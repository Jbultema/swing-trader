# System design

```text
public daily OHLCV -> validation + immutable snapshot
       -> independent adjusted-monthly reconciliation -> fail-closed data gate
       -> features at close
       -> monthly selection + explicit risk variants -> next-open target weights
       -> costed backtest + regime tests -> signed research artifacts
       -> explainable dashboard + human-executed ticket
```

The core is intentionally small. Research code creates immutable artifacts; the dashboard only reads them. This prevents a UI refresh from silently changing historical results or a research score from becoming an order.

## No-paid individual-stock path

```text
public current roster + stale-reference diagnostic
       -> immutable prospective-only universe snapshot
       -> completed-session adjusted OHLCV + 99% coverage gate
       -> three close-known momentum screens -> one frozen consensus
       -> public FINRA activity sidecar (diagnostic only; no ranking/state input)
       -> public SEC filing-event sidecar (diagnostic only; no sentiment/ranking/state input)
       -> free-quota independent checks for primary candidates/holdings only
       -> next-open primary targets + diagnostic market-guard comparator + explicit exits
       -> immutable prospective outcomes; human remains the only executor
```

Every artifact is labeled `data_cost_policy=no_paid_sources`. Current membership is never projected
backward. A historical interval is eligible for performance reporting only if its point-in-time
member-date price and corporate-action coverage passes the same fail-closed audit. Otherwise it is
either a visibly biased diagnostic or omitted.

The primary arm holds at most ten names. Its held-name union with ten new consensus candidates and
SPY requires at most 21 Alpha Vantage daily calls. The market-guard arm cannot expand that quota:
it is labeled as a diagnostic comparator and is never counted as primary prospective performance.
Both arms start from cash and must advance through immediately consecutive recorded sessions.
A decision-policy/config hash names each durable lineage. It covers universe and price handling,
independent gates, candidate construction, signals, exits, and paper accounting. The full package
hash is retained on every state and the evaluator has its own code hash, so UI or neutral sidecar
changes stay visible without restarting the portfolio. The hosted job restores only a matching
policy lineage, records holidays as no-ops before spending optional-provider calls, and fails on a
missed trading session. Public-price batches receive bounded single-symbol retries, but the 99%
close coverage threshold is never relaxed.

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

The scheduled workflow downloads data, reconciles completed-month returns against Alpha Vantage when its repository secret is configured, runs validation, locks a content-hashed shadow record, and uploads a read-only artifact. It has `contents: read` permission and no broker credentials. A missing or failed secondary feed leaves hypothetical research visible but invalidates every action. Human execution is a hard system boundary.

## Champion exit hierarchy

1. Absolute-momentum exit: trailing 12-month return becomes non-positive.
2. Scheduled rank exit: the asset leaves the top-three selection at month-end.
3. Cash: if fewer than three assets have positive momentum, the unfilled allocation stays in cash.

The separately reported panic-guard comparator exits all risk positions when 20-session benchmark volatility exceeds 35% or benchmark drawdown from its 200-session high exceeds 12%. It reduced historical drawdown modestly but gave up enough return that it did not become champion.

The rejected fast candidates also tested asset trend and trailing-risk exits. Those controls reacted more often but did not improve net risk-adjusted performance.

Signals are observed after the close and modeled no earlier than the next regular-session open. Overnight gaps remain real losses; no exit rule guarantees a price.
