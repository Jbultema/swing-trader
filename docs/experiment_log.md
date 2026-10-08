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

## Market-breadth guard — hosted initialization, no performance yet

Commit `07af924` preregistered a coverage-gated market-participation comparator before any stock
lineage observed a return. Hosted run `37727637608` initialized lineage
`policy-6aead590b58c-config-0d6519db8fde` from cash after the 2026-10-07 close and durably archived
the hash-valid candidate, state, evaluation, and run records. The public-price gate passed with all
504 requested symbols current and 499 with at least 252 observations. The separate shares gate
passed with 501 of 503 current constituents usable (99.60%); ERIE and WAT were stale.

The existing benchmark guard was risk-on: SPY remained above its 200-session moving average and
below the volatility ceiling. Market breadth disagreed. The current-roster return panel had 99.60%
or better coverage in each of the 20 measured sessions, but only 46.24% of observable constituents
advanced on an average day and only 27.44% advanced on the latest day. Because a new breadth-on
regime requires 55% participation, the diagnostic arm explained all ten consensus candidates as
`market_breadth_guard_risk_off` and retained 100% cash. The primary consensus still recorded nine
targets and 10% cash, while the classic 12-1 arm retained five targets and 50% cash after its sector
cap.

A raw 50% participation threshold would have flipped 42 times in a deliberately non-performance
504-session current-roster behavior diagnostic. The frozen 45% exit / 55% re-entry hysteresis band
reduced that to four, compared with three flips for the benchmark guard. The diagnostic used
today's roster and was used only to reject an operationally costly rule, not to score returns or
select a historical winner.

Alpha Vantage and earnings checks were absent because the repository secret is not configured, so
the primary record remains ineligible. The breadth arm passed its own public-data gate and remains
diagnostic only. FINRA context passed, the SEC sidecar retained its nonblocking 403 failure, and no
order was placed.

This lineage has one initialized state and zero realized transitions. CAGR, Sharpe, drawdown, hit
rate, and excess return are undefined. The next completed session is the first possible return
observation; paired intervals wait for 21 sessions, family-wide inference waits for 63, and
readiness still requires 126 eligible sessions, 30 completed exits, and 10 risk-off sessions.

## Exit-policy arithmetic correction — before any realized transition

The pre-transition exit audit found that the ATR trail multiplied the current ATR fraction by the
post-entry high-water price. That is not the registered close-based Chandelier-style construction:
the ATR fraction is defined relative to the current adjusted close, so it must first be converted
back to price units and then subtracted from the high-water mark. The old expression became
artificially looser as a winner retreated. The prospective path also rebuilt its high-water mark
from closes alone, while the historical path correctly retained a higher entry open, and only the
historical path failed closed when ATR was missing.

The corrected policy uses `high_water - 3 * (current_close * atr_fraction_14d)`, retains the entry
adjusted open in the high-water maximum, and schedules a next-open fail-safe exit when ATR is
unavailable. Historical and prospective ledgers now expose the entry basis, high-water mark, ATR in
adjusted-price units, hard-loss trigger, and ATR trigger. This correction changes the decision-policy
hash and therefore starts a new prospective lineage; it does not splice revised exit rules into the
existing record. The prior lineage had one initialization and zero realized transitions, so no
observed return was discarded and no result was selected. No historical stock panel currently
passes the preregistered coverage gate; if one later does, all retrospective stock metrics must be
generated under the corrected arithmetic before they are quoted.

## Published largest-stock momentum comparator — gross evidence, not a backtest

The official Fama-French comparison was expanded from broad deciles and the largest half of the
market to the largest NYSE-size quintile crossed with prior-return quintiles. The locked report
contains 26,182 daily observations through 2026-08-31, hashes all seven official source archives,
and computes direct winner-minus-loser spreads with 21-lag Newey-West statistics. This is materially
closer to the liquid large-cap target universe, but it remains an aggregate CRSP portfolio with
unknown constituent turnover and no swing-trader exits or execution costs.

The key result is less exciting than the standalone winner CAGRs. From 2020, the largest-quintile
prior-month winner portfolio compounded at 21.56%, but the direct winner-minus-loser characteristic
spread was only 2.56% annualized gross with t=0.25. From 2022, winner CAGR was 18.48%, while the
spread fell to 0.43% with t=0.04. The large-half short-horizon spreads were negative over both
windows. By comparison, largest-quintile 12-to-2-month winner-minus-loser spreads were 8.27% with
t=0.71 since 2020 and 14.75% with t=1.24 since 2022. None is a costed or statistically decisive
implementation result.

This rejects the naive claim that trading recent large-cap winners faster is itself a dependable
edge. It does not falsify the narrower Medhat-Schmeling hypothesis, which conditions short-term
continuation on unusually high share turnover. The frozen 5/10/21-session share-turnover arms and
volume proxy therefore remain prospective diagnostics; they are not promoted or retuned from this
published aggregate comparison.

## Superseded simple 12-1 stock-momentum initialization — retained for audit

Commit `43133d9` preregistered a transparent individual-stock comparator before the first realized
stock transition. Hosted run `37726298791` initialized lineage
`policy-15b6cbe11385-config-e3ec4e243d9c` from cash after the 2026-10-07 close and durably archived
the hash-valid candidate, state, evaluation, and run records. The public-price gate passed with all
504 requested symbols current and 499 with at least 252 observations. The separate shares gate
passed with 501 of 503 current constituents usable (99.60%); ERIE and WAT were stale.

The control ranks eligible current constituents solely by t-252-to-t-21 return, without the
consensus trend or volume-confirmation entry filters. Its raw top ten were SNDK, LITE, MU, MRNA,
WDC, STX, DELL, BE, INTC, and COHR. Eight were Information Technology names. The frozen three-name
sector entry cap therefore retained SNDK, LITE, MU, MRNA, and BE, explicitly skipped the other five
technology names, and left 50% in cash. This is the intended anti-concentration behavior and a
direct warning that even a simple momentum control can become an AI/semiconductor-era bet.

The primary consensus recorded nine next-open targets and 10% cash. Each exact share-turnover arm
recorded seven targets and 30% cash. Alpha Vantage and earnings checks were absent because the
repository secret is not configured, so the primary record is ineligible; the Yahoo-only classic
control and other available experimental arms remain diagnostic only. FINRA context passed and the
SEC sidecar retained its nonblocking 403 failure. No order was placed.

This lineage has one initialized state and zero realized transitions. CAGR, Sharpe, drawdown, hit
rate, and excess return are undefined. The first scientifically useful result begins with the next
completed session, paired intervals wait for 21 sessions, family-wide inference waits for 63, and
readiness still requires 126 eligible sessions, 30 completed exits, and 10 risk-off sessions.

## Superseded exact share-turnover initialization — retained for audit

Before the simple stock-momentum control was preregistered, policy lineage
`policy-fab869db5397-config-3c8429efbbec` initialized from cash. The Yahoo price gate passed with
501 of 504 requested symbols current (99.40%) and 499 with at least 252 observations (99.01%). The
separate shares-outstanding gate passed for 500 of 503 current constituents (99.40%); ERIE, WAT,
and WBD were excluded as stale. Independent top quintiles of t-20-to-t-3 return and share turnover
again selected MRNA, ILMN, P, ON, LITE, SWKS, COHR, GNRC, SMCI, and RVTY before portfolio
constraints. The new three-name sector entry cap left SWKS, COHR, and SMCI as explained
Information Technology `SKIP` rows, so the exact 5-, 10-, and 21-session arms each recorded seven
next-open targets and 30% cash. The primary consensus recorded nine targets and 10% cash after A
became an explained fourth-Health-Care skip.

This record has one state and zero realized transitions, so CAGR, Sharpe, drawdown, hit rate, and
excess return are all undefined. Alpha Vantage and earnings checks still received the provider's
free-limit response, so the primary consensus state is operationally visible but ineligible. The
exact Yahoo-only diagnostics initialized correctly, FINRA context passed, every candidate/state
hash verified, and no order was placed. The next completed session is the first possible return
observation; 21 sessions are required before paired intervals and 63 before family-wide inference.

The same-close earlier lineages were superseded before observing any transition while share-gate
coupling, sector concentration, and public-price recovery were hardened. One hosted run had failed
unchanged 99% coverage after Yahoo returned only 354 of 504 latest closes and the old recovery path
made hundreds of serial requests. A later full-roster collection passed with 501 current closes in
18 seconds. The frozen recovery now retries incomplete names collectively, permits at most ten
residual single-name calls, and runs after both UTC and U.S. Eastern midnight; it never relaxes the
coverage gate. Because none of the superseded lineages observed a return, these changes did not
discard or select on performance.
