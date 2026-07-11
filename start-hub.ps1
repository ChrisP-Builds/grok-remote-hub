# Start Grok Remote Hub (venv, deps, detached background process)
# Uses WMI Win32_Process.Create so the hub survives after this script exits
# (shell job objects kill Start-Process children).
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

function Get-HubProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match '-m\s+hub' -and
            ($_.CommandLine -like "*Grok Remote Hub*" -or $_.CommandLine -like "*$Root*")
        }
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

# Already healthy?
$port = 8787
$cfg = Join-Path $Root "config.toml"
if (Test-Path $cfg) {
    $m = Select-String -Path $cfg -Pattern '^\s*bind_port\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}

if (Test-HubHealth "127.0.0.1" $port) {
    $live = @(Get-HubProcesses)
    if ($live.Count -gt 0) {
        $live[0].ProcessId | Set-Content -Path $PidFile -Encoding ascii
        Write-Host "Hub already running (PID $($live[0].ProcessId))"
    } else {
        Write-Host "Hub already healthy on port $port"
    }
} else {
    # Clear stale pid
    if (Test-Path $PidFile) {
        $oldPid = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($oldPid -match '^\d+$') {
            $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
            if (-not $proc) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
        }
    }

    # Kill stale hub procs that are not healthy
    Get-HubProcesses | ForEach-Object {
        Write-Host "Stopping stale hub PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 400

    $py = (Resolve-Path $Python).Path
    $cmdLine = "`"$py`" -m hub"
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = $cmdLine
        CurrentDirectory = $Root
    }
    if ($r.ReturnValue -ne 0 -or -not $r.ProcessId) {
        Write-Host "ERROR: Failed to start hub (WMI ReturnValue=$($r.ReturnValue))"
        exit 1
    }
    $r.ProcessId | Set-Content -Path $PidFile -Encoding ascii
    Write-Host "Started hub PID $($r.ProcessId) (detached)"
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

$candidates = @("127.0.0.1")
if ($tsIp -and $tsIp -ne "127.0.0.1") {
    $candidates += $tsIp
}

$day = (Get-Date).ToString("yyyyMMdd")
$hubLog = Join-Path $Logs "hub-$day.log"

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
    if ($okHosts -contains "127.0.0.1") {
        break
    }
    Start-Sleep -Milliseconds 500
}

if ($okHosts.Count -eq 0) {
    Write-Host "ERROR: Hub did not become healthy within 30s."
    Write-Host "  Log: $hubLog"
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
if ($tsIp) {
    Write-Host ""
    Write-Host "Phone (Tailscale Connected): http://${tsIp}:${port}"
    # MagicDNS name is machine-specific; print IP only (run: tailscale status)
}
Write-Host ""
exit 0
