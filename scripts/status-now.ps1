$ErrorActionPreference = "Continue"
$Root = "D:\Projects\Grok Remote Hub"
Set-Location $Root
try {
    $u = Invoke-RestMethod "http://127.0.0.1:8787/health" -TimeoutSec 3
    Write-Host ("unauth ok={0}" -f $u.ok)
} catch {
    Write-Host ("unauth DOWN: {0}" -f $_.Exception.Message)
    exit 1
}
$tok = ""
$m = Select-String -Path ".\config.toml" -Pattern 'hub_token\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($m) { $tok = $m.Matches[0].Groups[1].Value }
if ($tok) {
    $h = Invoke-RestMethod ("http://127.0.0.1:8787/health?token={0}" -f $tok) -TimeoutSec 5
    Write-Host ("agent={0} process={1} acp={2} connected={3}" -f $h.agent, $h.agentProcess, $h.acpQuality, $h.acpConnected)
    Write-Host ("heal={0} exhausted={1} uptime={2}s boot={3} wsClients={4}" -f $h.acpHealAttempts, $h.acpHealExhausted, $h.uptimeSeconds, $h.bootId, $h.wsClients)
    Write-Host ("local=http://127.0.0.1:8787/?token={0}" -f $tok)
    if ($h.tailscaleIp) { Write-Host ("tailscale=http://{0}:8787/?token={1}" -f $h.tailscaleIp, $tok) }
}
$hubN = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match '-m\s+hub(\s|$)' -and $_.CommandLine -notmatch 'watchdog' -and $_.CommandLine -like '*Grok Remote Hub*'
}).Count
$wdN = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'hub\.watchdog'
}).Count
$agentN = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'agent' -and $_.CommandLine -match 'serve' -and $_.Name -match 'grok'
}).Count
Write-Host ("procs hub={0} watchdog={1} agent_serve={2}" -f $hubN, $wdN, $agentN)
