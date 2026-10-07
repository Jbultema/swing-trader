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
| SEC EDGAR | Public, keyless, authoritative filings | Implemented candidate-bound acceptance-time 8-K/10-Q/10-K/Form 4/6-K/ownership event sidecar | This execution environment currently receives SEC HTTP 403; GitHub-hosted access is tested separately and failure never changes ranks/state |
| FRED | Public, keyless CSV | Cash return and macro-regime series | Macro data are not stock-specific catalysts |
| FINRA Reg SHO | Public, keyless daily file; license not assumed open | Implemented candidate-bound off-exchange activity diagnostic | Covers FINRA-reported off-exchange activity, not exchange volume, short interest, or a directional signal |
| Nasdaq Data Link WIKI Prices | Public-domain archive, free account key | Pre-April-2018 replication/cross-check only | Provider discontinued support and explicitly does not recommend it for investment analysis |

Official references:

- Alpha Vantage documentation and free quota: <https://www.alphavantage.co/documentation/> and
  <https://www.alphavantage.co/support/>
- SEC APIs and nightly bulk archives: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
- FINRA Reg SHO daily volume: <https://developer.finra.org/docs/api-explorer/query_api-equity-reg_sho_daily_short_sale_volume>
  and the consolidated file pattern
  <https://cdn.finra.org/equity/regsho/daily/CNMSshvol20261006.txt>
- FINRA's explanation of daily short-sale volume versus short interest:
  <https://www.finra.org/investors/insights/short-interest>
- Public-domain WIKI archive status: <https://data.nasdaq.com/databases/WIKIP>
- `pitindex`: <https://github.com/arielNacamulli/pitindex>

## Rejected or conditional sources

- Stooq was historically attractive as a bulk, independent OHLCV source, but its U.S. archive
  returned HTTP 401 in a live 2026-10-07 check and recent access requires an interactive key. It is
  not an unattended dependency.
- Alpha Vantage documents quarterly `SHARES_OUTSTANDING`, which would support the academic
  volume/shares turnover characteristic. A redacted, quota-accounted 2026-10-07 probe using the
  personal free key returned `premium_only`; the hash-verified artifact contains only the response
  fingerprint, schema keys, classification, and row counts. It retains neither the key nor provider
  message. The project therefore does not silently replace share turnover with that paid field.
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
   recorded session. Gaps do not get reconstructed using today's roster. Full-package and config
   provenance remain explicit, while the decision-policy and config hashes define separate
   lineages so changed trading logic cannot be spliced into an old performance path. Neutral data
   collectors and UI changes do not restart an otherwise identical paper portfolio.
4. Evaluate targets at the next available open and at 5, 21, and 63 sessions. Retain failed data
   gates as operational evidence but exclude them from eligible performance. The live paper ledger
   is self-financing: no daily resizing, fractional shares, explicit entry/exit costs, and
   adjustment-safe share rebasing to the prior locked close.
5. Permit historical performance only for date ranges whose point-in-time member observations,
   252-session warm-up, corporate actions, and terminal returns pass the preregistered gates. A
   shorter clean period is preferable to a longer biased one.
6. Treat SEC, FINRA, and future-earnings data as event/risk features captured before a decision;
   never backfill today's event view into old signals.
7. Download FINRA files only for completed sessions already present in the locked price panel.
   Validate the documented header, integer trailer count, date, uniqueness, and volume arithmetic;
   bind the exact source-byte hash to a candidate-only parquet. Keep the resulting 1/5/20-session
   ratios outside ranking and portfolio state until a preregistered prospective experiment has
   enough observations to judge them.
8. Fetch SEC submissions only for CIKs in the exact candidate-bound universe. Use the acceptance
   timestamp as the causal boundary, preserve each response hash, and treat form/item counts as
   neutral event context. Never infer filing sentiment from metadata alone.

Operationally, full-universe Yahoo downloads use batches for speed, then retry incomplete symbols
twice as single-name requests. The 99% current-close gate remains unchanged; retries recover
transient timeouts and rate limits but do not conceal persistent absences. The live 2026-10-07 dry
run recovered a rate-limited SYF request and passed at 503 of 504 latest closes, with WBD retained as
the sole missing symbol.

Yahoo can also publish logically inconsistent daily bars, including a high below a positive open or
a nonpositive field. The prospective loader applies only a conservative, manifest-enumerated repair:
it expands high/low to include positive open/close values and quarantines the entire row otherwise.
It never narrows a range or invents a close. The source-quality gate still fails when any expansion
exceeds 1% of close, when more than 5% of the latest cross-section is affected, or when quarantined
rows exceed 0.01% of the requested history. Scheduled artifacts retain the price and universe
parquets for a 90-day forensic window; the immutable manifest records every original and normalized
value used by the strategy. The 2026-10-07 live proof enumerated 16 range expansions across 3.17%
of the latest cross-section (maximum 0.473% of close), quarantined one nonpositive WBD row, and then
passed the normalized price gate with 502 of 504 latest closes.

The free Alpha quota is shared across the project. ETF monthly reconciliation is refreshed on
Sunday and stored with a capture timestamp and content hash for at most eight days. Stock checks run
after weekday closes. This schedule prevents two individually free workflows from jointly exceeding
the documented 25-call daily allowance.

Prospective monitoring remains deliberately slow. The system will not describe the paper result as
mature before 126 eligible sessions, 30 completed exits, and 10 eligible risk-off sessions. Paired
stationary-bootstrap uncertainty begins only after 21 eligible sessions, uses the same gated dates
for the primary arm and SPY, and remains a diagnostic rather than trading authority.

The primary consensus remains the only Alpha-validated arm. Separate short-volume proxy and exact
share-turnover arms with 5, 10, and 21-session maximum holds use identical next-open accounting,
exit rules, and 50 bp round-trip costs, but only the locked Yahoo snapshots. Their all-session
metrics are visible as unvalidated diagnostics and cannot contribute to the primary evidence
threshold. This preserves the free quota while testing the shorter-holding hypothesis prospectively
instead of selecting a recent winner.
The evaluator separates overnight, explicit execution-cost, and intraday performance for every arm,
then starts paired inference after 21 sessions and family-wide multiple-testing control after 63.
Those additions require no new provider and do not alter the frozen decision lineage.

`short_volume` is a frozen internal identifier for short-horizon price momentum plus a relative
20-versus-126-session volume participation proxy. It is not FINRA short-sale volume and it is not
Medhat-Schmeling share turnover. Exact replication would require point-in-time shares outstanding,
and Alpha Vantage's documented field was premium-only on the tested free key.

The implemented open workaround is prospective rather than historical. `snapshot-stock-shares`
queries Yahoo's unofficial `Ticker.get_shares_full` endpoint for the exact locked current roster,
retains only the latest value, and marks it usable no earlier than its own capture time. Provider
history is counted and discarded, never backfilled into earlier sessions. The immutable parquet and
manifest require the exact roster, positive shares, no future timestamps, at least 99% coverage,
and a maximum 130-day provider-observation age; they bind the current-universe hash and explicitly
disable historical backfill and action authority. `verify-stock-shares` recalculates the gate and
checks both file hashes and the universe binding.

The 2026-10-07 live proof recorded 503 roster rows and 19,263 source observations, discarded 18,760
historical rows, and passed with 500 usable names (99.40%). There were no missing, fetch-error,
invalid, future, duplicate, unexpected, or absent-roster names. ERIE, WAT, and WBD were excluded as
stale. This proves that open data can support a forward-only share-turnover test; it does not make
Yahoo historical shares point-in-time.

The preregistered `share_turnover_skip3` arms now implement that forward test. At each close, the
screen independently ranks the return and share-turnover characteristics over sessions t-20 through
t-3, retains the top quintile of each, and selects at most ten names from their intersection by the
average percentile. Captured shares become eligible only at their manifest timestamp, so the first
lineage starts from cash and cannot manufacture a backtest. The 5-, 10-, and 21-session arms share
the same exits and 50 bp round-trip cost assumption. Their evaluator compares holding horizons
within the exact-signal family; they remain Yahoo-only diagnostics with no action authority and no
performance conclusion until forward transitions accrue.

The practical result is slower than buying a curated database, but it is honest: public and free
data can support a strong prospective system and selected covered historical replications. It
cannot justify a universal survivor-free historical claim when missing names are correlated with
failure.

## Implemented FINRA workaround

`swing-trader data snapshot-finra-activity` retrieves 21 consolidated daily files from FINRA's
public CDN for the exact sessions in the candidate's locked price snapshot. It preserves fractional
reported volumes, maps FINRA's class-share slash only for ticker matching, validates every full
source file before filtering, and records each source URL and byte hash. The normalized snapshot is
bound to the candidate, price, and universe manifests and can be re-audited with
`swing-trader data verify-finra-activity`.

The 2026-10-07 live proof locked 231 rows for 11 candidate/benchmark symbols across 21 completed
sessions with 100% latest-session coverage. One initial run failed safely because the valid ticker
`NA` was interpreted by pandas as a missing-value token; the parser now disables default NA-token
inference and carries a regression test. This is operational evidence for the feed, not evidence of
predictive edge. The nightly workflow records retrieval failure as a nonblocking experimental
diagnostic, and the manifest sets `candidate_ranking_input`, `portfolio_state_input`, and
`action_authorized` to false.

## Implemented SEC workaround

`swing-trader data snapshot-sec-events` requests the official SEC submissions JSON for the exact
CIKs represented by the candidate/benchmark shortlist. It validates the payload CIK, requested
ticker associations, recent-filing array shapes, accession numbers, and acceptance timestamps;
then locks only tracked filings accepted at or before capture. The snapshot preserves response
hashes, diagnostic form/item counts, market-phase timing, and exact candidate/universe/price
bindings. Its manifest disables sentiment, direction, ranking, portfolio-state, and action use.

The official endpoint returned HTTP 403 from this execution environment on 2026-10-07 even with a
declared research user agent. The module and daily nonblocking recovery path are implemented and
tested, but live availability is not claimed until the dedicated GitHub-hosted access probe passes.
The probe writes a retained success/failure artifact; the bot does not silently substitute an
unofficial filing mirror.
