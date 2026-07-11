# Desktop terminal follower for Grok Remote Hub sessions (read-only tail).
# Usage:
#   .\follow.ps1
#   .\follow.ps1 --session 019f493c-af12-7652-a6d8-bf645c10921c
#   .\follow.ps1 --cwd "C:\path\to\your\project"
#   .\follow.ps1 -v --max-seconds 2
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "ERROR: venv python not found at $Python"
    Write-Host "Create it: python -m venv .venv ; .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

& $Python -m hub.follow @args
exit $LASTEXITCODE
