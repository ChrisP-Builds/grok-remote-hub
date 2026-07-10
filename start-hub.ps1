# Start Grok Remote Hub (venv, deps, background process)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Logs = Join-Path $Root "logs"
$PidFile = Join-Path $Logs "hub.pid"

if (-not (Test-Path $Python)) {
    Write-Host "Creating venv..."
    py -3 -m venv $Venv
    if (-not (Test-Path $Python)) {
        python -m venv $Venv
    }
}

& $Python -m pip install -q -r (Join-Path $Root "requirements.txt")

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

if (Test-Path $PidFile) {
    $oldPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($oldPid -match '^\d+$') {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "Hub already running (PID $oldPid)"
            # Still report health URLs if reachable
        } else {
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        }
    }
}

$day = (Get-Date).ToString("yyyyMMdd")
$outLog = Join-Path $Logs "hub-stdout-$day.log"
$errLog = Join-Path $Logs "hub-stderr-$day.log"
$hubLog = Join-Path $Logs "hub-$day.log"

$already = $false
if (Test-Path $PidFile) {
    $checkPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($checkPid -match '^\d+$' -and (Get-Process -Id ([int]$checkPid) -ErrorAction SilentlyContinue)) {
        $already = $true
    }
}

if (-not $already) {
    # Hub FileHandler also writes logs/hub-YYYYMMDD.log.
    # Redirected Start-Process keeps a stable PID for hub.pid.
    $p = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "hub") `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -PassThru

    $p.Id | Set-Content -Path $PidFile -Encoding ascii
    Write-Host "Started hub PID $($p.Id)"
}

# Resolve Tailscale URL
$tsIp = $null
$tsExe = "C:\Program Files\Tailscale\tailscale.exe"
if (Test-Path $tsExe) {
    try {
        $tsIp = (& $tsExe ip -4 2>$null | Select-Object -First 1).Trim()
    } catch {}
}
if (-not $tsIp) {
    try {
        $tsIp = (tailscale ip -4 2>$null | Select-Object -First 1).Trim()
    } catch {}
}

$port = 8787
$cfg = Join-Path $Root "config.toml"
if (Test-Path $cfg) {
    $m = Select-String -Path $cfg -Pattern '^\s*bind_port\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}

$candidates = @("127.0.0.1")
if ($tsIp -and $tsIp -ne "127.0.0.1") {
    $candidates += $tsIp
}

function Test-HubHealth([string]$HostAddr, [int]$PortNum) {
    $url = "http://${HostAddr}:${PortNum}/health"
    try {
        $r = Invoke-RestMethod -Uri $url -TimeoutSec 2 -ErrorAction Stop
        return ($null -ne $r -and $r.ok -eq $true)
    } catch {
        return $false
    }
}

Write-Host "Waiting for health (up to 30s)..."
$deadline = (Get-Date).AddSeconds(30)
$okHosts = @()
while ((Get-Date) -lt $deadline) {
    $okHosts = @()
    foreach ($h in $candidates) {
        if (Test-HubHealth $h $port) {
            $okHosts += $h
        }
    }
    # Success when localhost is up; Tailscale optional but preferred when present
    if ($okHosts -contains "127.0.0.1") {
        break
    }
    # Or at least one host if only TS was bound (legacy)
    if ($okHosts.Count -gt 0 -and -not ($candidates -contains "127.0.0.1")) {
        break
    }
    Start-Sleep -Milliseconds 500
}

if ($okHosts.Count -eq 0) {
    Write-Host "ERROR: Hub did not become healthy within 30s."
    Write-Host "  Logs:"
    Write-Host "    $errLog"
    Write-Host "    $outLog"
    Write-Host "    $hubLog"
    exit 1
}

Write-Host ""
Write-Host "Hub is up. Working URLs:"
foreach ($h in $candidates) {
    $url = "http://${h}:${port}"
    if ($okHosts -contains $h) {
        Write-Host "  OK  $url"
    } else {
        Write-Host "  --  $url  (not responding yet)"
    }
}
if (-not $tsIp) {
    Write-Host "(Tailscale IP not found; local only)"
}
Write-Host ""
exit 0
