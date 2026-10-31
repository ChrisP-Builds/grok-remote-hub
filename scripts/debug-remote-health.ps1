# Overall health debug for Grok Remote Hub (local + Tailscale)
$ErrorActionPreference = "Continue"
$Root = "D:\Projects\Grok Remote Hub"
Set-Location $Root
$Logs = Join-Path $Root "logs"

function Section([string]$t) { Write-Host ""; Write-Host "=== $t ===" }

Section "TIME"
Write-Host (Get-Date -Format "o")

Section "CONFIG"
$tok = ""
$port = 8787
if (Test-Path .\config.toml) {
    $m = Select-String -Path .\config.toml -Pattern 'hub_token\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $tok = $m.Matches[0].Groups[1].Value; Write-Host ("hub_token set len={0}" -f $tok.Length) } else { Write-Host "hub_token empty/missing" }
    $pm = Select-String -Path .\config.toml -Pattern '^\s*bind_port\s*=\s*(\d+)' | Select-Object -First 1
    if ($pm) { $port = [int]$pm.Matches[0].Groups[1].Value }
    $bm = Select-String -Path .\config.toml -Pattern '^\s*backend\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($bm) { Write-Host ("agent backend={0}" -f $bm.Matches[0].Groups[1].Value) } else { Write-Host "agent backend=serve (default)" }
    Write-Host ("bind_port={0}" -f $port)
} else { Write-Host "no config.toml" }

Section "PROCESSES"
$hubN = 0; $wdN = 0; $serveN = 0; $leaderN = 0
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
    $c = $_.CommandLine
    if (-not $c) { return }
    if ($c -match 'hub\.watchdog' -and $c -like '*Grok Remote Hub*') {
        $wdN++; Write-Host ("watchdog pid={0}" -f $_.ProcessId)
    } elseif ($c -match '-m\s+hub(\s|$)' -and $c -notmatch 'watchdog' -and $c -like '*Grok Remote Hub*') {
        $hubN++; Write-Host ("hub pid={0}" -f $_.ProcessId)
    } elseif ($c -match 'agent' -and $c -match 'serve' -and $_.Name -match 'grok') {
        $serveN++; Write-Host ("serve pid={0} cmd={1}" -f $_.ProcessId, ($c.Substring(0, [Math]::Min(120, $c.Length))))
    } elseif ($c -match 'agent' -and $c -match 'leader' -and $_.Name -match 'grok') {
        $leaderN++; Write-Host ("leader pid={0}" -f $_.ProcessId)
    }
}
Write-Host ("counts hub={0} watchdog={1} serve={2} leader={3}" -f $hubN, $wdN, $serveN, $leaderN)

Section "PORTS"
foreach ($p in @($port, 2419)) {
    $listens = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $p })
    if ($listens.Count -eq 0) { Write-Host ("port {0}: NOT listening" -f $p) }
    else {
        foreach ($L in $listens) {
            Write-Host ("port {0}: listen pid={1} local={2}" -f $p, $L.OwningProcess, $L.LocalAddress)
        }
    }
}

Section "HEALTH UNAUTH (liveness)"
try {
    $u = Invoke-RestMethod ("http://127.0.0.1:{0}/health" -f $port) -TimeoutSec 5
    $keys = ($u.PSObject.Properties.Name) -join ","
    Write-Host ("ok={0} keys={1}" -f $u.ok, $keys)
    $rich = @("opsCounters", "acpTraceRecent", "liveTurns", "hosts", "tailscaleIp")
    $leaked = @($rich | Where-Object { $u.PSObject.Properties.Name -contains $_ })
    if ($tok -and $leaked.Count -gt 0) { Write-Host ("WARN unauth rich leak: {0}" -f ($leaked -join ",")) }
    elseif ($tok) { Write-Host "unauth liveness minimal OK (token set)" }
} catch {
    Write-Host ("FAIL: {0}" -f $_.Exception.Message)
}

Section "HEALTH AUTH (full)"
$h = $null
if ($tok) {
    try {
        $h = Invoke-RestMethod ("http://127.0.0.1:{0}/health?token={1}" -f $port, $tok) -TimeoutSec 8
        Write-Host ("ok={0}" -f $h.ok)
        Write-Host ("agent={0} process={1} acpQuality={2} acpConnected={3}" -f $h.agent, $h.agentProcess, $h.acpQuality, $h.acpConnected)
        Write-Host ("healAttempts={0} healError={1} healExhausted={2}" -f $h.acpHealAttempts, $h.acpHealError, $h.acpHealExhausted)
        Write-Host ("uptime={0}s bootId={1} pid={2} wsClients={3}" -f $h.uptimeSeconds, $h.bootId, $h.pid, $h.wsClients)
        Write-Host ("turnRunning={0} loadedSession={1} agentBackend={2}" -f $h.turnRunning, $h.loadedSessionId, $h.agentBackend)
        Write-Host ("bind={0} hosts={1} ts={2}" -f $h.bind, (($h.hosts) -join ","), $h.tailscaleIp)
        Write-Host ("hubVersion={0} cliVersion={1} compatOk={2}" -f $h.hubVersion, $h.cliVersion, $h.compatOk)
        if ($h.compatIssues) { Write-Host ("compatIssues={0}" -f ($h.compatIssues -join "; ")) }
        if ($h.opsCounters) {
            $c = $h.opsCounters
            Write-Host ("ops: ensure_reuse={0} ensure_load={1} ensure_new={2} prompt_ok={3} prompt_error={4} acp_heal_attempt={5} agent_restart={6} ws_client_reset={7}" -f `
                $c.ensure_reuse, $c.ensure_load, $c.ensure_new, $c.prompt_ok, $c.prompt_error, $c.acp_heal_attempt, $c.agent_restart, $c.ws_client_reset)
        }
    } catch {
        Write-Host ("FAIL auth health: {0}" -f $_.Exception.Message)
    }
} else {
    Write-Host "skip (no token)"
    try { $h = Invoke-RestMethod ("http://127.0.0.1:{0}/health" -f $port) -TimeoutSec 5 } catch {}
}

Section "TAILSCALE REACHABILITY"
$ts = if ($h -and $h.tailscaleIp) { $h.tailscaleIp } else { $null }
if (-not $ts) {
    try {
        $tsOut = & tailscale ip -4 2>$null
        if ($tsOut) { $ts = ($tsOut | Select-Object -First 1).Trim() }
    } catch {}
}
if ($ts) {
    try {
        $tu = Invoke-RestMethod ("http://{0}:{1}/health" -f $ts, $port) -TimeoutSec 5
        Write-Host ("ts unauth ok={0} from {1}" -f $tu.ok, $ts)
    } catch {
        Write-Host ("ts unauth FAIL {0}: {1}" -f $ts, $_.Exception.Message)
    }
    if ($tok) {
        try {
            $ta = Invoke-RestMethod ("http://{0}:{1}/health?token={2}" -f $ts, $port, $tok) -TimeoutSec 8
            Write-Host ("ts auth ok={0} agent={1} acp={2}" -f $ta.ok, $ta.agent, $ta.acpQuality)
        } catch {
            Write-Host ("ts auth FAIL: {0}" -f $_.Exception.Message)
        }
    }
} else {
    Write-Host "no tailscale IP"
}

Section "DIAGNOSTICS API"
if ($tok) {
    try {
        $d = Invoke-RestMethod ("http://127.0.0.1:{0}/api/admin/diagnostics?token={1}" -f $port, $tok) -TimeoutSec 10
        Write-Host ("diagnostics ok keys={0}" -f (($d.PSObject.Properties.Name) -join ","))
    } catch {
        Write-Host ("diagnostics: {0}" -f $_.Exception.Message)
    }
} else { Write-Host "skip" }

Section "STATIC SPA (token gate)"
try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/app.js" -f $port) -TimeoutSec 5 -UseBasicParsing
    Write-Host ("app.js status={0} len={1}" -f $r.StatusCode, $r.RawContentLength)
} catch {
    $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { "?" }
    Write-Host ("app.js FAIL status={0}" -f $code)
}
try {
    $r2 = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/sessions" -f $port) -TimeoutSec 5 -UseBasicParsing
    Write-Host ("api/sessions unauth status={0} (expect 401 if token set)" -f $r2.StatusCode)
} catch {
    $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { "?" }
    Write-Host ("api/sessions unauth status={0}" -f $code)
}

Section "WATCHDOG STATE"
$ws = Join-Path $Logs "watch-hub-state.json"
if (Test-Path $ws) { Get-Content $ws -Raw } else { Write-Host "no watch-hub-state.json" }
$wp = Join-Path $Logs "watchdog.pid"
if (Test-Path $wp) {
    $wpid = (Get-Content $wp -Raw).Trim()
    $alive = [bool](Get-Process -Id $wpid -ErrorAction SilentlyContinue)
    Write-Host ("watchdog.pid={0} alive={1}" -f $wpid, $alive)
}

Section "RECENT ERRORS (hub log)"
$hl = Get-ChildItem $Logs -Filter "hub-*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($hl) {
    Write-Host $hl.Name
    Select-String -Path $hl.FullName -Pattern "ERROR|WARNING|disconnect|ws_closed|heal|force_kill|Auth\(|no close frame" |
        Select-Object -Last 25 |
        ForEach-Object { "{0}|{1}" -f $_.LineNumber, $_.Line.Trim() }
} else { Write-Host "no hub log" }

Section "RECENT AGENT ERRORS"
$al = Get-ChildItem $Logs -Filter "agent-*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($al) {
    Write-Host $al.Name
    Select-String -Path $al.FullName -Pattern "ERROR|fatal|Auth\(|quit" |
        Select-Object -Last 15 |
        ForEach-Object { "{0}|{1}" -f $_.LineNumber, $_.Line.Trim() }
} else { Write-Host "no agent log" }

Section "GROK CLI VERSION"
$grok = Join-Path $env:USERPROFILE ".grok\bin\grok.exe"
if (Test-Path $grok) { & $grok --version 2>&1 } else { Write-Host "grok.exe not found" }

Section "SUMMARY"
$score = @()
if ($hubN -lt 1) { $score += "HUB_DOWN" }
if ($hubN -gt 2) { $score += "HUB_DUP_PROCS" }
if ($serveN -lt 1 -and $leaderN -lt 1) { $score += "AGENT_NOT_RUNNING" }
if ($h -and $h.ok -ne $true) { $score += "HEALTH_NOT_OK" }
if ($h -and $h.acpConnected -ne $true) { $score += "ACP_DOWN" }
if ($h -and $h.agentProcess -ne "up") { $score += "AGENT_PROCESS_DOWN" }
if ($h -and $h.acpHealExhausted -eq $true) { $score += "HEAL_EXHAUSTED" }
if ($score.Count -eq 0) { Write-Host "OVERALL: HEALTHY" } else { Write-Host ("OVERALL: ISSUES -> {0}" -f ($score -join ", ")) }
Write-Host "DEBUG_DONE"
