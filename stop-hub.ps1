# Stop Grok Remote Hub. Optionally keep or kill hub-owned agent on 2419.
#
# Params:
#   -KeepAgent   Do not kill grok agent serve (phone / multi-turn continuity).
#                Also keeps the external health watchdog (restart path used by
#                hub.watchdog via restart-hub.ps1 -KeepAgent).
#   -KillAgent   Kill hub-owned agent on the default serve port (full teardown).
#
# Defaults when neither switch is set: full stop — kill hub + agent + watchdog
# so intentional stop does not auto-restart.
# If both are passed, KeepAgent wins.
# Restart path (-KeepAgent): stop hub only; keep agent and keep watchdog.
param(
    [switch]$KeepAgent,
    [switch]$KillAgent
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "logs\hub.pid"
$WatchdogPidFile = Join-Path $Root "logs\watchdog.pid"
$AgentPidFile = Join-Path $Root "logs\agent.pid"
$SecretFile = Join-Path $Root "data\agent.secret"
$AgentPort = 2419

# Hub listen port from config.toml (default 8787).
$port = 8787
$cfg = Join-Path $Root "config.toml"
if (Test-Path $cfg) {
  $m = Select-String -Path $cfg -Pattern '^\s*bind_port\s*=\s*(\d+)' | Select-Object -First 1
  if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}

# KeepAgent wins when both are set; otherwise default = kill agent (full stop).
$doKillAgent = $true
if ($KeepAgent) {
    $doKillAgent = $false
} elseif ($KillAgent) {
    $doKillAgent = $true
} elseif (-not $KeepAgent -and -not $KillAgent) {
    $doKillAgent = $true
}

function Stop-PidSafe([int]$ProcessId) {
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    Write-Host "Stopping PID $ProcessId ($($proc.ProcessName))"
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    # Wait briefly for exit
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 100
    }
    return $true
}

$stopped = $false
if (Test-Path $PidFile) {
    $raw = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($raw -match '^\d+$') {
        $stopped = Stop-PidSafe ([int]$raw)
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# Kill full process tree for python -m hub (venv launcher + child).
# Exclude hub.watchdog — that is stopped separately so intentional stop does not auto-restart.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -notmatch 'hub\.watchdog' -and
        $_.CommandLine -match '-m\s+hub(\s|$)' -and
        ($_.CommandLine -like "*$Root*" -or $_.CommandLine -like "*Grok Remote Hub*")
    } |
    ForEach-Object {
        Write-Host "Stopping hub process PID $($_.ProcessId)"
        Stop-PidSafe ([int]$_.ProcessId) | Out-Null
        $stopped = $true
    }

# Anyone still listening on hub bind_port from our tree (not watchdog)
Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object {
        $op = $_.OwningProcess
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$op" -ErrorAction SilentlyContinue
        if ($p -and $p.CommandLine -and $p.CommandLine -match 'hub' -and $p.CommandLine -notmatch 'hub\.watchdog') {
            Write-Host "Stopping listener PID $op on $port"
            Stop-PidSafe ([int]$op) | Out-Null
            $stopped = $true
        }
    }

# Stop external health watchdog only on full intentional stop (not -KeepAgent).
# Restart path (watchdog -> restart-hub.ps1 -KeepAgent -> stop-hub -KeepAgent)
# must leave the watchdog alive or the long-lived daemon kills itself.
if (-not $KeepAgent) {
    $watchdogStopped = $false
    if (Test-Path -LiteralPath $WatchdogPidFile) {
        $wdRaw = (Get-Content -LiteralPath $WatchdogPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($wdRaw -match '^\d+$') {
            $wdPid = [int]$wdRaw
            if (Stop-PidSafe $wdPid) {
                $watchdogStopped = $true
            }
        }
        Remove-Item -LiteralPath $WatchdogPidFile -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match 'hub\.watchdog' -and
            ($_.CommandLine -like "*$Root*" -or $_.CommandLine -like "*Grok Remote Hub*")
        } |
        ForEach-Object {
            Write-Host "Stopping watchdog PID $($_.ProcessId)"
            Stop-PidSafe ([int]$_.ProcessId) | Out-Null
            $watchdogStopped = $true
            $stopped = $true
        }
    if ($watchdogStopped) {
        Write-Host "Watchdog stopped."
    }
} else {
    Write-Host "Keeping watchdog (-KeepAgent)."
}

$agentKilled = $false
if ($doKillAgent) {
    # Orphaned hub-owned agent: only kill if it looks like our serve on 2419 with our secret
    $secret = $null
    if (Test-Path $SecretFile) {
        $secret = (Get-Content $SecretFile -Raw -ErrorAction SilentlyContinue).Trim()
    }

    if (Test-Path $AgentPidFile) {
        $apRaw = (Get-Content $AgentPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($apRaw -match '^\d+$') {
            $ap = [int]$apRaw
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ap" -ErrorAction SilentlyContinue
            if ($p -and $p.CommandLine -and $p.CommandLine -match 'serve') {
                Write-Host "Stopping agent from pid file PID $ap"
                Stop-PidSafe $ap | Out-Null
                $agentKilled = $true
                $stopped = $true
            }
        }
        Remove-Item $AgentPidFile -Force -ErrorAction SilentlyContinue
    }

    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^(grok|grok\.exe)$' -and
            $_.CommandLine -and
            $_.CommandLine -match 'agent' -and
            $_.CommandLine -match 'serve' -and
            $_.CommandLine -match "127\.0\.0\.1:$AgentPort"
        } |
        ForEach-Object {
            $cmd = $_.CommandLine
            $own = $false
            # Own if secret appears in argv (hub-spawned serve --secret <ours>)
            if ($secret -and $cmd.Contains($secret)) {
                $own = $true
            } elseif ($cmd -match '127\.0\.0\.1:2419' -and $cmd -match 'serve') {
                # Hub default bind; safe for single-user local agent port
                $own = $true
            }
            if ($own) {
                Write-Host "Stopping hub-owned agent PID $($_.ProcessId)"
                Stop-PidSafe ([int]$_.ProcessId) | Out-Null
                $agentKilled = $true
                $stopped = $true
            }
        }
} else {
    Write-Host "Keeping agent on port $AgentPort (-KeepAgent)."
}

if (-not $stopped -and -not $agentKilled) {
    if ($doKillAgent) {
        Write-Host "No hub process found."
    } else {
        Write-Host "Hub stop complete (agent kept)."
    }
} else {
    Write-Host "Hub stopped."
    if ($agentKilled) {
        Write-Host "Agent on port $AgentPort stopped."
    } elseif (-not $doKillAgent) {
        Write-Host "Agent on port $AgentPort left running."
    }
}
