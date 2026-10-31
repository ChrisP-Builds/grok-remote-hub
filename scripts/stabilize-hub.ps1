# Stabilize: one hub, one agent, one watchdog. KeepAgent hub bounce.
$ErrorActionPreference = "Continue"
$Root = "D:\Projects\Grok Remote Hub"
Set-Location $Root

Write-Host "=== Stop all hub / hub.watchdog ==="
& powershell -NoProfile -ExecutionPolicy Bypass -File ".\stop-hub.ps1" 2>&1
Start-Sleep -Seconds 2

# Extra cleanup for duplicates
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        ($_.CommandLine -match 'hub\.watchdog' -or ($_.CommandLine -match '-m\s+hub(\s|$)' -and $_.CommandLine -notmatch 'watchdog')) -and
        ($_.CommandLine -like "*Grok Remote Hub*" -or $_.CommandLine -like "*$Root*")
    } |
    ForEach-Object {
        Write-Host ("Force stop pid={0}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Write-Host "=== Start hub (ensures agent + watchdog) ==="
& powershell -NoProfile -ExecutionPolicy Bypass -File ".\start-hub.ps1" 2>&1

Start-Sleep -Seconds 3
$tok = ""
$m = Select-String -Path ".\config.toml" -Pattern 'hub_token\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($m) { $tok = $m.Matches[0].Groups[1].Value }

$up = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $url = if ($tok) { "http://127.0.0.1:8787/health?token=$tok" } else { "http://127.0.0.1:8787/health" }
        $h = Invoke-RestMethod $url -TimeoutSec 3
        if ($h.ok -and $h.acpConnected) {
            Write-Host ("STABLE ok agent={0} acp={1} process={2} pid={3} boot={4}" -f $h.agent, $h.acpQuality, $h.agentProcess, $h.pid, $h.bootId)
            $up = $true
            break
        }
        Write-Host ("wait acpConnected={0} process={1}" -f $h.acpConnected, $h.agentProcess)
    } catch {
        Write-Host ("wait health: {0}" -f $_.Exception.Message)
    }
    Start-Sleep -Seconds 1
}
if (-not $up) { Write-Host "NOT_STABLE"; exit 1 }

$hubN = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match '-m\s+hub(\s|$)' -and $_.CommandLine -notmatch 'watchdog' -and $_.CommandLine -like "*Grok Remote Hub*"
}).Count
$wdN = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'hub\.watchdog'
}).Count
Write-Host ("hub_procs={0} watchdog_procs={1}" -f $hubN, $wdN)
if ($tok) {
    Write-Host ("local=http://127.0.0.1:8787/?token={0}" -f $tok)
}
Write-Host "STABILIZE_DONE"
