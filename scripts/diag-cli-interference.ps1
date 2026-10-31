$ErrorActionPreference = "Continue"
$Root = "D:\Projects\Grok Remote Hub"
Set-Location $Root

Write-Host "=== config agent backend ==="
if (Test-Path .\config.toml) {
    Select-String -Path .\config.toml -Pattern 'backend|agent|bind|port|hub_token' |
        ForEach-Object { $_.Line.Trim() }
} else { Write-Host "no config.toml" }

Write-Host "=== hub health ==="
$tok = ""
$m = Select-String -Path .\config.toml -Pattern 'hub_token\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($m) { $tok = $m.Matches[0].Groups[1].Value }
try {
    $url = if ($tok) { "http://127.0.0.1:8787/health?token=$tok" } else { "http://127.0.0.1:8787/health" }
    $h = Invoke-RestMethod $url -TimeoutSec 5
    Write-Host ("ok={0} agentBackend={1} process={2} acp={3} connected={4}" -f $h.ok, $h.agentBackend, $h.agentProcess, $h.acpQuality, $h.acpConnected)
} catch { Write-Host ("health fail: {0}" -f $_.Exception.Message) }

Write-Host "=== all grok processes ==="
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'grok' -or ($_.CommandLine -and $_.CommandLine -match 'grok') } |
    ForEach-Object {
        $c = if ($_.CommandLine) { $_.CommandLine } else { "" }
        if ($c.Length -gt 160) { $c = $c.Substring(0, 160) + "..." }
        Write-Host ("pid={0} name={1} cmd={2}" -f $_.ProcessId, $_.Name, $c)
    }

Write-Host "=== hub/watchdog processes ==="
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'hub' -and $_.CommandLine -like '*Grok Remote Hub*' } |
    ForEach-Object {
        $c = $_.CommandLine
        if ($c.Length -gt 140) { $c = $c.Substring(0, 140) + "..." }
        Write-Host ("pid={0} {1}" -f $_.ProcessId, $c)
    }

Write-Host "=== recent hub log kill/restart/exit ==="
$hl = Get-ChildItem .\logs -Filter "hub-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($hl) {
    Write-Host $hl.Name
    Select-String -Path $hl.FullName -Pattern "force_kill|force_restart|Agent process exited|leader|KillAgent|taskkill|spawned|port closed|External agent" |
        Select-Object -Last 40 |
        ForEach-Object { "{0}: {1}" -f $_.LineNumber, $_.Line }
}

Write-Host "=== agent log tail errors ==="
$al = Get-ChildItem .\logs -Filter "agent-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($al) {
    Write-Host $al.Name
    Get-Content $al.FullName -Tail 25
}
Write-Host "DONE"
