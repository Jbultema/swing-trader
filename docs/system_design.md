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

The scheduled workflow downloads data, reconciles completed-month returns against Alpha Vantage when its repository secret is configured, runs validation, and uploads a read-only artifact. It has `contents: read` permission and no broker credentials. A missing or failed secondary feed leaves hypothetical research visible but invalidates every action. Human execution is a hard system boundary.

## Champion exit hierarchy

1. Absolute-momentum exit: trailing 12-month return becomes non-positive.
2. Scheduled rank exit: the asset leaves the top-three selection at month-end.
3. Cash: if fewer than three assets have positive momentum, the unfilled allocation stays in cash.

The separately reported panic-guard comparator exits all risk positions when 20-session benchmark volatility exceeds 35% or benchmark drawdown from its 200-session high exceeds 12%. It reduced historical drawdown modestly but gave up enough return that it did not become champion.

The rejected fast candidates also tested asset trend and trailing-risk exits. Those controls reacted more often but did not improve net risk-adjusted performance.

Signals are observed after the close and modeled no earlier than the next regular-session open. Overnight gaps remain real losses; no exit rule guarantees a price.
