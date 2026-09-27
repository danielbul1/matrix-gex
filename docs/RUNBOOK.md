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

- `stale`: LSE data is too old during US market hours.
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
- `Update LSE Data` fetches options chains and candles from London Strategic Edge, regenerates `data_status.json`, runs strict smoke validation, and commits only validated data.

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

- `--db PATH`: the Railway snapshot DB. On the Railway service it lives at
  `/data/matrix_flow.sqlite3` (override via env `TRIPITY_MATRIX_FLOW_DB`);
  locally, copy it down or pass `--db`. Table: `matrix_gex_snapshot`
  (1-minute spot + total_gex/dex/vex/chex + flip + walls per session).
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
python tools\backtest_spread.py --store C:\path\chain.sqlite3 --gex-filter --out gex.json
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
  `--gex-percentile`, 0.5 by default). Each gets three rows, `kept`,
  `rejected` (the Rejected Sessions, replayed as their own strategy) and
  `baseline`, each with its own Position Cap, metrics over the same
  calendar, trades and Skipped Sessions. A filter's Undecided Sessions (no
  Naive GEX, or no percentile yet) are listed under `undecided` and sit in
  no row, `baseline` included, so the three rows cover the same sessions. `gex` lists every session's Naive GEX, Gamma Regime and
  percentile.
- `gex_crosscheck.py` compares our daily Naive GEX with SqueezeMetrics' free
  daily GEX (the DIX CSV, downloaded unless `--csv` points to a copy) and
  prints the Pearson correlation and the share of sessions with the same
  sign. The scales differ, so only those two numbers mean anything. Run it
  before reading any GEX Filter result.

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

## Regime Journal

Daily workflow: write your regime call before the open in the dashboard's
**Journal** view (label, key levels, notes). The server freezes the engine's
verdict (label / agreement / reasoning / walls) next to it at save time. You
can replace the call until it is graded; entries lock once graded. After the
close (16:05 ET) the day is auto-graded — on the next `GET` — using the same
outcome rules as the backtester (`tripity_experiment.matrix_outcome`), and
both your label and the engine's frozen label are scored, so the stats answer
"should I trust myself or the machine?".

Storage: table `matrix_journal_entries` in the same flow DB
(`/data/matrix_flow.sqlite3` on Railway, override via
`TRIPITY_MATRIX_FLOW_DB`), one row per `(session_date, symbol)`. It is NOT
covered by the snapshot retention deletes.

Endpoints (same host as the other `/api/matrix/*` routes):

- `POST /api/matrix/journal` `{symbol, user_label, user_levels, notes}` —
  save/replace today's call; 409 once the entry is graded.
- `GET /api/matrix/journal?days=60` — recent entries with grades; past
  ungraded days are auto-graded on the way out.
- `POST /api/matrix/journal/grade` `{date, symbol}` — grade explicitly (400
  before the session completes, idempotent afterwards).
- `GET /api/matrix/journal/stats?days=60` — hit rates: you overall, engine
  overall, you-when-agreeing vs you-when-disagreeing, per-label breakdown,
  with day counts and a small-sample caution (<30 scored days = noise).

Grading needs the day's prices: the candles feed first, then the snapshot
DB's own spot series (same fallback as the backtester). Days without price
coverage stay ungraded until data exists.

## Data Source Notes


