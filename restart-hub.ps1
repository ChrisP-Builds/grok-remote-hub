# Schedule a fully detached hub restart (stop then start).
# Parent exits immediately so a hub-owned agent terminal can finish without
# being killed mid-chain. Child is created via WMI Win32_Process.Create
# (same pattern as start-hub.ps1); Start-Process alone dies with the job object.
#
# Default: keep agent serve across hub bounce (pass -KeepAgent to stop-hub).
# -KillAgent: full teardown of hub + agent before start.
# -NoWait: fire-and-forget (agent-owned restarts must use this).
param(
    [switch]$NoWait,
    [switch]$KillAgent
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

$day = (Get-Date).ToString("yyyyMMdd")
$LogFile = Join-Path $Logs "restart-hub-$day.log"
$StatusFile = Join-Path $Logs "restart-status.json"
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $psExe)) {
    $psExe = "powershell.exe"
}

function Get-HubPort {
    $port = 8787
    $cfg = Join-Path $Root "config.toml"
    if (Test-Path -LiteralPath $cfg) {
        $m = Select-String -Path $cfg -Pattern '^\s*bind_port\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
    }
    return $port
}

function Get-HubHealth([int]$PortNum) {
    $url = "http://127.0.0.1:${PortNum}/health"
    try {
        return Invoke-RestMethod -Uri $url -TimeoutSec 2 -ErrorAction Stop
    } catch {
        return $null
    }
}

function Write-RestartStatus {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        $PreBootId = $null,
        $BootId = $null,
        $ChildPid = $null
    )
    $obj = [ordered]@{
        state     = $State
        preBootId = $PreBootId
        bootId    = $BootId
        at        = (Get-Date).ToUniversalTime().ToString("o")
        pid       = $ChildPid
    }
    ($obj | ConvertTo-Json -Compress) | Set-Content -LiteralPath $StatusFile -Encoding utf8
}

$port = Get-HubPort
$preBootId = $null
$pre = Get-HubHealth $port
if ($null -ne $pre -and $pre.ok -eq $true -and $pre.bootId) {
    $preBootId = [string]$pre.bootId
}

# Bake paths as JSON strings so spaces and special chars are safe in -EncodedCommand.
$rootJson = ConvertTo-Json -InputObject $Root -Compress
$logJson = ConvertTo-Json -InputObject $LogFile -Compress
$psJson = ConvertTo-Json -InputObject $psExe -Compress
# Default restart keeps agent; only pass kill when -KillAgent.
$stopArgs = if ($KillAgent) { "" } else { " -KeepAgent" }
$stopArgsJson = ConvertTo-Json -InputObject $stopArgs -Compress

# Child runs stop/start as separate powershell -File processes via cmd.exe so
# script `exit` codes stay isolated (no ExitException in this wrapper).
$child = @"
`$ErrorActionPreference = 'Continue'
`$Root = $rootJson
`$Log = $logJson
`$PsExe = $psJson
`$StopArgs = $stopArgsJson
function WLog([string]`$m) {
    `$line = '{0} {1}' -f (Get-Date).ToString('o'), `$m
    Add-Content -LiteralPath `$Log -Value `$line -ErrorAction SilentlyContinue
    Write-Host `$line
}
Set-Location -LiteralPath `$Root
WLog 'restart-hub: scheduled child started'
Start-Sleep -Seconds 2

`$stopPath = Join-Path `$Root 'stop-hub.ps1'
WLog ('restart-hub: running stop-hub.ps1' + `$StopArgs)
`$stopCmd = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}"{2} >> "{3}" 2>&1' -f `$PsExe, `$stopPath, `$StopArgs, `$Log
cmd.exe /c `$stopCmd | Out-Null
`$stopCode = 0
if (`$null -ne `$LASTEXITCODE) { `$stopCode = [int]`$LASTEXITCODE }
WLog ('restart-hub: stop-hub.ps1 exit=' + `$stopCode)

`$startPath = Join-Path `$Root 'start-hub.ps1'
WLog 'restart-hub: running start-hub.ps1'
`$startCmd = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" >> "{2}" 2>&1' -f `$PsExe, `$startPath, `$Log
cmd.exe /c `$startCmd | Out-Null
`$startCode = 0
if (`$null -ne `$LASTEXITCODE) { `$startCode = [int]`$LASTEXITCODE }
WLog ('restart-hub: start-hub.ps1 exit=' + `$startCode)

WLog ('restart-hub: complete stop=' + `$stopCode + ' start=' + `$startCode)
if (`$startCode -ne 0) { exit `$startCode }
exit 0
"@

$encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($child))
$cmdLine = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"

$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $cmdLine
    CurrentDirectory = $Root
}
if ($r.ReturnValue -ne 0 -or -not $r.ProcessId) {
    Write-Host "ERROR: Failed to schedule restart (WMI ReturnValue=$($r.ReturnValue))"
    Write-RestartStatus -State "failed" -PreBootId $preBootId -BootId $null -ChildPid $null
    exit 1
}

$childPid = [int]$r.ProcessId
Write-RestartStatus -State "scheduled" -PreBootId $preBootId -BootId $null -ChildPid $childPid
Write-Host "Restart scheduled (detached). Browser will reconnect when hub is up."
if ($KillAgent) {
    Write-Host "  mode: full stop (hub + agent)"
} else {
    Write-Host "  mode: keep agent across hub bounce"
}
Write-Host "  child PID $childPid"
Write-Host "  Log: $LogFile"
if ($preBootId) {
    Write-Host "  preBootId: $preBootId"
}

if ($NoWait) {
    exit 0
}

Write-Host "Waiting for hub restart…"
$deadline = (Get-Date).AddSeconds(90)
$newBootId = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 750
    $h = Get-HubHealth $port
    if ($null -eq $h -or $h.ok -ne $true) { continue }
    $bid = $null
    if ($h.bootId) { $bid = [string]$h.bootId }
    if ($preBootId) {
        if (-not $bid -or $bid -eq $preBootId) { continue }
        $newBootId = $bid
        break
    }
    # No preBootId: any healthy response is success
    $newBootId = if ($bid) { $bid } else { "(unknown)" }
    break
}

if (-not $newBootId) {
    Write-Host "ERROR: Timed out waiting for hub to become healthy (90s)."
    Write-Host "  Log: $LogFile"
    Write-Host "  Status: $StatusFile"
    Write-RestartStatus -State "timeout" -PreBootId $preBootId -BootId $null -ChildPid $childPid
    exit 1
}

Write-RestartStatus -State "healthy" -PreBootId $preBootId -BootId $newBootId -ChildPid $childPid
Write-Host "Hub restarted and healthy (bootId=$newBootId)"
Write-Host "  http://127.0.0.1:$port/"
exit 0
