# ============================================================================
# DEPRECATED (2026-07-19)
# This local collector loop is superseded by the GitHub Actions pipeline
# (.github/workflows/update-cboe.yml) and the Railway backend service at
# https://api.trytripity.site. Do not run in production; kept for reference.
# ============================================================================
$ErrorActionPreference = "Continue"

Set-Location -LiteralPath $PSScriptRoot

$marketTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$intervalSeconds = 60
$idleSeconds = 300
$logPath = Join-Path $PSScriptRoot "cboe_refresh.log"

function Get-EasternNow {
  return [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $marketTz)
}

function Test-MarketCollectionWindow {
  $now = Get-EasternNow
  if ($now.DayOfWeek -eq "Saturday" -or $now.DayOfWeek -eq "Sunday") {
    return $false
  }

  $open = $now.Date.AddHours(9).AddMinutes(30)
  $close = $now.Date.AddHours(16).AddMinutes(20)
  return ($now -ge $open -and $now -le $close)
}

function Write-CollectorLog($message) {
  $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  $line = "[$stamp] $message"
  Write-Output $line
  Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

Write-CollectorLog "CBOE collector started. Fetch interval: ${intervalSeconds}s during market hours."

while ($true) {
  if (Test-MarketCollectionWindow) {
    Write-CollectorLog "refresh started"
    python fetch_cboe.py 2>&1 | ForEach-Object {
      Write-Output $_
      Add-Content -LiteralPath $logPath -Value $_ -Encoding UTF8
    }
    Write-CollectorLog "refresh finished"
    Start-Sleep -Seconds $intervalSeconds
  } else {
    $et = Get-EasternNow
    Write-CollectorLog "market closed / idle check. Eastern time: $($et.ToString('yyyy-MM-dd HH:mm:ss'))"
    Start-Sleep -Seconds $idleSeconds
  }
}
