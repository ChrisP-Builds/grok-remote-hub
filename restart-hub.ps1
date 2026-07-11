# Schedule a fully detached hub restart (stop then start).
# Parent exits immediately so a hub-owned agent terminal can finish without
# being killed mid-chain. Child is created via WMI Win32_Process.Create
# (same pattern as start-hub.ps1); Start-Process alone dies with the job object.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

$day = (Get-Date).ToString("yyyyMMdd")
$LogFile = Join-Path $Logs "restart-hub-$day.log"
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $psExe)) {
    $psExe = "powershell.exe"
}

# Bake paths as JSON strings so spaces and special chars are safe in -EncodedCommand.
$rootJson = ConvertTo-Json -InputObject $Root -Compress
$logJson = ConvertTo-Json -InputObject $LogFile -Compress
$psJson = ConvertTo-Json -InputObject $psExe -Compress

# Child runs stop/start as separate powershell -File processes via cmd.exe so
# script `exit` codes stay isolated (no ExitException in this wrapper).
$child = @"
`$ErrorActionPreference = 'Continue'
`$Root = $rootJson
`$Log = $logJson
`$PsExe = $psJson
function WLog([string]`$m) {
    `$line = '{0} {1}' -f (Get-Date).ToString('o'), `$m
    Add-Content -LiteralPath `$Log -Value `$line -ErrorAction SilentlyContinue
    Write-Host `$line
}
Set-Location -LiteralPath `$Root
WLog 'restart-hub: scheduled child started'
Start-Sleep -Seconds 2

`$stopPath = Join-Path `$Root 'stop-hub.ps1'
WLog 'restart-hub: running stop-hub.ps1'
`$stopCmd = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" >> "{2}" 2>&1' -f `$PsExe, `$stopPath, `$Log
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
    exit 1
}

Write-Host "Restart scheduled (detached). Browser will reconnect when hub is up."
Write-Host "  child PID $($r.ProcessId)"
Write-Host "  Log: $LogFile"
exit 0
