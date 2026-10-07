# Individual-stock short-horizon research program

## Objective

Test whether liquid U.S. stocks exhibit implementable continuation over roughly one week to three
months, then exit when the trend, rank, or risk state deteriorates. Every signal is observed after
the regular-session close and can execute no earlier than the next regular-session open. This is a
research program, not an authorization to trade.

## Evidence-backed signal families

The initial registry deliberately contains three interpretable families rather than a large
technical-indicator search:

1. **Short-horizon, volume-conditioned momentum** combines the prior 21-session return, nearness
   to the 52-week high, and recent versus baseline volume. Medhat and Schmeling report that
   high-turnover stocks exhibit short-term momentum while low-turnover stocks exhibit reversal.
   The implemented volume ratio is a participation proxy, not share turnover; a provider with
   point-in-time shares outstanding is required to reproduce their characteristic exactly. Alpha
   Vantage's documented `SHARES_OUTSTANDING` endpoint returned `premium_only` to a redacted,
   quota-accounted free-key probe on 2026-10-07. The open workaround captures only the latest Yahoo
   shares value and permits its use from capture forward; it does not backfill provider history.
   The first 503-name capture passed with 500 usable observations (99.40%), while ERIE, WAT, and WBD
   failed the 130-day age limit. The frozen internal name `short_volume` means short-horizon plus
   relative volume; it never means FINRA short-sale volume or the new, not-yet-integrated academic
   share-turnover signal. George and Hwang report that nearness to the 52-week high contains
   information beyond conventional momentum.
2. **Smooth momentum** combines 12-1 and 63-session return ranks with the Da-Gurun-Warachka
   information-discreteness measure. It favors gains accumulated through many small moves rather
   than a few jumps.
3. **Volume-confirmed breakout** combines nearness to the 52-week high, medium-horizon return, and
   abnormal volume. It is a preregistered hypothesis, not an assumed profitable chart pattern.

Primary literature:

- Medhat and Schmeling, *Short-Term Momentum*, Review of Financial Studies (2021):
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3150525
- George and Hwang, *The 52-Week High and Momentum Investing*, Journal of Finance (2004):
  https://doi.org/10.1111/j.1540-6261.2004.00695.x
- Da, Gurun, and Warachka, *Frog in the Pan*, Review of Financial Studies (2014):
  https://academicweb.nd.edu/~zda/Frog.pdf
- Blitz, Huij, and Martens, *Residual Momentum*, Journal of Empirical Finance (2011):
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2319861
- Kaminski and Lo, *When Do Stop-Loss Rules Stop Losses?*, Journal of Financial Markets (2014):
  https://dspace.mit.edu/handle/1721.1/114876

Post-earnings-announcement drift remains a second-stage family. It requires point-in-time analyst
expectations or a carefully defined standardized-unexpected-earnings signal. Published evidence
also warns that much of the apparent payoff is concentrated in illiquid stocks and can be consumed
by trading costs.

## Exit hypotheses

Each entry family is crossed with the same limited exit registry:

- hard close-to-entry loss limit;
- close-based ATR trail from the post-entry high-water mark;
- loss of the 50-over-200-session trend state;
- non-positive 21-session momentum;
- cross-sectional rank falling below twice the entry breadth; and
- a maximum 63-session holding period.

Stops are evaluated at a close and executed at the next open. The simulator never assumes a stop
fills at its threshold through an overnight gap. Multiple simultaneous reasons are retained for the
dashboard rather than collapsed into a generic `SELL`.

## Data-source findings

### Open-data prototype

- `pitindex` reconstructs point-in-time S&P 500 membership from 2005 and documents its free-source
  caveats: https://github.com/arielNacamulli/pitindex
- SEC EDGAR submissions and XBRL facts are free, timestamped, keyless, and updated throughout the
  day: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- FINRA publishes keyless consolidated daily short-sale-volume files. They describe only trades
  reported to FINRA facilities and are neither consolidated exchange volume nor short interest:
  https://developer.finra.org/docs/api-explorer/query_api-equity-reg_sho_daily_short_sale_volume
- Yahoo through `yfinance` is adequate for a disposable prototype and provider cross-checks, but it
  is an unofficial endpoint and cannot guarantee complete delisted-symbol history.
- Alpaca provides free historical U.S. equity data since 2016, but its free real-time feed is IEX
  only. Full consolidated real-time coverage is a paid tier:
  https://docs.alpaca.markets/docs/about-market-data-api

This tier can falsify weak ideas cheaply. It cannot support a strong claim of survivor-free
historical performance because removed constituents may lack complete prices or corporate actions.
That limitation is material, not theoretical: a 2026-10-07 audit requested all 976 unique tickers
in the reconstructed S&P 500 history from 2005 onward. Yahoo returned no history for 296 (30.3%)
and fewer than 252 sessions for 313. The missing set contains many acquisitions, bankruptcies, and
ticker changes. Backtesting only the 680 names Yahoo happened to return would condition the result
on survival and is prohibited by the validation contract.

### No-paid-data boundary

Paid feeds are out of scope. The project has no active Bloomberg, Sharadar, CRSP, Norgate, Massive,
Trading Economics, or other paid-data adapter or CLI entry point.

The open-data workaround is deliberately asymmetric. Current-universe screening is feasible with a
fresh public roster and one immutable Yahoo snapshot. Independent checking is then focused on the
small set of names that could actually be held or bought, which fits Alpha Vantage's free 25-call
daily quota. Historical performance is reported only for intervals that pass the same 99%
point-in-time member-date coverage, warm-up, identity, corporate-action, and terminal-return gates.
An interval that fails does not get a polished performance number.

Several apparent alternatives were rejected after live review. Stooq's U.S. bulk archive returned
HTTP 401 on 2026-10-07 and now requires an interactive access key. A promising new GitHub price
panel has no clear license and acknowledges only 76% historical-member coverage in 2015. Nasdaq's
public-domain WIKI Prices archive ends in April 2018 and its publisher explicitly no longer
recommends it for investment analysis. These can inform replication checks, not become hidden
sources of truth. Full findings and the prospective workaround are documented in
`docs/open_data_plan.md`.

### Forward-looking information

Price and volume are continuation indicators, not literally forward-looking information. The
research queue distinguishes information that was genuinely knowable before a decision:

- SEC filing acceptance times are open, authoritative event timestamps. Filing text and XBRL facts
  can support post-disclosure continuation but do not supply analyst expectations.
  https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- Alpha Vantage supplies future earnings calendars, earnings history, estimates/revisions, and news
  sentiment. Its free 25-call daily allowance is enough for a bulk calendar snapshot and spot
  checks, not a daily point-in-time estimates history for roughly 500 stocks.
  https://www.alphavantage.co/documentation/
- SEC as-filed XBRL facts can support a point-in-time standardized-unexpected-earnings family
  without analyst estimates. It remains a second-stage experiment because filing concepts,
  amendments, and fiscal periods require issuer-level reconciliation rather than a naive wide join.

There is no accepted free source for a complete historical analyst-consensus vintage panel. The
workaround is to use SEC acceptance timestamps and as-filed facts for a separately preregistered
fundamental-surprise family, and to capture future earnings dates prospectively for risk control.
No consensus estimate is inferred or backfilled.

Future earnings dates may be used prospectively to explain gap risk or block new entries, but they
cannot be backfilled from today's calendar and presented as historical evidence.

The implemented `swing-trader data snapshot-earnings` command writes a timestamped, hashed,
non-overwriting Alpha Vantage calendar and labels its manifest
`prospective_event_risk_only_not_historical_backfill`. The parser fails closed on unexpected
schemas, empty responses, or malformed rows. On 2026-10-07 the configured endpoint returned the
documented header followed by a malformed character-wise row, consistent with a provider/quota
error, so no live snapshot was accepted. When a valid snapshot is available, pre-market and
after-close events create appropriately shifted new-entry blackouts; existing positions remain
governed by the preregistered price, rank, ATR, loss, and time exits.

## Open-source implementation findings

- Microsoft Qlib offers point-in-time data abstractions, factor pipelines, walk-forward modeling,
  portfolio construction, and nested execution: https://github.com/microsoft/qlib
- QuantConnect LEAN is a mature event-driven engine with corporate-action and security-master
  concepts: https://github.com/QuantConnect/Lean
- VectorBT is useful for fast hypothesis sweeps, but speed does not substitute for point-in-time
  universe or event-driven accounting: https://github.com/polakowo/vectorbt
- OpenBB is a provider aggregation layer, not a source of survivor-free truth:
  https://github.com/openbq-org/OpenBB
- SEC filing parsers such as `edgar-crawler` can accelerate text extraction, but the authoritative
  timestamp and filing payload remain EDGAR: https://github.com/lefterisloukas/edgar-crawler

The project will keep its small auditable engine for the first experiments. Qlib and LEAN are
comparison/reference implementations, not dependencies until parity tests demonstrate a need.

## Validation contract

1. Correct the simulator so holdings drift between actual trades and idle capital earns cash.
2. Freeze the experiment registry before downloading the sealed-test interval.
3. Require point-in-time membership and retain delisted names, mergers, ticker changes, and failed
   downloads as explicit records.
4. Model next-open execution, bid-ask/slippage tiers, position limits, and volume participation.
5. Compare with SPY, an equal-weight point-in-time universe, and classic 12-1 stock momentum.
6. Report selection, validation, and sealed-test periods separately; use block bootstrap, PBO, and
   false-discovery controls across the complete candidate family.
7. Reject any result carried by microcaps, one sector, a few AI-era winners, or unavailable names.
8. Begin a new prospective shadow only after the data, decision-policy, and evaluation hashes are
   frozen and separately recorded.

No candidate becomes actionable merely because it has a higher retrospective CAGR.

### Statistical validation details

The configured inference settings are frozen with the experiment family: 2,000 stationary
bootstrap resamples, a 21-session mean block, eight PBO time partitions, and a 5% false-discovery
level. The implementation applies one bootstrap index to every variant so serial dependence and
cross-strategy correlation are not broken. It reports:

- centered one-sided excess-return tests for every capacity-passing variant;
- a family-wide p-value for the best observed t-statistic;
- conservative Benjamini-Yekutieli q-values, chosen because related signal/exit variants are not
  independent;
- a transparent CSCV-style approximate PBO showing how often an in-sample winner ranks below the
  out-of-sample median; and
- a paired stationary-bootstrap interval for the stitched expanding walk-forward path versus SPY.

The stationary bootstrap follows the dependence-preserving motivation of Politis and Romano:
https://doi.org/10.1080/01621459.1994.10476870. The multiplicity controls follow Benjamini and
Hochberg's false-discovery framework and the dependency extension of Benjamini and Yekutieli:
https://doi.org/10.1111/j.2517-6161.1995.tb02031.x and
https://doi.org/10.1214/aos/1013699998. PBO is explicitly labeled approximate because the small,
correlated registry is not a guarantee that every formal asymptotic assumption holds:
https://escholarship.org/uc/item/4w1110bb.

The selected strategy also receives split-specific ticker-level gross P&L attribution and hostile
regime reports. Large top-one/top-five contribution shares expose dependence on a few historical
winners. Current sector or "AI" labels are intentionally not projected backward; a sector claim
requires dated point-in-time classifications, not today's company narrative.

## Implemented research scaffold

### Published open benchmark readout

The command `swing-trader research published-momentum` locks the official Fama-French daily
value-weighted prior-return deciles, size-by-prior-return portfolios, and daily market/risk-free
series. These portfolios are constructed daily from the CRSP universe and currently run through
2026-08-31. They are an open, published gross comparator rather than a reconstruction from the
project's incomplete historical stock panel.

The 2026-10-07 snapshot produced the following gross CAGRs:

| Window | Market | Broad 12-2 winners | Large 12-2 winners | Large prior-month winners | Large prior-month losers |
| --- | ---: | ---: | ---: | ---: | ---: |
| Since 2010 | 14.34% | 17.43% | 14.96% | 13.65% | 15.03% |
| Since 2020 | 15.46% | 19.27% | 15.45% | 17.32% | 16.13% |
| Since 2022 | 11.98% | 21.67% | 13.53% | 14.98% | 15.45% |

The liquid large-cap control is the key result. The spectacular recent broad 12-2 winner return did
not survive at anything close to the same magnitude among large stocks. Large prior-month winners
show a modest recent gross advantage, but they lag the market over the longer post-2010 window. From
2022 their 19.14% volatility and -28.66% drawdown were also worse than the market's 17.95% and
-25.45%. Daily reconstitution turnover is unavailable, so no cost-adjusted advantage can be
claimed. This supports a separately measured, high-participation short-horizon prospective arm; it
does not support replacing the consensus arm or promising unusually high returns. The prospective
state now records three such diagnostics from the same `short_volume` ranking with maximum holding
periods of 5, 10, and 21 sessions. Every arm uses the same next-open execution, exits, and 50 bp
round-trip cost. They are deliberately excluded from primary eligible performance because the free
Alpha quota validates only the consensus portfolio.

Official construction details and downloads:

- https://mba.tuck.dartmouth.edu/pages/faculty/ken.French/Data_Library/det_10_port_form_pr_12_2_daily.html
- https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html

### Prospective session and experiment attribution

Lou, Polk, and Skouras document that momentum-strategy returns can accrue differently overnight and
intraday, including offsetting behavior between the two sessions. That finding motivates a
measurement diagnostic, not a new trading rule. The prospective evaluator multiplicatively links
each arm's prior-close-to-open gross return, explicit next-open trading-cost drag, and post-cost
open-to-close return and fails if those components do not reconcile to the paper account. SPY uses
the same decomposition and its modeled entry cost.

The 5/10/21-session short-volume arms receive paired stationary-bootstrap comparisons against SPY,
the primary consensus, and the same-signal 21-session reference after 21 complete sessions. A common
stationary bootstrap across the frozen arm family begins only after 63 sessions and applies the
Benjamini-Yekutieli correction for dependent multiple tests. These remain Yahoo-only diagnostics;
neither a favorable interval nor a family-wide discovery can enter the Alpha-validated primary
record or authorize a trade.

- https://personal.lse.ac.uk/polk/research/TugOfWar.pdf
- https://doi.org/10.1016/j.jfineco.2019.03.011

- `stock_universe.py` snapshots the current S&P 500 roster from public sources, binds raw-source
  hashes, records an unavailable SEC cross-check explicitly, and prohibits historical backfill.
- `stock_live_data.py` excludes incomplete sessions, locks adjusted current-roster OHLCV, requires
  99% latest-close coverage, quantifies 252-session signal coverage, and binds the universe hash.
- `stock_candidates.py` freezes all three top-ten screens and their feature-level explanations
  before the next open. The live 2026-10-06 screen produced 18 unique names rather than 30 because
  the families overlap.
- `alpha_validation.py` reserves a local free-quota ledger and checks only primary-arm holdings,
  consensus candidates, and SPY against Alpha Vantage daily closes. It never embeds the API key in
  an artifact.
- `stock_shadow_state.py` advances one immediately consecutive close-to-next-open paper state,
  recomputes adjustment-safe entry bases and high-water marks, records every exit reason, and
  rejects session gaps. The consensus arm is primary; the guarded arm and preregistered 5/10/21
  short-volume holding arms are diagnostic comparators.
- `stock_daily.py` restores a hash-linked decision-policy/config lineage, refreshes the public roster
  and adjusted OHLCV, avoids free-provider calls on holidays, records optional-provider failures,
  and advances the state even when validation is unavailable so failed gates remain observable.
- `stock_prospective.py` reads only an intact prior-record chain, scores self-financing paper equity
  with no survivor resizing, assigns eligibility from the decision made one close earlier, builds a
  same-session costed SPY comparator, evaluates the frozen 5/21/63-session policy windows only when
  every intervening gate passed, decomposes overnight/cost/intraday returns, and starts paired and
  family-corrected uncertainty only after their frozen observation thresholds.
- `stock_signals.py` calculates the three causal feature families from dated OHLCV and mandatory
  point-in-time membership.
- `stock_strategy.py` maintains next-open entry prices, close-based high-water marks, holding age,
  rank decay, optional prospective earnings-entry blackouts, and a multi-reason
  BUY/HOLD/SELL/SKIP ledger suitable for the explainability dashboard.
- `events.py` validates and immutably snapshots prospective Alpha Vantage earnings calendars and
  maps report timing into causal pre-event and post-event session flags.
- `finra_activity.py` validates each full public FINRA file, including its trailer count and volume
  arithmetic, then locks 21 sessions of candidate-bound 1/5/20-session activity context. It retains
  exact source hashes and is explicitly prohibited from ranking candidates or changing a portfolio
  state. Daily short-sale volume is never labeled short interest or bearish pressure.
- `sec_filing_events.py` validates official submissions JSON by CIK and ticker, applies the EDGAR
  acceptance timestamp as the point-in-time boundary, locks exact source hashes and filing links,
  and exposes neutral filing-event diagnostics without sentiment, ranking, state, or action use.
- `stock_data.py` and `swing-trader data audit-stocks` fail closed on missing historical members,
  member-date gaps, or inadequate 252-session warm-up.
- `execution.py` compares each asset-level trade with median dollar volume known before the open and
  rejects paths above the preregistered 1% ADV limit.
- `stock_research.py` runs the fixed signal/exit registry, required comparators, 20/50/100 bp
  round-trip cost tiers, fixed selection/validation/sealed-test reports, and expanding annual
  walk-forward selection.
- `stock_validation.py` adds common stationary-bootstrap family tests, arbitrary-dependence
  false-discovery adjustment, approximate combinatorial PBO, paired walk-forward uncertainty, and
  held-weight ticker P&L concentration.
- Every stock manifest binds ordered fingerprints of the exact OHLCV, membership, benchmark, cash,
  and terminal-return objects used by the run, plus SHA-256 hashes of every generated evidence
  artifact and the complete installed strategy package.
- `stock_audit.py` and `swing-trader research verify-stocks` fail closed on missing, modified,
  untracked, stale-code, or non-research-authority bundles. The dashboard's stock tab renders only
  after this verification passes.
- `cash.py` uses the official FRED DGS3MO series with a one-observation lag and calendar-day accrual,
  avoiding an implicit 0% cash return before BIL existed.
  https://fred.stlouisfed.org/series/DGS3MO
- The shared backtester now lets holdings drift between real trades and credits unallocated capital
  with the configured Treasury-bill proxy instead of silently assuming free daily rebalancing and
  zero-return cash.

The remaining critical path is accumulating immediately consecutive prospective stock states and
independently validating every candidate or holding within the free quota across enough exits and
both market regimes. The evaluator and dashboard are implemented, but the first lineage has no
mature transition yet. Historical stock performance remains unreported where the open panel fails
its coverage gate; synthetic integration tests prove mechanics, not edge.
