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
   point-in-time shares outstanding is required to reproduce their characteristic exactly. George
   and Hwang report that nearness to the 52-week high contains information beyond conventional
   momentum.
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

### Practical paid research tier

Bloomberg is not required. The preferred first evaluation is **Sharadar Prices** because its direct
API/bulk service advertises active and delisted securities, total-return-adjusted prices, corporate
actions, ticker changes, and S&P 500 constituent history in one Mac-compatible dataset. It covers
21,000 stock tickers back to 1997. Public pricing is not exposed without an account, so no purchase
is assumed: https://sharadar.com/bundle

**Massive Stocks Advanced** is the better candidate if minute bars become necessary. As reviewed on
2026-10-07 it costs USD 199 per month and includes all U.S. tickers, corporate actions, flat files,
and 20+ years of history. The cheaper USD 79 tier has only ten years, which omits two major market
regimes in this protocol: https://massive.com/pricing?product=stocks

**Norgate US Stocks Platinum** advertises delisted securities, historical index constituents, and
history to 1990 for USD 346.50 per six months or USD 630 per year. It is attractive on price, but its
official Python and Zipline integrations are Windows-only; ASCII export would add a fragile manual
step on this Mac: https://norgatedata.com/stockmarketpackages.php and
https://norgatedata.com/accessibility.php

Every candidate must pass the code-level coverage audit before use: no missing historical member,
at least 99% member-date price coverage, sufficient pre-membership history for the signal warm-up,
verified adjustment semantics, and explicit ticker/corporate-action mapping. Provider marketing is
not treated as validation evidence.

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
8. Begin a new prospective shadow only after the data and implementation hashes are frozen.

No candidate becomes actionable merely because it has a higher retrospective CAGR.

## Implemented research scaffold

- `stock_signals.py` calculates the three causal feature families from dated OHLCV and mandatory
  point-in-time membership.
- `stock_strategy.py` maintains next-open entry prices, close-based high-water marks, holding age,
  rank decay, and a multi-reason BUY/HOLD/SELL ledger suitable for the explainability dashboard.
- `stock_data.py` and `swing-trader data audit-stocks` fail closed on missing historical members,
  member-date gaps, or inadequate 252-session warm-up.
- The shared backtester now lets holdings drift between real trades and credits unallocated capital
  with the configured Treasury-bill proxy instead of silently assuming free daily rebalancing and
  zero-return cash.

The remaining critical path is provider evaluation and ingestion, followed by the preregistered
walk-forward experiment. No individual-stock performance number exists yet because the open Yahoo
panel failed the coverage gate.
