# Diagnose hub/agent disconnects
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $Root "hub"))) {
    $Root = "D:\Projects\Grok Remote Hub"
}
Set-Location $Root
$Logs = Join-Path $Root "logs"

Write-Host "=== HEALTH ==="
try {
    $unauth = Invoke-RestMethod "http://127.0.0.1:8787/health" -TimeoutSec 3
    Write-Host ("unauth ok={0} keys={1}" -f $unauth.ok, (($unauth.PSObject.Properties.Name) -join ","))
} catch {
    Write-Host ("unauth DOWN: {0}" -f $_.Exception.Message)
}

$tok = ""
$cfg = Join-Path $Root "config.toml"
if (Test-Path $cfg) {
    $m = Select-String -Path $cfg -Pattern 'hub_token\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $tok = $m.Matches[0].Groups[1].Value }
}
if ($tok) {
    try {
        $f = Invoke-RestMethod ("http://127.0.0.1:8787/health?token={0}" -f $tok) -TimeoutSec 5
        Write-Host ("agent={0} process={1} acpQuality={2} acpConnected={3}" -f $f.agent, $f.agentProcess, $f.acpQuality, $f.acpConnected)
        Write-Host ("healAttempts={0} healError={1} healExhausted={2}" -f $f.acpHealAttempts, $f.acpHealError, $f.acpHealExhausted)
        Write-Host ("uptime={0} bootId={1} wsClients={2} turnRunning={3}" -f $f.uptimeSeconds, $f.bootId, $f.wsClients, $f.turnRunning)
    } catch {
        Write-Host ("auth health fail: {0}" -f $_.Exception.Message)
    }
} else {
    Write-Host "no hub_token in config"
}

Write-Host "=== PROCESSES ==="
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and (
            $_.CommandLine -match '-m\s+hub' -or
            $_.CommandLine -match 'agent.*serve' -or
            $_.CommandLine -match 'agent.*leader' -or
            ($_.Name -match 'grok' -and $_.CommandLine -match 'serve|leader')
        )
    } |
    ForEach-Object {
        $c = $_.CommandLine
        if ($c.Length -gt 140) { $c = $c.Substring(0, 140) + "..." }
        Write-Host ("pid={0} {1}" -f $_.ProcessId, $c)
    }

Write-Host "=== HUB LOG TAIL ==="
$hl = Get-ChildItem $Logs -Filter "hub-*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($hl) {
    Write-Host $hl.FullName
    Get-Content $hl.FullName -Tail 60
} else { Write-Host "no hub log" }

Write-Host "=== ACP TRACE TAIL ==="
$at = Get-ChildItem $Logs -Filter "acp-trace-*.jsonl" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($at) {
    Write-Host $at.FullName
    Get-Content $at.FullName -Tail 25
} else { Write-Host "no acp trace" }

Write-Host "=== AGENT LOG TAIL ==="
$al = Get-ChildItem $Logs -Filter "agent-*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($al) {
    Write-Host $al.FullName
    Get-Content $al.FullName -Tail 40
} else { Write-Host "no agent log" }

Write-Host "=== PORT 2419 / 8787 ==="
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 2419, 8787 } |
    ForEach-Object { Write-Host ("listen port={0} pid={1}" -f $_.LocalPort, $_.OwningProcess) }

Write-Host "DIAG_DONE"
