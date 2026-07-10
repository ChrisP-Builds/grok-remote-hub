# Stop Grok Remote Hub and hub-owned agent (if orphaned)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "logs\hub.pid"
$SecretFile = Join-Path $Root "data\agent.secret"
$AgentPort = 2419

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

# Fallback: find python -m hub in this root
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match '-m\s+hub' -and
        ($_.CommandLine -like "*$Root*" -or $_.CommandLine -like "*Grok Remote Hub*")
    } |
    ForEach-Object {
        Write-Host "Stopping leftover hub process PID $($_.ProcessId)"
        Stop-PidSafe ([int]$_.ProcessId) | Out-Null
        $stopped = $true
    }

# Orphaned hub-owned agent: only kill if it looks like our serve on 2419 with our secret
$secret = $null
if (Test-Path $SecretFile) {
    $secret = (Get-Content $SecretFile -Raw -ErrorAction SilentlyContinue).Trim()
}

$agentKilled = $false
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

if (-not $stopped) {
    Write-Host "No hub process found."
} else {
    Write-Host "Hub stopped."
    if ($agentKilled) {
        Write-Host "Agent on port $AgentPort stopped."
    }
}
