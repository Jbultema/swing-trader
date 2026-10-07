# No-paid-data research plan

## Policy

The stock system will not purchase or depend on Bloomberg, Sharadar, CRSP, Norgate, Massive,
commercial Nasdaq products, or any other paid feed. Open-source software does not make its
underlying data open, so every source is classified separately by cost, access, license, coverage,
and decision role. Raw provider data stays in ignored local artifacts and is never redistributed.

This boundary makes a complete long-history, survivor-free U.S. equity panel difficult. The system
will not hide that limitation. It substitutes strict coverage gates and prospective evidence for a
backtest whose missing failures would mechanically flatter performance.

## Accepted sources

| Source | Cost/access | Role | Hard limitation |
| --- | --- | --- | --- |
| Wikipedia current S&P 500 table | Public, no key | Current roster, names, sectors, CIKs | Current observation only; never historical backfill |
| `pitindex` | MIT package built from public sources | Point-in-time membership research and independent roster diagnostic | Delisted identity/action provenance is incomplete; the published wheel updater is currently broken |
| Yahoo via `yfinance` | Public unofficial endpoint, no key | Full-universe adjusted OHLCV screening and immutable daily snapshots | No service guarantee; missing/recycled delisted symbols make old stock backtests incomplete |
| Alpha Vantage free tier | Free personal key, 25 calls/day | Candidate/holding-only independent daily checks and one bulk earnings calendar call | Cannot validate all 500 names every day; adjusted daily history is not on the free endpoint |
| SEC EDGAR | Public, keyless, authoritative filings | Acceptance-time 8-K/10-Q/10-K/Form 4 events and as-filed facts | This execution environment currently receives SEC HTTP 403; scheduled GitHub-hosted access must be tested separately |
| FRED | Public, keyless CSV | Cash return and macro-regime series | Macro data are not stock-specific catalysts |
| FINRA Reg SHO | Public API | Experimental daily short-volume participation feature | Covers FINRA-reported off-exchange activity, not total short interest or a direct bearish signal |
| Nasdaq Data Link WIKI Prices | Public-domain archive, free account key | Pre-April-2018 replication/cross-check only | Provider discontinued support and explicitly does not recommend it for investment analysis |

Official references:

- Alpha Vantage documentation and free quota: <https://www.alphavantage.co/documentation/> and
  <https://www.alphavantage.co/support/>
- SEC APIs and nightly bulk archives: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
- FINRA Reg SHO daily volume: <https://developer.finra.org/docs/api-explorer/query_api-equity-reg_sho_daily_short_sale_volume>
- Public-domain WIKI archive status: <https://data.nasdaq.com/databases/WIKIP>
- `pitindex`: <https://github.com/arielNacamulli/pitindex>

## Rejected or conditional sources

- Stooq was historically attractive as a bulk, independent OHLCV source, but its U.S. archive
  returned HTTP 401 in a live 2026-10-07 check and recent access requires an interactive key. It is
  not an unattended dependency.
- A new GitHub `sp500-data` project publishes useful verification ideas, but it has no clear data or
  code license, has no established adoption, and its own README reports only 76% historical-member
  price coverage in 2015. It is research input, not imported data.
- Alpaca and Tiingo both offer zero-dollar accounts, but they are proprietary services with account
  terms rather than open data. They are not dependencies or planned fallbacks for the operating
  path; their documentation is useful only for evaluating whether an optional manual cross-check
  would add information.
- GitHub or Kaggle price dumps with unclear provenance, licenses, adjustment rules, or update
  processes are not accepted merely because they are downloadable.

## Workarounds that preserve scientific validity

1. Screen the full current universe from one immutable primary snapshot, combine the three frozen
   families into one preregistered top-ten consensus, then independently check only its current
   holdings, new candidates, and SPY. The worst case is 21 daily-price calls; one bulk calendar call
   still fits within Alpha Vantage's documented free allowance.
2. Keep one consensus portfolio as the primary prospective arm. Preserve the market-guard version
   as a diagnostic comparator, not a second independently optimized portfolio that can exhaust the
   free validation budget. Multiple-testing and family-wide uncertainty still apply to historical
   research.
3. Start every stock strategy from cash. Each later paper state must follow the immediately prior
   recorded session. Gaps do not get reconstructed using today's roster. Implementation and config
   hashes define separate lineages, so changed logic cannot be spliced into an old performance path.
4. Evaluate targets at the next available open and at 5, 21, and 63 sessions. Retain failed data
   gates as operational evidence but exclude them from eligible performance.
5. Permit historical performance only for date ranges whose point-in-time member observations,
   252-session warm-up, corporate actions, and terminal returns pass the preregistered gates. A
   shorter clean period is preferable to a longer biased one.
6. Treat SEC, FINRA, and future-earnings data as event/risk features captured before a decision;
   never backfill today's event view into old signals.

Operationally, full-universe Yahoo downloads use batches for speed, then retry incomplete symbols
twice as single-name requests. The 99% current-close gate remains unchanged; retries recover
transient timeouts and rate limits but do not conceal persistent absences. The live 2026-10-07 dry
run recovered a rate-limited SYF request and passed at 503 of 504 latest closes, with WBD retained as
the sole missing symbol.

The free Alpha quota is shared across the project. ETF monthly reconciliation is refreshed on
Sunday and stored with a capture timestamp and content hash for at most eight days. Stock checks run
after weekday closes. This schedule prevents two individually free workflows from jointly exceeding
the documented 25-call daily allowance.

The practical result is slower than buying a curated database, but it is honest: public and free
data can support a strong prospective system and selected covered historical replications. It
cannot justify a universal survivor-free historical claim when missing names are correlated with
failure.
