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
| Fama-French Data Library | Public, keyless aggregate CRSP portfolios | Broad, large-half, and largest-size-quintile prior-return comparators | Gross aggregate returns cannot reveal constituents, turnover, costs, exits, or account-level implementability |
| FINRA Reg SHO | Public, keyless daily file; license not assumed open | Implemented candidate-bound off-exchange activity diagnostic | Covers FINRA-reported off-exchange activity, not exchange volume, short interest, or a directional signal |
| Nasdaq Data Link WIKI Prices | Public-domain archive, free account key | Pre-April-2018 replication/cross-check only | Provider discontinued support and explicitly does not recommend it for investment analysis |
| Tiingo supported-ticker catalog | Public, keyless catalog; historical API needs a zero-dollar account | Implemented feasibility diagnostic for missing PIT labels | Proprietary/internal-use data; catalog includes reserved symbols and does not prove API access or security identity |

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
- Fama-French daily prior-return portfolios and construction details:
  <https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html>
- Tiingo EOD documentation and zero-dollar Starter limits:
  <https://www.tiingo.com/documentation/end-of-day> and
  <https://www.tiingo.com/about/pricing>

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
  terms rather than open data. They are not dependencies for the operating path. Tiingo's public
  catalog is now an implemented research-only feasibility diagnostic, while authenticated history
  remains an optional manual cross-check that must stay local and pass security-identity tests.
- Nasdaq's current historical-price pages are visible without a paid terminal, but Nasdaq's terms
  prohibit automated or manual capture for data-analysis use. Scraping them would not be a durable
  or permissioned research feed: <https://www.nasdaq.com/legal>.
- Financial Modeling Prep and Twelve Data advertise limited free account tiers, but they are
  proprietary keyed services, not open datasets. They add another quota and terms dependency
  without solving survivor-free historical membership, so they are not adopted as fallbacks.
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
   Register a simple 12-1 current-roster stock-momentum arm as a Yahoo-only control so the composite
   and shorter-horizon rules must demonstrate incremental value under identical portfolio rules.
   Register a second consensus comparator that combines the benchmark guard with 20-session
   advance/decline breadth. It uses the already locked full cross-section, requires 95% coverage,
   applies a symmetric 45%/55% exit/re-entry hysteresis band to control churn, and starts only from
   capture-forward evidence rather than projecting today's roster backward.
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
9. Use `swing-trader data audit-tiingo-catalog` only to bound a possible free-account recovery.
   The audit excludes provider rows whose documented date bounds are null, uses end-exclusive PIT
   membership, quarantines recycled labels whose stored prices begin after index exit, and records
   exact-label and anti-recycling-suffix matches separately. It always emits
   `historical_backtest_ready=false` and `action_authorized=false`: Tiingo states that its public
   catalog includes reserved symbols, so only authenticated metadata and historical bars can prove
   availability, and neither can prove that a recycled ticker refers to the intended security.
10. Probe and acquire the official public-domain WIKI archive only through the ignored local
    `NASDAQ_DATA_LINK_API_KEY`. `probe-nasdaq-wiki` retains no credential or returned price, while
    `download-nasdaq-wiki` hashes and schema-checks the raw ZIP and still labels it pending identity
    and coverage audit. The keyless endpoint returned HTTP 403 on 2026-10-08 UTC, so the project
    does not pretend the archive can be reproduced unattended without a free account.

### WIKI supplement feasibility result

The public WIKI ticker list suggested meaningful overlap, so a second diagnostic examined the
unlicensed `teddykoker/survivorship-free-spy` research archive without importing or committing it.
Its 18 MiB ZIP (`38f59346018c8ffab5a2ad97843231f15b3988fb52798f0552ea153752b42af4`)
contains 617,642 adjusted OHLCV rows for 639 labels from 2013-02-28 through 2018-02-28,
derived by its author from WIKI and Yahoo.

Merged with the locked October 2026 research panel, exact point-date coverage rose to 99.8% on
2015-01-05 and 100% on 2017-01-03. Across all SPY sessions from 2014-01-02 through 2026-09-29,
the diagnostic reached 99.31% member-date coverage and 98.75% 252-session warm-up coverage. It
still failed the production research gate because BTUUQ-201704, FRC, HFC, and LIFE-201402 had no
accepted mapped series. FRC is especially non-negotiable: excluding the S&P 500 member that failed
during the 2023 bank crisis would directly flatter a loss-avoidance strategy.

This establishes that the official free-key WIKI archive is worth acquiring and auditing, not that
the community ZIP is an accepted source. WIKI ends before FRC joined the index, so even the official
archive cannot close every post-2018 failure hole. A historical performance claim remains prohibited
until every missing identity, corporate action, and terminal return is resolved or the tested period
is prospectively bounded without selecting it from performance.

### Explicit identity-recovery result

The two post-2018 holes are recoverable from Yahoo only through explicit, reviewed mappings—not by
stripping suffixes or joining on ticker text:

- `FRC -> FRCB`: OTC Markets identifies FRCB as First Republic Bank common stock, while the FDIC
  records the bank's May 1, 2023 closure and receivership. Yahoo's FRCB backfill covers every
  reviewed FRC membership session, 252 prior sessions, and the 2023-05-04 removal-session exit.
  Official identity evidence: <https://www.otcmarkets.com/stock/FRCB/overview> and
  <https://www.fdic.gov/resources/resolutions/bank-failures/failed-bank-list/first-republic.html>.
- `HFC -> DINO`: HF Sinclair states that DINO replaced HollyFrontier and that each existing HFC
  share converted one-for-one. Yahoo's DINO backfill covers every reviewed HFC membership session,
  252 prior sessions, and the 2021-06-04 removal-session exit. Official identity evidence:
  <https://www.hfsinclair.com/investor-relations/press-releases/Press-Release-Details/2022/HollyFrontier-and-Holly-Energy-Partners-Announce-Completion-of-Transactions-with-The-Sinclair-Companies-and-Establishment-of-New-Parent-Company-HF-Sinclair-Corporation/default.aspx>.

The live 2026-10-08 audit locked 2,344 local rows. FRCB contained one internally inconsistent
2021-05-05 range: its high failed to enclose another positive OHLC value by 0.5094% of close. The
same conservative policy used by the prospective feed expanded only that high/low envelope, below
the frozen 1% limit, and recorded the affected session and magnitude. HFC required no repair.
Against the base research panel from 2014, the two mappings raised member-date coverage from 96.94%
to 97.06%, warm-up coverage from 96.53% to 96.64%, and reduced completely missing labels from 47
to 45. More importantly, they remove FRC and HFC from the four-label remainder of the earlier WIKI
feasibility merge; `BTUUQ-201704` and `LIFE-201402` remain.

Those final two labels require different treatment. Peabody's SEC-filed release says old BTUUQ
common stock was extinguished for no value on 2017-04-03 and the new BTU was new equity, so a modern
BTU series must never be spliced onto the predecessor. Only time-bounded pre-removal WIKI rows can
be considered: <https://www.sec.gov/Archives/edgar/data/1064728/000119312517108650/d368865dex991.htm>.
Thermo Fisher states that Life Technologies closed as a $76.13-per-share cash acquisition on
2014-02-03, providing an official terminal-action fact but not the missing daily path:
<https://ir.thermofisher.com/investors/news-events/news/news-details/2014/Thermo-Fisher-Scientific-Completes-Acquisition-of-Life-Technologies-Corporation-2014-2-3/default.aspx/1000/>.

`snapshot-historical-identities` writes the recovered rows and a hash-bound manifest only under an
ignored local directory. The verifier proves bytes, schema, reviewed membership intervals, coverage,
warm-up, exit-session availability, and bounded repairs. It deliberately leaves
`historical_backtest_ready=false`: Yahoo is unofficial, the surrounding downloaded panel remains
unlicensed research input, and price-level cross-source reconciliation, corporate actions, and
terminal returns still require separate acceptance.

### Tiingo catalog feasibility result

The 2026-10-08 diagnostic used the public Tiingo catalog and the locally held October 2026
`Johnbrick123/sp500-data` release as research input. The latter remains unadopted because it has no
clear license and explicitly reports incomplete old-history coverage. The audit reproduced its
published PIT denominator and price-range coverage after quarantining 14 recycled ticker labels.
The locked input hashes were `8b0537814ea8685af4832d07934a87fdae7c94f27e6812477ce326e9dcc9ecb3`
(membership), `2662a45c8f74144f17da208f3db45e7cd0efefd5c87cf9f95f166b90960ab94f`
(prices), and `8f871fe2a5914a581b4bc2ded4d9f757fc1ac5a53bcfd9ad8b4cf8092e8858ff`
(catalog ZIP).

| PIT date | Existing range coverage | Catalog-only upper bound | Catalog hints using suffixed labels |
| --- | ---: | ---: | ---: |
| 1996-01-02 | 43.9% | 49.8% | 28 of 29 |
| 2005-01-03 | 64.8% | 70.9% | 28 of 30 |
| 2010-01-04 | 82.2% | 90.3% | 35 of 40 |
| 2015-01-05 | 91.9% | 96.4% | 18 of 22 |
| 2019-01-11 | 98.0% | 98.2% | 0 of 1 |
| 2024-09-23 | 99.6% | 99.8% | 0 of 1 |

This rejects the optimistic hypothesis that the public catalog alone unlocks a long, survivor-safe
stock backtest. Before 2019 it leaves material gaps, and most apparent additions reuse a base ticker
that may now identify a different company. Even the upper bound says nothing about returned bars,
corporate-action completeness, terminal returns, or API eligibility. A free token could test the
remaining post-2014 candidates locally, but it cannot make the pre-2014 sample rigorous by itself.

Operationally, full-universe Yahoo downloads use one batch for speed, retry the incomplete subset
collectively, and then allow at most ten residual single-name requests. The 99% current-close gate
remains unchanged; bounded retries recover transient timeouts and rate limits without turning a
bad cross-section into hundreds of serial provider calls. The latest hosted 2026-10-07 run passed
at 501 of 504 current closes (99.40%) and retained every absence explicitly.

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

The primary consensus remains the only Alpha-validated arm. A coverage-gated market-breadth guard,
a simple 12-1 stock-momentum control with a 21-session maximum hold, plus separate short-volume
proxy and exact share-turnover arms with 5, 10, and 21-session maximum holds, use identical
next-open accounting, exit rules, sector caps, and 50 bp round-trip costs but only the locked Yahoo
snapshots. Their all-session metrics are visible as unvalidated diagnostics and cannot contribute
to the primary evidence threshold. This preserves the free quota while testing drawdown control,
complexity, and the shorter-holding hypothesis prospectively instead of selecting a recent winner.
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
checks both file hashes and the universe binding. The default provider collection is isolated in a
child process and bounded to ten minutes. A deadline breach preserves completed rows, labels every
unresolved name `collection_timeout`, fails the experimental coverage gate when appropriate, and
still lets the independent primary path continue.

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

The signal is an operational adaptation, not a claimed replication. Medhat and Schmeling study
value-weighted long-short corner portfolios across broad common-stock universes and report their
main effect over roughly 21 sessions. This project is long-only, selects at most ten current S&P 500
names, equal weights them, applies a three-name GICS sector entry cap, and permits disciplined early
exits. The 21-session arm is therefore the closest reference; 5 and 10 sessions test whether the
user's faster-holding hypothesis survives rather than assuming it does. A capped candidate is not
replaced by a lower-ranked stock, so unfilled risk remains cash.

Share-turnover availability is deliberately not a primary-consensus dependency. If a freshly
captured share artifact is intact and bound to the current universe but fails its coverage,
observation-age, or provider-source gate, the consensus and relative-volume proxy arms still
advance. Exact arms take no new entries and schedule every held name for a next-open fail-safe exit.
The evaluator retains those returns in all-session operational metrics but excludes a transition
from exact-signal inference when its prior-close share gate failed. Artifact corruption, a future
capture, excessive capture age, or a mismatched universe remains fatal because the system cannot
prove what information was available.

The practical result is slower than buying a curated database, but it is honest: public and free
data can support a strong prospective system and selected covered historical replications. It
cannot justify a universal survivor-free historical claim when missing names are correlated with
failure.

The hosted price collector requests the current roster in one batch and retries incomplete latest
rows collectively before allowing at most ten residual single-name calls. The job is scheduled at
06:30 UTC so the previous U.S. session has settled and both UTC and U.S. Eastern midnight have
passed. Alpha Vantage documents the 25-request free daily allowance but not its reset boundary, so
the workflow does not infer that UTC midnight restores quota. Neither recovery step relaxes the
99% current-close gate; a partial provider cross-section still produces no state.

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

The feed remains context-only after literature review. FINRA explains that the daily file covers
publicly disseminated off-exchange transactions, omits some offsetting activity, is not consolidated
with exchange data, and is not short interest. Diether, Lee, and Werner find daily short flow can
predict negative abnormal returns, while Daske, Richardson, and Tuna find no reliable daily lead.
That combination does not justify assigning a bullish or bearish label to this partial feed:

- FINRA Information Notice 5/10/19:
  https://www.finra.org/sites/default/files/2019-07/information-notice-051019.pdf
- Diether, Lee, and Werner, *Can Short-Sellers Predict Returns? Daily Evidence*:
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=761724
- Daske, Richardson, and Tuna, *Do Short Sale Transactions Precede Bad News Events?*:
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=722242

## Implemented SEC workaround

`swing-trader data snapshot-sec-events` requests the official SEC submissions JSON for the exact
CIKs represented by the candidate/benchmark shortlist. It validates the payload CIK, requested
ticker associations, recent-filing array shapes, accession numbers, and acceptance timestamps;
then locks only tracked filings accepted at or before capture. The snapshot preserves response
hashes, diagnostic form/item counts, market-phase timing, and exact candidate/universe/price
bindings. Its manifest disables sentiment, direction, ranking, portfolio-state, and action use.

The official submissions API returned HTTP 403 from this execution environment on 2026-10-07 even
with a declared research user agent. A hosted probe also received 403 from all four official routes
tested: submissions JSON, latest-filings RSS, the daily master index, and the nightly submissions
bulk archive. SEC documents the APIs and bulk archives as public and keyless, but its automated-tool
policy permits traffic controls, so availability from this network is not claimed:
<https://www.sec.gov/search-filings/edgar-application-programming-interfaces> and
<https://www.sec.gov/files/privacy.htm>. The module and daily nonblocking recovery path remain
implemented and tested. A well-formed provider-access failure is retained and warned without a red
run, while a missing or malformed report fails the workflow. The bot does not silently substitute
an unofficial filing mirror.
