# Validation protocol

## Frozen partitions

- Development: start through 2018-12-31.
- Validation: 2019-01-01 through 2023-12-31.
- Labeled holdout: 2024-01-01 onward.
- Prospective shadow: begins only after the candidate commit is frozen and scheduled snapshots are locked.

The first v1 run opened the labeled holdout. V1 failed because an implementation defect resized
positions daily despite the frozen weekly-rebalance specification. V2 corrects that mismatch, but
the 2024-present interval is now diagnostic only and cannot authorize promotion. A new prospective
shadow interval is the next valid holdout.

## Required comparisons

- Buy and hold SPY.
- SPY above/below its 200-session moving average.
- A simple 12-month long-only dual-momentum rotation.
- The frozen candidate with identical data, timing, and transaction-cost assumptions.

## Required diagnostics

- CAGR, volatility, zero-risk-free Sharpe and Sortino, maximum drawdown, Calmar, worst day, daily 95% expected shortfall, exposure, turnover, and modeled costs.
- Global financial crisis, COVID crash/rebound, 2022 inflation/rate shock, and 2023-present concentration cycle.
- Signal causality and next-session execution tests.
- Parameter-neighborhood stability, cost sensitivity, missing-data tests, and data-revision hashes.
- Broad-asset-only, no-sector, no-QQQ/XLK, and leave-one-asset-out universe sensitivity so current technology winners and surviving funds cannot silently carry the result.
- Block-bootstrap confidence intervals and a multiple-testing/PBO audit before selecting among variants.
- Twelve completed months of adjusted-return agreement across independent providers for every traded ticker; missing, stale, or divergent secondary data fails the action gate.

## Promotion ladder

1. Retrospective research candidate.
2. Frozen prospective shadow with timestamped tickets and no allocation authority.
3. Human review after enough independent decisions cover both risk-on and risk-off conditions.
4. Small manual pilot only after data-source reconciliation, operational alerts, account-rule review, and explicit user approval.

At no stage may a backtest or dashboard place an order automatically.

Each `swing-trader daily` run writes a content-hashed, non-overwriting JSON record under `reports/shadow`. The self-contained schema-v3 record separately embeds and binds the stable strategy/configuration, the full Python-package implementation hash, the exact primary-data snapshot, the data-quality gate, and the hypothetical ticket before later outcomes are known. Run timestamps and changing data endpoints are deliberately excluded from the strategy hash so repeated records can prove whether the specification actually remained frozen. Scheduled artifacts are retained for 90 days; records are also appended to a durable evidence branch.

Prospective scoring deduplicates repeated daily snapshots with the same monthly decision and target weights. It enters at the first adjusted open after the record's as-of close, evaluates 5-, 21-, and 63-session open-to-open outcomes only once those exits exist, and deducts the frozen one-way turnover cost. Records whose original independent-data gate failed remain visible as operational evidence but are excluded from eligible performance summaries.
