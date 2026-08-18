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


