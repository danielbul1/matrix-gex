# Matrix Runbook

## Health Checks

Static hosting:

```powershell
Invoke-WebRequest https://<your-site>/data_status.json
```

Flask server:

```powershell
Invoke-WebRequest http://localhost:5000/status
```

Expected healthy states:

- `fresh`: market-hours data is current.
- `off_hours`: market is closed; stale age is not actionable.

Actionable unhealthy states:

- `stale`: the options data is too old during US market hours.
- `future`: data timestamp is ahead of the current market clock.
- `unknown` or `missing`: status generation or data parsing failed.

## Manual Data Refresh

```powershell
$env:LSE_API_KEY = "lse_live_..."
python fetch_lse.py
python tools\build_data_status.py
python tools\smoke_check.py
```

During US market hours, use strict validation before publishing:

```powershell
$env:MATRIX_REQUIRE_FRESH_DATA = "1"
python tools\smoke_check.py
Remove-Item Env:\MATRIX_REQUIRE_FRESH_DATA
```

## Live Dashboard

The dashboard frontend was unified on the Railway copy (2026-07-24). There is
no local dashboard page anymore — open the live UI:

```text
https://api.trytripity.site/matrix/
```

## Local Flask Server

```powershell
$env:LSE_API_KEY = "lse_live_..."
python server.py
```

## Before Commit

```powershell
python -m py_compile server.py fetch_lse.py tools\build_data_status.py tools\smoke_check.py
python tools\build_data_status.py
python tools\smoke_check.py
git diff --check
```

## GitHub Actions

- `Smoke Check` validates dashboard/data contracts on code and data changes.
- `Update LSE Data` fetches options chains from Cboe and candles from London Strategic Edge (Data 3.0), regenerates `data_status.json`, runs strict smoke validation, and commits only validated data.

## Regime Backtest

Scores the regime engine (`matrix_regime.compute_regime`) against what
sessions actually did: for each stored trading day it replays the regime
inputs as of a fixed decision time (default 10:00 ET), records the label,
classifies the realized post-decision outcome (TREND_UP / TREND_DOWN / RANGE /
PIN from explicit threshold rules), and reports hit rates per label, per
force-agreement level, and a confusion matrix. MIXED makes no prediction and
is excluded from every accuracy denominator.

```powershell
python tools\backtest_regime.py --symbol SPY --db C:\path\matrix_flow.sqlite3
python tools\backtest_regime.py --symbol SPY --start 2026-08-01 --end 2026-08-31 --decision-time 10:00 --json
```

Data sources (auto-detected; force with `--source db|history`):

- `--db PATH`: a copy of the old Railway snapshot DB (`matrix_flow.sqlite3`,
  table `matrix_gex_snapshot`: 1-minute spot + total_gex/dex/vex/chex + flip +
  walls per session). The service stopped writing it and deleted it on
  2026-10-26 (ADR 0001), so only a copy taken before then works here.
- `--history-root DIR`: fallback `exposure_history/` JSON written by
  `tools/build_exposure_history.py` (GitHub Actions path; 30-min per-strike
  GEX/DEX only, so VEX/CHEX replay as zero there).
- `--candles PATH`: `candles_data.json`-style intraday OHLC used for day
  outcomes when it covers the session; otherwise outcomes are derived from
  the source's own spot series.

Interpreting the report:

- Hit rates are only meaningful per label: PIN bets on PIN/RANGE, TRAP_DOOR
  on TREND_DOWN, GRIND_UP on TREND_UP-or-upward-drift, SQUEEZE on TREND_UP.
- The agreement breakdown (3/3 vs 1/3) shows whether force alignment adds
  edge; if it does not, the agreement score is dead weight.
- Small-sample caution: with fewer than ~30 scored days a hit rate is noise.
  The report prints a CAUTION line in that case — accumulate history before
  tuning or removing rules on the numbers.
- Replay coverage: snapshot rows written after the Phase-5 migration persist
  `atm_iv`, `term_slope` and the per-force deadzone scales
  (`gex_scale`/`vex_scale`/`chex_scale`), which reactivates the engine's VEX
  leg — TRAP_DOOR / SQUEEZE / GRIND_UP are reachable for those sessions.
  Older rows (NULL slope, zero scales) and the `exposure_history/` fallback
  still replay with a neutral VEX force, so only PIN / MIXED can fire there.

## Spread Backtest

Answers "how much money", where the regime backtest answers "was the Engine
Label right". It replays the Baseline (a 7DTE SPXW put credit spread entered every
session at 10:00 ET, at most five open at once) over the chain store and
writes a JSON report: Sharpe, Sortino, max drawdown, worst week, CVaR 5%, win
rate, average and total P&L, trade count, every trade and every skipped
session. Returns are daily, realized when trades close, with a zero risk-free
rate; drawdown, worst week and CVaR (the
mean of the worst 5% of daily returns) are fractions of equity.

```powershell
pip install -r requirements.txt   # optopsy builds the spreads and fills
python tools\backtest_spread.py --store C:\path\chain.sqlite3 --out report.json
python tools\backtest_spread.py --store C:\path\chain.sqlite3 --short-delta 0.10 --width 50 --fill-ratio 1.0
python tools\backtest_spread.py --store C:\path\chain.sqlite3 --exit-rule managed
python tools\backtest_spread.py --store C:\path\chain.sqlite3 --grid --out grid.json
```

- Exits: `--exit-rule hold` (the default) settles at the SPX close on the
  expiry date. `--exit-rule managed` buys the spread back at the first
  intraday snapshot
  (the store's 30-minute grid) where that costs <= 50% of the credit (take
  profit) or >= 2x the credit (stop), else holds to expiry. Buy-backs fill
  under the same `--fill-ratio` as the entry.
- Grid: `--grid` runs short delta {0.10, 0.16, 0.20} x width {25, 50} x exit
  rule {hold, managed}, and refuses the single-cell flags. `headline` has one row per cell at the 50% fill;
  `fill_sensitivity` repeats every cell at mid, 50% and full spread. The fill
  levels need not rank mid >= 50% >= full. With managed exits, the
  take-profit is a share of the credit, so a richer mid credit can take profit
  on a mark where the 50% run holds on and keeps the whole credit. In any
  variant, a richer credit lowers the risk per spread, so more contracts can
  fit and a losing trade loses more.
- Open-position cap: a spread expiring today is still open at the 10:00
  entry (it settles at 16:00), as in Option Omega and Option Alpha. With
  7DTE entries every session, a held Baseline settles into five entries,
  then one Skipped Session; managed exits free slots early, so hold and
  managed runs can trade different sessions.

- Chain store: `railway-service/src/tripity_experiment/chain_store.py`
  defines the schema (`option_chain_snapshot`, one row per contract per
  snapshot; `daily_close`, VIX and SPX closes). The backfill and the forward
  collector write it; the backtester only reads it.
- Short strike: the put whose delta (canonical greeks engine, snapshot IV) is
  closest to `--short-delta`; long strike = short − `--width`.
- Costs: fills cross `--fill-ratio` of the half-spread from mid (0.5 by
  default); IBKR tiered commission (with the $1.00 order minimum on each leg)
  plus Cboe SPXW customer fees by premium tier, per contract on entry and on a
  managed exit, none at cash settlement.
- Sizing: the most contracts whose total max loss, entry costs included,
  fits `--risk-pct` × equity,
  equity = `--equity` plus P&L of trades closed before the session. With the default
  1%, one 25-wide SPX spread needs roughly $220k of equity.

### GEX Filter

```powershell
python tools\backtest_spread.py --store C:\path\chain.sqlite3 --gex-filter --out gex.json --summary gex.md
python tools\backtest_spread.py --store C:\path\chain.sqlite3 --gex-filter --gex-percentile 0.8 --exit-rule managed
python tools\gex_crosscheck.py --store C:\path\chain.sqlite3
```

- Naive GEX: the dashboard's number, from the canonical greeks engine over
  the whole SPX/SPXW chain (0DTE included) at each session's 10:00 ET entry
  snapshot: SOD Open Interest, the snapshot's spot and IV, calls + and
  puts −. Nothing after 10:00 and no later open interest is read.
- GEX Percentile: the share of the previous 252 sessions whose Naive GEX sits
  below the session's own; none until 20 past sessions exist. History is read
  from up to 380 calendar days before `--start`, so a Holdout run starts
  warm.
- `--gex-filter` runs two filters on one configuration: `positive_gamma`
  (Gamma Regime positive) and `gex_percentile` (percentile above
  `--gex-percentile`, 0.5 by default). Each gets four rows, `kept`,
  `rejected` (the Rejected Sessions, replayed as their own strategy),
  `baseline` and `iv_matched_control`, each with its own Position Cap,
  metrics over the same calendar, trades and Skipped Sessions. A filter's
  Undecided Sessions (no usable entry snapshot, no percentile yet, or no
  VIX close before the session; a snapshot is unusable when rows with an IV
  hold under 90% of its SOD Open Interest) are listed under `undecided` and sit in no row,
  `baseline` included, so `kept` + `rejected` = `baseline`. `gex` lists
  every session's Naive GEX, Gamma Regime and percentile; `covariates` its
  VIX, ATM IV and VRP.
- IV-Matched Control: the `baseline` sessions with the lowest VIX, as many as
  `kept` holds (ties go to the earlier session); `vix_threshold` is the
  highest VIX it includes. VIX is the Cboe close of the session before, so
  nothing after the 10:00 entry is read.
- `comparison`, per filter:
  - `regression`: OLS of trade P&L (the `baseline` trades) on the filter
    flag, with VIX, ATM IV (the entry snapshot's IV at the strike nearest
    spot, on the 7DTE expiry) and VRP (ATM IV minus the annualized realized
    vol of the last 20 SPX daily returns) as covariates; a 95% t interval on
    the flag's coefficient. Trades missing a covariate are dropped and
    counted.
  - `bootstrap`: `kept` minus `iv_matched_control`, 95% intervals on the
    Sharpe difference and the mean-P&L-per-trade difference, from 2000
    paired resamples of 5-day blocks (fixed seed, so reruns agree).
  - `by_year`: each side's trades and mean P&L per trade by entry year.
  - `verdict`: `positive` only when the regression interval and both
    bootstrap intervals sit above zero and `kept` beats the control's mean
    P&L in every year both trade; otherwise `null`.
- The report's first field, and the first line of `--summary` (Markdown
  tables per filter), is the verdict: the filters that beat their
  IV-Matched Control, or `GEX adds nothing beyond VIX as a trade filter`.
  Two filters are tested, so treat a lone marginal positive with suspicion.
- Sign Conventions (#70): `sign_conventions` reruns the filters under each
  of `naive` (the `filters` above), `dealers_short_all` and `reverse_naive`,
  with the same rows and `comparison`. Under `dealers_short_all` GEX is never
  positive, so only `gex_percentile` runs. `sign_convention_check`, also at
  the top of `--summary` with a table of every verdict, says whether any
  filter's verdict changes with the convention. A filter that wins under
  only one convention is a warning, not a finding.
- `gex_crosscheck.py` compares our daily Naive GEX with SqueezeMetrics' free
  daily GEX (the DIX CSV, downloaded unless `--csv` points to a copy) and
  prints the Pearson correlation and the share of sessions with the same
  sign. The scales differ, so only those two numbers mean anything. Run it
  before reading any GEX Filter result.

## Strategy Backtest (SPY/QQQ)

Compares option strategies on the ETFs before one is chosen
(`tools/backtest_options.py`). It reads the same chain store, but only lazily
(one entry snapshot per session, then each chosen contract's own history), so
a full SPY history never has to fit in memory.

```powershell
python tools\backfill_chain.py --store C:\path\etf.sqlite3 --roots SPY,QQQ --start 2018-01-02 --end 2025-09-30
python tools\backtest_options.py --store C:\path\etf.sqlite3 --compare --underlying SPY,QQQ --out compare.json --summary compare.md
python tools\backtest_options.py --store C:\path\etf.sqlite3 --underlying SPY --strategy iron_condor --width 5 --exit-rule managed
```

- Strategies: `put_credit_spread`, `iron_condor`, `short_strangle`,
  `long_strangle`. Short strikes at the delta nearest `--delta`; a wing is the
  strike nearest `--width` dollars beyond its short strike (within $1). One
  position a session at 10:00 ET on the expiry nearest 7 days out, at most
  five open.
- `--compare` runs every strategy over its grid: credit strategies at delta
  {0.10, 0.16, 0.20}, the spreads at width {3, 5}, the long strangle at delta
  {0.25, 0.40}, each with both Exit Rules: 34 configurations per underlying,
  beside buy-and-hold of the underlying. The report states how many
  configurations it tried; the best of many looks good partly by luck.
- Exit Rules: `hold` books every leg at intrinsic against the expiry-day
  close. `managed` closes a credit at <= 50% / >= 2x of the credit, and the
  long strangle at >= 2x / <= 50% of its debit.
- Early Assignment: a short call in the money at the close before an
  ex-dividend date, with time value under the dividend, closes the position
  that evening. Needs the dividends the backfill loads from Yahoo.
- Sizing: contracts so the Risk Unit (worst expiry loss over every strike and
  a 10% gap either way) plus entry costs is at most `--risk-pct` of equity.
  The short strangle is therefore sized for a 10% gap, not unbounded loss.
- Costs: IBKR tiered commissions with the $1 per-leg order minimum, plus a
  rough $0.25 per contract of exchange/ORF/OCC fees for ETF options. Check it
  against a real IBKR statement before trusting low-premium results.
- The Holdout: sessions from 2025-10-01 on are cut from every run, even with
  an `--end` inside them. `--holdout` replays only them; run it once, on the
  one configuration chosen from the Dev Period.

### GEX on SPY/QQQ

```powershell
python tools\backtest_options.py --store C:\path\etf.sqlite3 --gex-study --underlying SPY,QQQ --entry-time 16:00 --summary gex.md
python tools\backtest_options.py --store C:\path\etf.sqlite3 --gex-filter --strategy iron_condor --width 5 --exit-rule managed --entry-time 16:00
python tools\backtest_options.py --store C:\path\etf.sqlite3 --strategy gex_wall_condor --width 5 --entry-time 16:00
```

- GEX at entry: the dashboard's Naive GEX (calls +, puts -, canonical
  gamma from each row's IV, open interest of 10 or more) over every expiry
  with more than an hour left, so a close entry leaves the dead same-day
  expiry out. Skipped when rows with an IV hold under 90% of the open
  interest. Walls: the strikes of largest call and put GEX.
- `gex_wall_condor`: sells the put wall and the call wall on the 7-day
  expiry, each with its wing; skipped when the walls do not bracket spot.
- `--gex-filter` (one configuration) and `--gex-study` (each strategy's
  default: deltas 0.16, long strangle 0.25, width 5, managed exits) run the
  spread backtester's two filters, Gamma Regime positive and GEX Percentile
  above 0.5, each with kept, rejected, Baseline and IV-Matched Control rows
  and the same regression, bootstrap and per-year verdict.
- Covariates: VIX is the last close before the session, ATM IV comes from
  the entry snapshot, VRP is ATM IV less 20-day realized vol of the
  underlying.

- The VRP Filter rides along in `--gex-filter` and `--gex-study`: it keeps
  sessions whose ATM IV beats 20-day realized vol (VRP > 0). Its regression
  controls for VIX and ATM IV only, since controlling for VRP would explain
  the filter away.
- `--compare` adds each row's Deflated Sharpe Ratio (Bailey and Lopez de
  Prado, 2014): the probability that its Sharpe beats the best Sharpe as many
  skill-free tries as the run made would show. Rows under 95% have not
  survived the search.

### Our own chain archive

`.github/workflows/archive-chains.yml` runs `tools/archive_cboe_chain.py`
after the close each weekday and commits one gzipped CSV per symbol per
session (SPY, QQQ, SPX) to the orphan `chain-archive` branch. It runs from
the default branch only, so it starts once this lands on main; every
evening it misses is lost.

```powershell
git fetch origin chain-archive
git worktree add ..\chain-archive origin/chain-archive
python tools\import_eod_chain.py --store C:\path\fresh.sqlite3 --archive ..\chain-archive\chain_archive --symbol SPY --yahoo --vix
python tools\backtest_options.py --store C:\path\fresh.sqlite3 --gex-filter --strategy short_strangle --entry-time 16:00 --holdout --start 2026-10-30
```

- The script writes only on the session's own evening (16:15 ET to
  midnight): earlier quotes are not closing quotes, and the next morning
  Cboe may already show open interest a close entry could not have known.
- About 340 KB a day for the three symbols, roughly 85 MB a year.
- Everything archived lies after `HOLDOUT_START`, so `--holdout --start
  2026-10-30` replays only sessions no test has seen yet.

### End-of-day archive

```powershell
curl -LO https://github.com/lambdaclass/options_backtester/releases/download/data-v1/SPY_options.parquet
curl -LO https://github.com/lambdaclass/options_backtester/releases/download/data-v1/SPY_underlying.parquet
python tools\import_eod_chain.py --store C:\path\etf.sqlite3 --options SPY_options.parquet --underlying SPY_underlying.parquet --verify --vix
```

- The free 2008-2025 archive first published as philippdubach/options-data
  (its own site and repo are gone; lambdaclass mirrors SPY, QQQ and IWM,
  pinned by SHA-256). QQQ starts 2011-03-23; SPY ends 2025-12-12.
- One snapshot per day, stamped 16:00 ET: run the backtester with
  `--entry-time 16:00`. Expiries 0-45 days out and strikes within 25% of the
  close are kept (`--max-dte`, `--strike-band`): about 8.5M SPY rows, 3.5
  minutes, 1.3 GB.
- The archive's IV and greeks are kept (vendor_delta, vendor_gamma); its
  open interest goes in as SOD Open Interest. The archive does not say when
  that open interest is stamped; at worst it is one night early.
- `--vix` also loads the free Cboe VIX closes the GEX Filter's control needs.
- About 130 SPY rows (none in QQQ) hold placeholder asks near 100,000. The
  backtester never enters or exits on a quote above its no-arbitrage bound
  (a put above its strike, a call above the underlying, 5% slack).

## Chain Store Backfill

Fills the chain store from ThetaData through the `thetadata` Python library:
a direct connection, no Theta Terminal, Python 3.12+. It uses only the quote
and open-interest endpoints, which every paid options tier includes, and
solves IV itself, so Options Standard ($80/month, history from 2016-01-01)
is enough and no index subscription is needed. The API key is read from
`THETADATA_API_KEY` and is never written to the store or printed.

Run the probe first, before any bulk download. It prints the earliest SPXW
date (and whether a data request on it actually returns data, since the
listings may not follow the tier), whether SPXW data comes back for
2018-01-02, whether open interest is stamped at the start or the end of the
day, and whether ThetaData supplies an SPX index price before 2022 (only
informational: the backfill derives its own level). If 2018-01-02 returns no
data, upgrade to Pro for the month.

```powershell
pip install -r requirements.txt
$env:THETADATA_API_KEY = "..."
python tools\backfill_chain.py --probe
python tools\backfill_chain.py --store C:\path\chain.sqlite3 --start 2024-03-04 --end 2024-03-08
```

- Pulls SPX and SPXW, 0-10 DTE (`--max-dte`), at the grid times (`--times`,
  default every 30 minutes 09:30-16:00 ET): the NBBO bid and ask as of each
  grid time, plus the day's open interest. One quote request per root per
  session. Loads the free Cboe VIX and SPX daily closes first (`--skip-cboe`
  to skip).
- Open interest is stored as SOD Open Interest, positions at the close of
  T-1: a report stamped on T before the open, or one stamped after the close
  of an earlier day. Anything else is refused, so the field stays empty
  rather than holding look-ahead or T-2 positions; a report without a time
  of day fails the session. A contract missing from a report has zero open
  interest.
- The underlying level is derived from put-call parity on the three strikes
  nearest the money of the nearest expiry, with the canonical greeks engine's
  rate. The summary lists sessions where the level at the last snapshot is
  more than 0.5% from the Cboe SPX close.
- IV is the canonical engine's `implied_vol` of each quote mid against that
  level, so the backtester reprices the stored quotes exactly. Contracts
  with no bid or a crossed quote have no IV; vendor delta and gamma stay
  empty. Solving IV costs roughly 5 s of CPU per full session (about 3 hours
  for 2018-2026).
- Checkpoints per session in `backfill_checkpoint`; rerun the same command to
  resume after an interruption. Failed sessions are listed in the summary and
  retried on the next run (exit code 1 while any failed). Sessions with no
  vendor data (holidays, or dates the tier does not serve) are listed as
  empty and asked again on the next run.
- `--concurrency` caps requests in flight (default 4, the Standard tier's
  limit; 8 on Pro).
- ETF roots: `--roots SPY,QQQ` pulls the ETFs the same way. Each underlying
  gets its own parity level (SPX and SPXW share one). Their daily closes
  (which settle the backtest) and cash dividends (which decide Early
  Assignment) load from Yahoo's chart API first (`--skip-yahoo` to skip).
  Probe an ETF with `--probe --probe-root SPY`. An SPY session holds far more
  strikes than SPX near the money; keep `--max-dte` at 10 or below.

## Data Source Notes


