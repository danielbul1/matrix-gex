# Matrix GEX — Agent Handoff

> Read this first if you're an agent (or human) picking up this project on a new machine.
> Everything needed to understand, run, and deploy the project is in this repo.

## What this project is

A market-analytics stack for 4 US symbols — **NDX, SPX, SPY, QQQ** — showing live prices,
options gamma/delta exposure (GEX/DEX), options flow, and dealer positioning.
Owner + his father are the only users. Live dashboard: https://api.trytripity.site/matrix/

## Repository layout

```
/                          → data pipeline + docs (this repo: danielbul1/matrix-gex)
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
- **16 simultaneous WebSocket stream symbols**; we use 4 (SPY, QQQ, SPX500/USD, NAS100/USD).
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
  - `GET /api/matrix/spots` — live spot per symbol (websocket or 50s-cached REST)
  - `GET /api/matrix/gex-history?symbol=SPY&session_offset=1..3` — 1-min GEX history,
    3 prior completed sessions (excludes today).
- Frontend polls `/api/matrix/spots` every 5s and recomputes the in-browser GEX engine
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
