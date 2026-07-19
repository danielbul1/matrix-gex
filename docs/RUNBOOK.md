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

## Local Dashboard Check

```powershell
python -m http.server 8765 --bind 127.0.0.1
```

Open:

```text
http://127.0.0.1:8765/gex_dashboard.html
```

To force the stale-data banner for UI verification:

```text
http://127.0.0.1:8765/gex_dashboard.html?debugStaleBanner=1
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

## Data Source Notes


