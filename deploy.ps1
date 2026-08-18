# ============================================================================
# DEPRECATED
# This manual deploy script belongs to the retired local collector pipeline,
# superseded by the GitHub Actions pipeline (.github/workflows/update-cboe.yml)
# and the Railway backend service at https://api.trytripity.site.
# It blindly commits and pushes whatever is in the working tree without any
# smoke check. Do not run in production; kept for reference.
# ============================================================================
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

$status = git status --short
if (-not $status) {
    Write-Host "No changes to deploy."
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
git add .
git commit -m "Update Matrix $stamp"
git push

Write-Host "Deployed to GitHub Pages."
