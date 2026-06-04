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
