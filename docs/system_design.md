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

The scheduled workflow downloads data, reconciles completed-month returns against Alpha Vantage when its repository secret is configured, runs validation, locks a content-hashed shadow record, and uploads a read-only artifact. It has `contents: read` permission and no broker credentials. A missing or failed secondary feed leaves hypothetical research visible but invalidates every action. Human execution is a hard system boundary.

## Champion exit hierarchy

1. Absolute-momentum exit: trailing 12-month return becomes non-positive.
2. Scheduled rank exit: the asset leaves the top-three selection at month-end.
3. Cash: if fewer than three assets have positive momentum, the unfilled allocation stays in cash.

The separately reported panic-guard comparator exits all risk positions when 20-session benchmark volatility exceeds 35% or benchmark drawdown from its 200-session high exceeds 12%. It reduced historical drawdown modestly but gave up enough return that it did not become champion.

The rejected fast candidates also tested asset trend and trailing-risk exits. Those controls reacted more often but did not improve net risk-adjusted performance.

Signals are observed after the close and modeled no earlier than the next regular-session open. Overnight gaps remain real losses; no exit rule guarantees a price.
