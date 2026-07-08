# Matrix

Options GEX dashboard for NDX, SPX, SPY, QQQ, IWM, AAPL, NVDA, and TSLA.

The GitHub Pages entry point is `index.html`, which opens `gex_dashboard.html`.

Data is stored in `cboe_data.json` and refreshed by `.github/workflows/update-cboe.yml`.
Operational status is published in `data_status.json` for lightweight external monitoring.
When using the Flask server, the same status is available at `/status`; it returns HTTP 200 for `fresh`/`off_hours` and HTTP 503 for `stale`, `future`, `unknown`, or missing status data.

See `docs/RUNBOOK.md` for operational checks, manual refresh steps, and pre-commit validation.

## Local validation

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

Regenerate the status file after manually refreshing data:

```powershell
python tools\build_data_status.py
```

Quick monitoring checks:

```powershell
Invoke-WebRequest https://<your-site>/data_status.json
Invoke-WebRequest http://localhost:5000/status
```
