# Matrix

Options GEX dashboard for NDX, SPX, SPY, and QQQ.

The live dashboard is at https://api.trytripity.site/matrix/ (served by the Railway backend). GitHub Pages now only redirects there.

Options chains (open interest, IV, greeks) come from **Cboe's free delayed snapshot**; prices, candles,
options flow and the live WebSocket come from **London Strategic Edge (LSE) Data 3.0**. The published data
files are refreshed by `.github/workflows/update-cboe.yml`.
Operational status is published in `data_status.json` for lightweight external monitoring.
When using the Flask server, the same status is available at `/status`; it returns HTTP 200 for `fresh`/`off_hours` and HTTP 503 for `stale`, `future`, `unknown`, or missing status data.

See `docs/RUNBOOK.md` for operational checks, manual refresh steps, and pre-commit validation.

## Architecture

Three pipelines exist in/around this repo:

- **A. GitHub Actions data pipeline (primary).** `.github/workflows/update-cboe.yml`
  runs `fetch_lse.py` hourly during US market hours (13-20 UTC, Mon-Fri),
  validates with `tools/build_data_status.py` + strict `tools/smoke_check.py`,
  and commits `cboe_data.json`, `candles_data.json`, `data_status.json`, and
  `exposure_history/` to the orphan `data` branch. Only validated data is
  published. The Railway backend/API is the live surface that serves the
  dashboard; the published data branch remains the validated data source.
- **B. Local Windows collector (DEPRECATED).** `run_cboe_collector.ps1`,
  `fetch_candles.py`, and `auto_deploy.ps1` are superseded by
  pipeline A and the Railway service. They are kept for reference only; each
  file carries a DEPRECATED header. Do not run them in production.
  (`fetch_cboe.py` is NOT deprecated — it is still maintained, gained VIX
  support on 2026-07-22, and is referenced by `smoke-check.yml`.)
- **C. Local Flask server (`server.py`).** Optional single-symbol (SPY) live
  GEX backend with a 60-second cache, stale-while-revalidate fallback, and
  `/status` monitoring endpoint. Intended for local/personal use.

## LSE plan limits

LSE moved to its **Data 3.0** API (`https://download.londonstrategicedge.com/v3`,
key in the `X-API-Key` header) in October 2026; the old `/vault/*` and `x_*`
calls answer 410/404. The free plan allows **60 calls a minute**, **one export
download at a time** and a 12.5 GiB weekly export allowance (`GET /v3/usage`
shows them). Its option chain carries no open interest and its Greeks are
computed twice a day, which is why the chains and OI stay on Cboe.

- Pipeline A runs **every 30 minutes** (cron `13,43 13-20 * * 1-5`): options from
  Cboe (no key), candles from LSE every 3 hours.
- The Railway host serializes its LSE candle exports (one download at a time)
  and falls back to Yahoo when LSE has no fresh candles, which happens for the
  intraday index history.
- `server.py` still calls the retired `x_options_snapshot` endpoint and needs
  moving to Cboe before it works again.

### On-demand US option symbols

The Railway Matrix UI can search the official Cboe option-symbol directory and
load symbols such as `MSFT` or `AVGO` on demand. These symbols use a delayed
Cboe option-chain snapshot and do not consume LSE historical-download quota.
Search results are cached for 24 hours and each option chain for the current
ET calendar day; new uncached chains are also capped at 12 upstream fetches
per minute. Intraday spot updates use one dynamic LSE WebSocket subscription
for the currently selected stock, and the browser recalculates the Greeks
locally against the daily Cboe OI/IV snapshot. Switching to another dynamic
stock unsubscribes the previous one; favorites do not consume stream slots.

Favorites are stored only in the browser. On-demand symbols are intentionally
excluded from Net Drift/Flow collection and GEX history snapshots; current
GEX, DEX, VEX, CHEX, Max Pain, and the other snapshot-derived views are still
calculated in the browser.

## API key

Set your LSE API key as an environment variable:

```powershell
$env:LSE_API_KEY = "lse_live_..."
```

For GitHub Actions, add the key as a repository secret named `LSE_API_KEY`.

## Local validation

Install dependencies once:

```powershell
pip install -r requirements.txt
```

Run a lightweight smoke check before changing the dashboard or data pipeline:

```powershell
python tools\smoke_check.py
```

The check validates the option-chain JSON shape and the `data_status.json` contract.

During US market hours, the check also inspects the SPX `asof` timestamp. By default stale data is reported as a warning so normal code changes are not blocked by an old committed data file. Data-update jobs run it in strict mode:

```powershell
$env:MATRIX_REQUIRE_FRESH_DATA = "1"
python tools\smoke_check.py
```

Refresh data manually:

```powershell
python fetch_lse.py
python tools\build_data_status.py
python tools\smoke_check.py
```

Quick monitoring checks:

```powershell
Invoke-WebRequest https://<your-site>/data_status.json
Invoke-WebRequest http://localhost:5000/status
```
