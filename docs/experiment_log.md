# Experiment log

All figures use the same adjusted daily dataset through 2026-10-06, next-open execution, and 10 basis points per unit of one-way turnover.

## Fast v1 — rejected

The frozen specification said weekly rebalance, but position volatility sizing changed every day. The defect produced 36.8 times annual turnover, 1.9% CAGR, 40.6% maximum drawdown, and 0.23 Sharpe. Cumulative modeled transaction costs were 83.7% of starting capital. This result opened the recent diagnostic interval.

## Fast v2 — rejected

V2 corrected weekly scheduling, added a rank buffer, and applied a 1% no-trade band. It still produced 23.4 times annual turnover, 1.8% CAGR, 35.6% maximum drawdown, and 0.22 Sharpe. Faster reaction did not create useful net edge.

## Monthly controls

The simple trailing-12-month, top-three, positive-absolute-momentum control became the research champion. Over the common post-warmup sample it produced 12.2% CAGR, 25.2% maximum drawdown, 0.77 Sharpe, and 6.5 times annual turnover. A daily panic guard lowered maximum drawdown to 22.6% but also lowered CAGR to 9.5% and Sharpe to 0.69. Slow and fast trend-exit variants had still lower risk-adjusted performance.

The champion is a retrospective research result, not a live-trading authorization. The recent interval is diagnostic because it has been inspected. Promotion requires a new prospective shadow record.

## Published time-series-momentum comparator

An unlevered, long-only translation of the published 12-month own-trend rule held every positive-momentum ETF at equal weight and otherwise held cash. It produced 10.14% CAGR, 0.768 Sharpe, 26.47% maximum drawdown, 0.383 Calmar, and 3.08 times annual turnover. This is a constraint-matched comparator, not a replication of the literature's short, volatility-scaled futures portfolios.

The top-three champion produced higher CAGR and Calmar, but its 0.765 Sharpe did not exceed the comparator's 0.768 and its 6.54 times turnover was more than double. The champion's advantage is therefore concentrated return and capital efficiency, not uniform risk-adjusted dominance. The broader comparator remains a lower-turnover prospective challenger.

## Universe and technology dependence

This diagnostic was added after the champion had been selected, so it cannot be used to claim a new holdout winner. QQQ plus XLK represented 21.2% of average modeled exposure and as much as 66.7%. Removing both reduced full-sample CAGR from 12.21% to 10.33%, confirming that technology overlap contributed materially but did not create the entire result.

Removing every sector ETF produced 11.08% CAGR, 0.78 Sharpe, 23.65% maximum drawdown, and 4.65 times annual turnover. The narrower broad-asset-class universe produced 8.58% CAGR. Across 17 leave-one-asset-out variants, CAGR ranged from 10.91% to 12.76%. The result is therefore not dependent on any single current fund, but the all-asset champion's incremental return is partly compensation for sector overlap and concentration. The simpler no-sector variant is a prospective challenger, not a retrospectively promoted replacement.

## Exact share-turnover forward test — initialized, no performance yet

After the 2026-10-07 close, policy lineage
`policy-e9d9cbfb21f1-config-304c8f60d6cb` initialized from cash. The open Yahoo
shares-outstanding gate passed for 500 of 503 current constituents (99.40%); ERIE, WAT, and WBD
were excluded as stale. Independent top quintiles of t-20-to-t-3 return and share turnover
intersected in 31 names. The locked top ten were MRNA, ILMN, P, ON, LITE, SWKS, COHR, GNRC, SMCI,
and RVTY. Separate 5-, 10-, and 21-session maximum-hold arms all recorded those next-open targets.

This record has one state and zero realized transitions, so CAGR, Sharpe, drawdown, hit rate, and
excess return are all undefined. Alpha Vantage and earnings checks failed because the daily free
quota had already been consumed; the primary consensus state therefore failed its eligibility gate.
The exact Yahoo-only diagnostics initialized correctly, FINRA context passed, all candidate/state
hashes verified, and no order was placed. The next completed session is the first possible return
observation; 21 sessions are required before paired intervals and 63 before family-wide inference.
