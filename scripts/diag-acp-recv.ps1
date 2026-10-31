$ErrorActionPreference = "Continue"
$Root = "D:\Projects\Grok Remote Hub"
$Logs = Join-Path $Root "logs"
Write-Host "=== ACP / disconnect / close frame in hub log ==="
$hl = Get-ChildItem $Logs -Filter "hub-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($hl) {
    Write-Host $hl.Name
    Select-String -Path $hl.FullName -Pattern "no close frame|recv ended|ws_closed|disconnect|quality|session_switch|ensure action|resume|foreign|cli_or" -CaseSensitive:$false |
        Select-Object -Last 50 |
        ForEach-Object { "{0}|{1}" -f $_.LineNumber, $_.Line }
}
Write-Host "=== acp-trace disconnect/connect ==="
$at = Get-ChildItem $Logs -Filter "acp-trace-*.jsonl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($at) {
    Write-Host $at.Name
    Get-Content $at.FullName -Tail 80 | Where-Object { $_ -match "disconnect|connect|quality|initialize|heal" }
}
Write-Host "=== health ==="
$tok = ""
$m = Select-String -Path (Join-Path $Root "config.toml") -Pattern 'hub_token\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($m) { $tok = $m.Matches[0].Groups[1].Value }
try {
    $url = if ($tok) { "http://127.0.0.1:8787/health?token=$tok" } else { "http://127.0.0.1:8787/health" }
    $h = Invoke-RestMethod $url -TimeoutSec 5
    Write-Host ("ok={0} acp={1} connected={2} process={3} loaded={4} turn={5} backend={6}" -f $h.ok, $h.acpQuality, $h.acpConnected, $h.agentProcess, $h.loadedSessionId, $h.turnRunning, $h.agentBackend)
} catch { Write-Host $_.Exception.Message }
Write-Host "DONE"
