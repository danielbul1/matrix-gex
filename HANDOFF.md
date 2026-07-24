# Matrix GEX — Agent Handoff

> Read this first if you're an agent (or human) picking up this project on a new machine.
> Everything needed to understand, run, and deploy the project is in this repo.

## What this project is

A market-analytics stack for 5 US symbols — **NDX, SPX, SPY, QQQ, VIX** — showing live prices,
options gamma/delta/vanna/charm exposure (GEX/DEX/VEX/CHEX), options flow, and dealer positioning.
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

## Data source: London Strategic Edge (LSE) — FREE plan limits

- **10 historical downloads/hour** (options snapshots + candles each count).
- **16 simultaneous WebSocket stream symbols**; we use 4 base underlyings
  (SPY, QQQ, SPX500/USD, NAS100/USD) plus at most 1 active on-demand stock.
- API key lives ONLY in env vars (`LSE_API_KEY` / `TRIPITY_MATRIX_LSE_API_KEY`). Never commit it.
- The Actions workflow respects the budget by design: options every 30 min, candles every 3 h,
  never more than 8 downloads in any 60-minute window.

## Live architecture (Railway)

- Service `backend` in Railway project **Tripity**, URL https://api.trytripity.site
- **WebSocket collector** (in-process asyncio task) streams SPY/QQQ prints + all 4 underlyings;
  latest spots in `_matrix_flow_collector.latest_spot`.
- **GEX snapshots**: every 1 minute during 9:30–16:00 ET, per symbol — spot, total GEX/DEX,
  zero-gamma flip, call/put walls, regime — stored in SQLite on the `/data` volume
  (`matrix_gex_snapshot` table). Retention: today + 3 prior sessions.
- Endpoints:
  - `GET /api/matrix/candles|cboe-data|flow` (original)
  - `GET /api/matrix/symbol-search?q=MSFT` — searches the daily-cached official
    Cboe option-symbol directory by ticker or company name.
  - `GET /api/matrix/symbol-data?symbol=MSFT` — daily-cached on-demand Cboe
    OI/IV chain; subscribes the active stock to the existing LSE WebSocket.
  - `GET /api/matrix/active-symbol?symbol=SPY` — clears the one dynamic stream
    subscription when the UI returns to a core symbol.
  - `GET /api/matrix/spots` — live spot per symbol (websocket or 50s-cached REST;
  VIX has no LSE stream, so it comes from the cached CBOE delayed quote)
  - `GET /api/matrix/gex-history?symbol=SPY&session_offset=1..3` — 1-min GEX history,
    3 prior completed sessions (excludes today).
- Frontend polls `/api/matrix/spots` every 1s and recomputes the in-browser GEX engine
  (matrix.js) so levels move live; replay UI mirrors Net Drift & Flow.

## How to deploy (any machine)

```bash
npm i -g @railway/cli
railway login                     # browser auth, once per machine
cd railway-service
railway link --project Tripity --environment production --service backend
railway up                        # build + deploy; verify afterwards:
curl https://api.trytripity.site/api/matrix/spots
```

Gotchas learned the hard way (2026-07-19):
- Run `railway up` from `railway-service/` itself; uploading the parent dir breaks Railpack detection.
- `railway.toml` startCommand must be `pip install . && python -m tripity_experiment.public_company_host`
  (runtime container ≠ build container; console scripts aren't on PATH).

## Safety switches (env vars, no deploy needed)

- `TRIPITY_MATRIX_STREAM_INDICES=0` → websocket back to SPY/QQQ only
- `TRIPITY_MATRIX_GEX_SNAPSHOTS=0` → disable the snapshotter task
- `TRIPITY_MATRIX_FLOW_COLLECTOR=0` → disable flow collector

## Health check routine

1. `curl https://api.trytripity.site/api/matrix/spots` → `ok:true`, fresh `asof`
2. `curl "https://api.trytripity.site/api/matrix/gex-history?symbol=SPY&session_offset=1"` → `ok:true`
3. `railway logs -n 100` → no snapshot/websocket errors

## Rules for agents working on this repo

1. Never exceed LSE free-plan budget (see above) — count before adding any fetch.
2. Never commit secrets; `.env` is gitignored, keys come from env only.
3. Plan first, get owner approval, then implement. Owner speaks Hebrew.
4. After any Railway deploy, run the health check routine above and report.
5. **MANDATORY: before every commit, update this HANDOFF.md** —
   a. If architecture/endpoints/env vars/deploy steps changed → update the relevant section.
   b. Always append an entry to the Session Log below (newest on top).

## Session Log (every agent MUST append — newest first)

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
