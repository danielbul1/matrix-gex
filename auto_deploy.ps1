$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

function Invoke-Deploy {
    $status = git status --short
    if (-not $status) {
        return
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$stamp] Changes detected. Deploying..."

    git add .

    $staged = git diff --cached --name-only
    if (-not $staged) {
        Write-Host "[$stamp] No staged changes."
        return
    }

    git commit -m "Auto update $stamp"
    git push

    Write-Host "[$stamp] Pushed to GitHub Pages."
}

Write-Host "Matrix auto deploy is watching:"
Write-Host $repo
Write-Host "Keep this window open. Press Ctrl+C to stop."

Invoke-Deploy

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $repo
$watcher.IncludeSubdirectories = $true
$watcher.EnableRaisingEvents = $true
$watcher.NotifyFilter = [System.IO.NotifyFilters]'FileName, DirectoryName, LastWrite, Size'

$pending = $false
$lastEvent = Get-Date

$action = {
    $path = $Event.SourceEventArgs.FullPath
    if ($path -match "\\.git\\|\\__pycache__\\|cboe_refresh\.log$|\.pyc$") {
        return
    }
    $script:pending = $true
    $script:lastEvent = Get-Date
}

Register-ObjectEvent $watcher Created -Action $action | Out-Null
Register-ObjectEvent $watcher Changed -Action $action | Out-Null
Register-ObjectEvent $watcher Deleted -Action $action | Out-Null
Register-ObjectEvent $watcher Renamed -Action $action | Out-Null

while ($true) {
    Start-Sleep -Seconds 2
    if ($pending -and ((Get-Date) - $lastEvent).TotalSeconds -ge 6) {
        $pending = $false
        try {
            Invoke-Deploy
        } catch {
            Write-Host "Deploy failed: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}
