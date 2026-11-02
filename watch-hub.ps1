# Optional one-shot health check for Grok Remote Hub (manual debug).
# Thin wrapper around: python -m hub.watchdog --once
# Single source of truth is hub/watchdog.py (long-lived daemon).
# Always exits 0 so scheduled-task / scripts stay quiet.
param(
    [int]$MaxFails = 2,
    [int]$CooldownMinutes = 5,
    [switch]$Quiet
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPy) { $VenvPy } else { "python.exe" }

$pyArgs = @(
    "-m", "hub.watchdog",
    "--once",
    "--max-fails", "$MaxFails",
    "--cooldown-minutes", "$CooldownMinutes",
    "--root", $Root
)

try {
    if ($Quiet) {
        & $Python @pyArgs 2>&1 | Out-Null
    } else {
        & $Python @pyArgs
    }
} catch {
    if (-not $Quiet) {
        Write-Host "watch-hub.ps1 wrapper error: $($_.Exception.Message)"
    }
}

exit 0
