# Matrix GEX — Agent Handoff

> Read this first if you're an agent (or human) picking up this project on a new machine.
> Everything needed to understand, run, and deploy the project is in this repo.

## What this project is

A market-analytics stack for 5 US symbols — **NDX, SPX, SPY, QQQ, VIX**. The dashboard copies
Quant Power's screens and nothing else (ADR `docs/adr/0001-matrix-mirrors-quant-power-screens.md`,
map issue #21); data reloads only on Refresh, never on a timer.
Owner + his father are the only users. Live dashboard: https://api.trytripity.site/matrix/

## Repository layout

```
/                          → data pipeline + docs (this repo: danielbul1/matrix-gex)
  index.html               → GitHub Pages redirect to https://api.trytripity.site/matrix/
  fetch_lse.py             → LSE options/candles fetcher (--options-only / --candles-only)
  tools/                   → smoke_check.py, build_data_status.py (data validation)
  .github/workflows/       → hourly data refresh (Actions cron, see below)
  railway-service/         → THE LIVE BACKEND (Railway deployment source of truth)
    src/tripity_experiment/public_company_host.py  → the whole server (ASGI app)
    src/tripity_experiment/web/matrix.js|.html|.css → the dashboard frontend
    pyproject.toml         → package def; start via `python -m tripity_experiment.public_company_host`
    railway.toml           → startCommand for Railway (DO NOT DELETE)
```

## Data sources

- **Cboe delayed snapshot** (`cdn-api.cboe.com`, free, no key): the option chains with
  open interest, IV and greeks. GEX needs the OI, so chains always come from here.
- **London Strategic Edge Data 3.0** (`https://download.londonstrategicedge.com/v3`,
  `X-API-Key` header): REST prices (`/v3/preview`), candles (`/v3/export`, CSV)
  and the chain's live volume (`/v3/options/chain`).
  The old `/vault/*` and `x_*` calls answer 410/404 since October 2026.
  - FREE plan: **60 calls/minute**, **one export download at a time**, 12.5 GiB/week of
    exports; `GET /v3/usage` shows the key's limits.
  - v3 chains carry **no open interest**, and their greeks are computed twice a day (a
    session old), so the host keeps Cboe's greeks and only overlays volume/last price.
  - Intraday index candles (SPX/NDX) have gaps on LSE; the host falls back to Yahoo when
    the newest LSE candle is over 4 days old.
- API keys live ONLY in env vars (`LSE_API_KEY` / `TRIPITY_MATRIX_LSE_API_KEY`). Never commit them.

## Live architecture (Railway)

- Service `backend` in Railway project **Tripity**, URL https://api.trytripity.site
- No background tasks: data is fetched when the browser asks. The old flow collector,
  per-minute GEX snapshots and their SQLite store (`/data/matrix_flow.sqlite3`) were removed
  on 2026-10-26; startup deletes that file if it is still there.
- Endpoints:
  - `GET /api/matrix/cboe-data` — core chains (Cboe OI + LSE volume, LSE REST spot).
  - `GET /api/matrix/candles?symbol=&interval=&range=` — intraday candles.
  - `GET /api/matrix/spx-quote` — Cboe delayed SPX quote.
  - `GET /api/matrix/symbol-data?symbol=MSFT` — daily-cached on-demand Cboe
    OI/IV chain (Cboe delayed spot).

## How to deploy (any machine)

Since 2026-10-26 the `backend` service builds from GitHub (`danielbul1/matrix-gex`, root
`/railway-service`, config `/railway-service/railway.toml`), **pinned to one commit**: pushes
to `main` do not deploy. To deploy a new commit, re-pin the service source to it in Railway
(Settings → Source) or run `railway up` as below.

```bash
npm i -g @railway/cli
railway login                     # browser auth, once per machine
cd railway-service
railway link --project Tripity --environment production --service backend
railway up                        # build + deploy; verify afterwards:
curl -s https://api.trytripity.site/api/matrix/cboe-data | head -c 200
```

Gotchas learned the hard way (2026-07-19):
- Run `railway up` from `railway-service/` itself; uploading the parent dir breaks Railpack detection.
- `railway.toml` startCommand must be `pip install . && python -m tripity_experiment.public_company_host`
  (runtime container ≠ build container; console scripts aren't on PATH).

## Health check routine

1. `curl https://api.trytripity.site/api/matrix/cboe-data` → JSON with `SPX`, `NDX`, `SPY`, `QQQ`
2. Open https://api.trytripity.site/matrix/ → the GEX chart draws
3. `railway logs -n 100` → no errors

## Rules for agents working on this repo

1. Never exceed LSE free-plan budget (see above) — count before adding any fetch.
2. Never commit secrets; `.env` is gitignored, keys come from env only.
3. Plan first, get owner approval, then implement. Owner speaks Hebrew.
4. After any Railway deploy, run the health check routine above and report.
5. **MANDATORY: before every commit, update this HANDOFF.md** —
   a. If architecture/endpoints/env vars/deploy steps changed → update the relevant section.
   b. Always append an entry to the Session Log below (newest on top).

## Session Log (every agent MUST append — newest first)

### 2026-10-26 — Tastytrade feed probe (not wired into the host)
- **Why:** researched George (georgefi.trade), Quant Data and open-source GEX/flow
  projects. What we lack is a real-time option trade tape; Tastytrade's API (free with
  a funded account) streams it over DXLink. Its TimeAndSale event is not officially
  supported, so `tools/tasty_probe.py` checks it before anything is built on it.
- **Probe:** streams SPX 0DTE (±30 strikes) for N minutes and reports: quote lag
  (real-time or delayed), tape coverage vs Trade day volume, bid/ask present on
  prints, feed aggressor side vs side from quote, sweep-like clusters, Greeks cadence,
  open interest. Needs `TASTY_CLIENT_SECRET` + `TASTY_REFRESH_TOKEN` (read scope);
  `pip install "tastytrade>=13,<14"` (not in requirements.txt yet).
- **Terms to respect:** personal use only, no display to third parties, no bulk
  "data collection" (ToS §8) — stream near-the-money strikes, not whole chains.
- **Railway:** the three `TASTY_*` vars sit on `backend` (values redacted to agents).
  A separate one-shot service `tasty-probe` (restart NEVER) builds this branch and runs
  the probe with references to them; read its result with `railway logs`. `backend` is
  untouched. Delete `tasty-probe` once the feed decision is made.
- `--check` tests login, chain and stream at any hour; the full run needs market hours.
- **First full run (SPX 0DTE, ±30 strikes, 15 min, 2026-10-26 11:01 ET):** 77,767
  TimeAndSale prints on 111/122 contracts, 99.9% with bid/ask, 100% valid, aggressor side
  always BUY/SELL (never undefined), 35% spread legs; every print on exchange C (SPXW is
  Cboe-only, so no multi-exchange sweeps there). Greeks update about every 79 s (compute
  our own for live GEX). OI on all contracts. That run's lag and coverage numbers were
  wrong (Quote times are empty; coverage skipped spread legs): fixed, rerun with `--symbol`.

### 2026-10-26 — Shell, menu and a path per screen (map #21, task #32)
- **Frontend:** sidebar in Quant Power's order (the "Matrix" brand link is home = Strike
  Chart, then OI Volume, GEX Analysis, Key Levels, P/C Ratio, How to use GEX, and
  Disclaimer as its own link). Each screen has its own path under `/matrix/`, and the
  ticker screens carry `?ticker=` so a link reopens the same state. Navigation uses
  `history.pushState`, so Back/Forward work. Each ticker screen has its own ticker field
  and Search button, and Enter is Search; on the Strike Chart that reloads the data
  (core tickers from `cboe-data`, any other through `symbol-data`). Screens not built yet
  show "Coming soon". On phones the menu folds behind a button.
- **Removed:** the Live Data Health panel (and its 1 s UI timer), the stale-data banner,
  favorites and the star, the search autocomplete list, the header badge and footer.
- **Backend:** every screen path (with or without a trailing slash) serves `matrix.html`;
  `/matrix?ticker=X` redirects to `/matrix/?ticker=X`. Removed
  `/api/matrix/symbol-search` and its Cboe directory cache (only the deleted autocomplete
  used it). CSS/JS are now linked by absolute `/matrix/...` paths.
- **Tests:** route, menu-order and old-shell tests in `tests/test_matrix_routes.py`.
  Checked in headless Chromium (desktop and 390 px) with recorded data.

### 2026-10-26 — Delete everything Quant Power does not have (map #21, task #31)
- **Frontend:** removed the DEX, VEX, CHEX, Market Structure, Exposure Lab, Net Drift &
  Flow, Matrix GEX, Shock Engine, Max Pain, Review and Journal screens; the AVG Power
  Zone, Weighted, sigma-range and DEX/VEX/CHEX chart controls; auto-refresh, the 1 s spot
  poller, GEX Replay, the regime banner; and the Market, Mode, Data Source and Spot
  Override controls with the synthetic demo chain. The GEX chart keeps the old defaults
  (whole chain, spot ± 2 expected moves). A failed Refresh no longer retries on a timer.
- **Backend:** removed `/api/matrix/flow|spots|gex-history|regime|active-symbol|journal*`,
  the LSE WebSocket collector, the GEX snapshotter and the SQLite store; startup deletes
  `matrix_flow.sqlite3`. The research modules (`matrix_gex`, `matrix_regime`,
  `matrix_outcome`) stay for the tools/backtests. Env vars `TRIPITY_MATRIX_FLOW_COLLECTOR`,
  `TRIPITY_MATRIX_GEX_SNAPSHOTS`, `TRIPITY_MATRIX_STREAM_INDICES` no longer do anything.
- **Tests:** dropped the journal and GEX-history tests and the host regime test; added
  `tests/test_matrix_routes.py`. Checked the page in headless Chromium with stub data.

### 2026-10-26 — LSE Data 3.0 migration
- **Outage:** LSE retired its old REST API. `x_options_snapshot`/`x_candles_15m` answer
  401/404 and `/vault/*` 410, so the Actions pipeline had committed nothing since
  2026-07-16 and the host's LSE chain overlay, candles, flow and REST spots were failing
  (the dashboard fell back to Cboe/Yahoo). The `LSE_API_KEY` repo secret was re-added.
- **Host:** chain → `/v3/options/chain` (overlay keeps Cboe greeks, takes `volume_today`
  and last price), flow → `/v3/options/flow` (`from` instead of `start`; no call before
  the open, which v3 refuses), spots → newest 1m candle from `/v3/preview`, candles →
  `/v3/export` CSV behind a lock (one download at a time) with a 4-day staleness guard
  that falls back to Yahoo. `MATRIX_LSE_PRICE_SYMBOLS` now maps to (market, symbol).
- **Pipeline:** `fetch_lse.py` takes options from Cboe (via `fetch_cboe.fetch_chain`,
  extracted from `fetch_cboe.main`) and candles from `/v3/export`; `data_status.json`
  source is now "Cboe delayed options snapshot" (smoke check accepts the old label too).
- **Not done:** `server.py` (local Flask) still calls `x_options_snapshot`; the WebSocket
  index names (SPX500/USD, NAS100/USD) still need checking during market hours; LSE v3
  also serves VIX live, which could replace the Cboe delayed VIX quote later.
- Verified against the live API: spots, candles, chain overlay and flow; full pipeline
  run + strict smoke check pass; 193 pytest green.

### 2026-10-26 — Cboe CDN host move fixed; optopsy pinned below 2.3
- **Outage:** Cboe moved its delayed-quotes CDN from `cdn.cboe.com` to
  `cdn-api.cboe.com` (old host answers 307). The host's httpx clients did not
  follow redirects, so `/api/matrix/cboe-data` returned `ok:false`, GEX
  snapshots stored sessions with zero points, and the VIX spot froze at
  2026-09-18. Fix: SPX/VIX quote and options-chain URLs point at
  `cdn-api.cboe.com`, and both Cboe clients now follow redirects. Same host
  swap in `fetch_cboe.py` and `tools/backfill_chain.py` (both already
  followed redirects; changed for consistency). The symbol directory moved
  too, but its client already followed redirects, so it was left alone.
  Verified live: all 5 chains (NDX/SPX/SPY/QQQ/VIX) load.
- **Deps:** optopsy 2.3.0 expects a `delta` column our chains lack (36
  backtest_spread tests fail); `requirements.txt` now pins `>=2.2,<2.3`.
  190 pytest green with 2.2.0.
- **Still open (owner action):** the `LSE_API_KEY` repo secret is missing, so
  `update-cboe.yml` has skipped every commit to the `data` branch since
  2026-07-16 while showing green. Needs the secret re-added in GitHub
  Settings → Secrets → Actions.
- **Not deployed yet:** the Cboe fix needs `railway up` from
  `railway-service/`, then the health check routine above.

### 2026-08-18 — Canonical greeks engine, regime engine, VEX/CHEX history, backtester, journal
- External review + 5-phase build (no prior commit; all verified locally, 92 pytest green).
- **Fixes:** smoke-check.yml compiled a nonexistent root `gamma_exposure.py` (CI red
  since 2026-07-19) → now `tools/gamma_exposure.py`, dead path filters pruned;
  server.py put-charm double negation (CHEX put side was wrong-signed); powerZone
  DEX/GEX unit mix ($M vs $B saturated the DEX term); spot=0 first-row poisoning;
  `%b %d` expiry key merged years; `norm_iv` threshold 1→3 in server.py AND
  fetch_lse.py; build_exposure_history vendor-gamma=0 now falls back to BS gamma,
  day keyed by ET session, interval 5→30; `/raw` gets a 60s cache; wrong put-gamma
  formula in tools/gamma_exposure.py; deploy.ps1 got the DEPRECATED header;
  README cron text + .env.example staleness fixed; .gitignore extended (data
  snapshots, exposure_history/, cboe_open_data.json).
- **Engine:** new `railway-service/src/tripity_experiment/matrix_gex.py` (pure
  stdlib) is the single source for BS greeks, GEX/DEX/VEX/CHEX, smooth zero-gamma
  flip, walls, expected move. Consumers rewired: public_company_host.py,
  server.py, tools/build_exposure_history.py. matrix.js keeps its in-browser
  engine (fallback spot constants refreshed). Host `flip` now uses the smooth
  profile method (was cumulative-sum) — intentional behavior change.
- **History:** `matrix_gex_snapshot` gains `total_vex`, `total_chex`, `atm_iv`,
  `term_slope`, `gex_scale`, `vex_scale`, `chex_scale` (PRAGMA+ALTER migration,
  legacy rows read as 0/NULL). `/api/matrix/gex-history` additive only.
- **Regime engine:** new `matrix_regime.py` + `GET /api/matrix/regime?symbol=`
  fuses GEX force / VEX wind / CEX wind / IV term structure / VRP (Garman-Klass
  from candles) into labeled verdicts (GRIND_UP, TRAP_DOOR, SQUEEZE, PIN, MIXED)
  with per-force reasoning; banner in the dashboard. Core symbols only.
- **Backtester:** `tools/backtest_regime.py` replays SQLite or exposure_history/
  through the regime engine and scores labels vs realized outcomes; shared rules
  live in `matrix_outcome.py`. RUNBOOK documents usage.
- **Journal:** `matrix_journal_entries` table + `POST/GET /api/matrix/journal`,
  `/grade`, `/stats` — pre-open user call frozen beside the engine verdict,
  auto-graded after close via matrix_outcome; stats split user-vs-engine and
  agree/disagree. New Journal nav view in the dashboard.
- **Known issues:** tools/smoke_matrix_frontend.js fails on HEAD (pre-existing
  DOM stub gap, matrix.js addEventListener) — untouched; old DB rows show zero
  VEX/CHEX until new snapshots accumulate; VIX regime has no VRP (no candles);
  no holiday calendar.

### 2026-07-24 — Pages frontend retired, unified on Railway copy
- Deleted the legacy GitHub Pages dashboard: `gex_dashboard.html`,
  `assets/dashboard.js`, `assets/dashboard.css` (assets/ dir removed — it held
  only those two files). Owner confirmed the Pages-only views (Edge, Dark Pool
  Levels, Options Heat Map) are not used; no porting needed.
- Root `index.html` is now a minimal redirect to
  https://api.trytripity.site/matrix/ (meta refresh + canonical + Hebrew
  fallback link). Single source of truth: `railway-service/src/tripity_experiment/web/matrix.{js,html,css}`.
- `tools/smoke_check.py`: removed all dashboard checks (REQUIRED_IDS DOM-ID
  list, script/css tag assertions, function-presence and mojibake checks on
  gex_dashboard.html, now-unused `re` import). JSON data-shape checks,
  data_status checks, SPX asof freshness, and strict-mode env behavior kept —
  `update-cboe.yml` still runs it in strict mode.
- `deploy-pages.yml`: added `--exclude 'railway-service'` to the rsync so the
  railway copy is no longer silently published to Pages as unlinked files.
- README corrected: live dashboard URL stated up front; `fetch_cboe.py` is NOT
  deprecated (VIX support 2026-07-22, referenced by smoke-check.yml) — only
  `run_cboe_collector.ps1`, `fetch_candles.py`, `auto_deploy.ps1` are; stale
  "Railway service code is not yet in this repo" section removed (resolved
  2026-07-19); pipeline A description no longer names the Pages dashboard as
  the data consumer.
- Validated: `python tools\smoke_check.py` passes; grep for
  `dashboard.js` / `gex_dashboard` / `matrix_remote` hits only historical
  mentions. No commit, no deploy, nothing under railway-service/ touched.

### 2026-07-24 — Frontend copies mapped; orphaned matrix_remote.js deleted
- Mapped all three frontend copies: railway `web/matrix.{js,html,css}` = production
  (strict superset: spots poller, replay, VIX, VEX/CHEX, search, dynamic symbols);
  `assets/dashboard.js` + `gex_dashboard.html` = live GitHub Pages copy (legacy views:
  Edge, Dark Pool Levels, Options Heat Map — exist nowhere else);
  `matrix_remote.js` = orphaned pre-poller snapshot with zero consumers.
- Deleted `matrix_remote.js` (untracked; content preserved in git history via the
  first railway-service commit) and removed its exclusion line from deploy-pages.yml.
- Unification plan (pending owner decision on Pages-only views): single source of
  truth = railway copy, then redirect Pages to https://api.trytripity.site/matrix/.
- Open doc bug: `fetch_cboe.py` has NO DEPRECATED header though README claims it
  does; it is still maintained (VIX added 2026-07-22) and referenced by
  smoke-check.yml. README or header needs fixing.

### 2026-07-24 — Live spot poller cadence 5s → 1s
- `matrix.js` `setupSpotsPoller()` now polls `/api/matrix/spots` every 1s instead of
  5s. Client-side only: no change to Cboe/LSE/Yahoo cadences, snapshot capture stays
  1/min, and upstream rate budgets are untouched (the endpoint serves our own backend).
- Deployed to Railway production (`railway up`); health checks passed: `/api/matrix/spots`
  fresh websocket spots, `/api/matrix/gex-history?symbol=SPY&session_offset=1` OK, and
  the served `matrix.js` contains `setInterval(pollLiveSpots, 1000)`.

### 2026-07-23 — Dynamic Cboe symbol search, favorites, and one live LSE stock
- Added a TradingView-style symbol search backed by Cboe's official option-symbol
  directory. Searches match both ticker and company name (`MSFT`/`Microsoft`,
  `AVGO`/`Broadcom`); the directory is cached for 24 hours.
- Added browser-local starred favorites. Favorites do not create API calls or
  WebSocket subscriptions.
- Added `/api/matrix/symbol-data` for on-demand option chains. The Cboe OI/IV
  snapshot is cached for the full ET calendar day, is isolated from the core
  market-data map, and is never written to GEX history or Net Drift/Flow tables.
  New uncached symbols are globally capped at 12 Cboe fetches/minute.
- Dynamic symbols calculate current GEX/DEX/VEX/CHEX/Max Pain and related views
  in the browser. Gamma/Delta/Vanna/Charm are recalculated locally as spot moves.
- The existing LSE collector now supports one dynamic underlying subscription:
  selecting a stock subscribes it; selecting another unsubscribes the previous;
  returning to NDX/SPX/SPY/QQQ/VIX clears it. SPY/QQQ option subscriptions and
  their persistent Net Drift/Flow history remain unchanged.
- Dynamic symbols hide GEX Replay and redirect Net Drift/Flow to SPY. No Yahoo
  feed is used for dynamic symbols; their live spot comes from the LSE WebSocket.
- Validation: Python compile, Node syntax, repository smoke check, live Cboe
  search/chain checks, headless Chrome search/favorite/flow-guard checks, and
  fake-client subscribe/unsubscribe lifecycle all passed.
- Production browser testing exposed and fixed an initial-load race: a dynamic
  symbol now waits for the core payload to finish and becomes selected only
  after its chain/config is ready. Search-result status updates are invalidated
  after selection so they cannot overwrite the loaded-chain status.
- Deployed to Railway production on `backend` (final deployment
  `4bf471e4-67af-4250-bd5b-2775cc54ff3c`). Production verification:
  MSFT search + 1,500-contract chain + LSE WebSocket spot passed; switching
  back to SPY removed MSFT from `/spots`; health, SPY live spot, and 391-point
  prior-session GEX history passed; headless Chrome reported no page errors or
  failed API responses.

### 2026-07-22 — Expiration quick-select, VIX as 5th symbol, Weighted overlay, VEX/CHEX views
- **Expiration quick-select** (matrix.html/css/js): 0DTE / Week / All buttons above
  `#expirationPicker` (styled like the Range buttons). They set picker checkboxes
  (checkboxes now carry `data-dte`) and run the exact manual-change flow
  (`saveCurrentExpirationSelection` + `run()` + flow reload); active state syncs with selection.
- **VIX** (matrix.js, public_company_host.py, fetch_cboe.py):
  - Frontend `SYMBOLS` gained `VIX:{spot:20, step:1, mult:100, baseIV:0.80, market:"US"}`.
  - `preciseYearsToExpiry` (JS) and `_matrix_years_to_expiry` (PY mirror) treat roots
    VIX + VIXW as AM-settled (9:30 ET), like SPX/NDX.
  - Backend `MATRIX_CBOE_SYMBOLS` gained `("_VIX", "VIX")`; verified live that
    `options/_VIX.json` matches the existing parser (OCC roots VIX/VIXW, OI/iv/gamma/delta present).
  - `/api/matrix/spots` now includes VIX via the cached CBOE delayed quote
    (`MATRIX_VIX_QUOTE_URL`, `_fetch_matrix_cboe_quote` generalized from the SPX quote helper,
    50s `_matrix_cboe_spot_cache`). LSE symbols untouched.
  - Root `fetch_cboe.py` SYMBOLS gained `("_VIX", "VIX")`.
  - Net Drift & Flow stays SPY/QQQ-only (existing auto-switch/message covers VIX).
- **Weighted button** (matrix.html/js): new pink `Weighted` pbtn in the GEX params row.
  It's an independent overlay toggle (pbtn metric buttons are multi-select, not exclusive).
  When active it draws 3 dashed marker lines on the Net GEX chart — Call/Put/Total
  OI-weighted strikes (Σ K·OI / Σ OI over the selected expirations) — with values in the legend.
- **VEX + CHEX views** (matrix.html/css/js): sidebar reordered to DEX, GEX, VEX, CHEX, then
  the rest (gex stays default). New views clone the DEX pattern via `BAR_METRIC_BY_CHART`
  (`net_vex`/`net_charm` bar metrics; `chartTargets` became a lookup map): signed bars per
  strike, 1σ/2σ/3σ/All range, tooltips with Call/Put breakdown + units note
  (VEX = $ per 1% vol move, CHEX = $ per day), KPI cards, per-view expiration memory
  (`selectedExpirationsByView` + both reset blocks). Axis titles show "Net VEX ($ per 1% vol
  move)" / "Net CHEX ($ per day)".
- Validation: `node --check` OK; `py_compile` OK; new `tools/smoke_matrix_frontend.js`
  (DOM-stubbed Node harness, real CBOE `_VIX.json`) — all checks PASS; backend VIX parsing +
  AM-settled mirror verified via AST extraction (fastmcp not installed locally).
- **Owner corrections (same day, review of the above):**
  - Weighted overlay now shows ONLY the Total Weighted Strike line (Call W / Put W lines
    and legend entries removed; `WEIGHTED_LINES` reduced to the total entry).
  - New DEX | GEX | VEX | CHEX segmented switcher inside the GEX chart card (bottom-right,
    Range-button styling, GEX default). It swaps the main gexChart's primary bars via
    `GEX_CHART_BAR_METRIC` + `chartBarMetricKey()`/`chartActiveMetrics()` (gexChart entry is
    dynamic; sidebar DEX/VEX/CHEX pages unchanged). Axis title/legend/tooltips follow the
    active metric; param overlays (AG/OI/Vol/Power/AVG Power/Weighted) work on any metric;
    the Net GEX pbtn still hides bars when GEX is the selected metric. State is module-level,
    so it survives symbol/expiration changes and the 5s spot poller.
- NOT deployed to Railway, no commit, no keys touched. Browser check of the new views pending.

### 2026-07-19 — Live GEX + history + repo unification (machine: owner's main PC)
- Added `/api/matrix/spots` (live spots, websocket-backed for all 4 symbols) and
  `/api/matrix/gex-history` (1-min GEX/DEX snapshots, 3 prior sessions).
- Backend: `matrix_gex_snapshot` SQLite table + minute capture task; websocket extended
  to SPX500/USD + NAS100/USD. Safety switches: TRIPITY_MATRIX_STREAM_INDICES, TRIPITY_MATRIX_GEX_SNAPSHOTS.
- Frontend: 5s live spot poller + GEX replay bar (day select + minute slider).
- railway-service/ committed to this repo for the first time; railway.toml created
  (startCommand: `pip install . && python -m tripity_experiment.public_company_host`).
- Actions workflow: options every 30 min / candles every 3 h; removed retry loop
  that could burn ~56 downloads/run; fetch_lse.py gained --options-only/--candles-only.
- Local server.py hardened (60s locked cache, stale fallback, ET timezones, 0DTE fix).
- Verified live 2026-07-19 22:53 UTC+3: dashboard 200, spots + gex-history OK.
