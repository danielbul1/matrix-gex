# Matrix

Options GEX dashboard for NDX, SPX, SPY, and QQQ.

The GitHub Pages entry point is `index.html`, which opens `gex_dashboard.html`.

Data is sourced from **London Strategic Edge (LSE)** and refreshed by `.github/workflows/update-cboe.yml`.
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
  published. The dashboard reads this branch (and the Railway API) at runtime.
- **B. Local Windows collector (DEPRECATED).** `run_cboe_collector.ps1`,
  `fetch_cboe.py`, `fetch_candles.py`, and `auto_deploy.ps1` are superseded by
  pipeline A and the Railway service. They are kept for reference only; each
  file carries a DEPRECATED header. Do not run them in production.
- **C. Local Flask server (`server.py`).** Optional single-symbol (SPY) live
  GEX backend with a 60-second cache, stale-while-revalidate fallback, and
  `/status` monitoring endpoint. Intended for local/personal use.

### Known gap: Railway service code is not yet in this repo (TODO)

The production backend at `https://api.trytripity.site` (serving `/matrix/`
and `/api/matrix/candles`, `/api/matrix/cboe-data`, `/api/matrix/flow`) is a
separate codebase that is not version-controlled here. Bring into this repo:

- [ ] Railway service source code (WebSocket collector + API + static hosting)
- [ ] Dockerfile / railway.toml / start-command configuration
- [ ] Its environment variable inventory (at minimum `LSE_API_KEY`)
- [ ] A `/data_status.json` endpoint on the Railway host (or update the
      runbook — the documented monitoring URL currently returns Not Found)

## LSE plan limits

The LSE free plan allows **~10 historical downloads per hour** and **up to 16
simultaneous WebSocket stream symbols**. How this repo stays within limits:

- Pipeline A runs **once per hour** (cron `13 13-20 * * 1-5`); one full
  refresh uses ~8 downloads (4 options snapshots + 4 candles) across the 4
  streamed symbols — within the 16-symbol cap and the hourly download cap.
  If the key is upgraded to a registered/paid plan, the workflow comment in
  `update-cboe.yml` explains how to restore 15-minute cadence.
- `server.py` caches LSE snapshots for 60 seconds and warns when upstream
  fetches exceed 8/hour; it never blocks requests.

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

The check validates the option-chain JSON shape, required dashboard DOM IDs, and common mojibake tokens in user-facing dashboard text.

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
