# External health watchdog for Grok Remote Hub.
# Polls http://127.0.0.1:<port>/health; on N consecutive failures (default 2)
# runs restart-hub.ps1 -KeepAgent (cooldown default 5 minutes).
# Always exits 0 so scheduled-task failure spam stays quiet.
param(
    [int]$MaxFails = 2,
    [int]$CooldownMinutes = 5,
    [switch]$Quiet
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

$day = (Get-Date).ToString("yyyyMMdd")
$LogFile = Join-Path $Logs "watch-hub-$day.log"
$StateFile = Join-Path $Logs "watch-hub-state.json"

function WLog([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK') $msg"
    Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8 -ErrorAction SilentlyContinue
    if (-not $Quiet) {
        Write-Host $line
    }
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

function Read-WatchState {
    $defaults = [ordered]@{
        consecutiveFails = 0
        lastRestartAt    = $null
        lastOkAt         = $null
        lastError        = $null
    }
    if (-not (Test-Path -LiteralPath $StateFile)) {
        return $defaults
    }
    try {
        $raw = Get-Content -LiteralPath $StateFile -Raw -ErrorAction Stop
        $j = $raw | ConvertFrom-Json -ErrorAction Stop
        return [ordered]@{
            consecutiveFails = if ($null -ne $j.consecutiveFails) { [int]$j.consecutiveFails } else { 0 }
            lastRestartAt    = $j.lastRestartAt
            lastOkAt         = $j.lastOkAt
            lastError        = $j.lastError
        }
    } catch {
        return $defaults
    }
}

function Write-WatchState($state) {
    $obj = [ordered]@{
        consecutiveFails = [int]$state.consecutiveFails
        lastRestartAt    = $state.lastRestartAt
        lastOkAt         = $state.lastOkAt
        lastError        = $state.lastError
    }
    ($obj | ConvertTo-Json -Compress) | Set-Content -LiteralPath $StateFile -Encoding utf8
}

function Test-HubHealth([int]$PortNum) {
    $url = "http://127.0.0.1:${PortNum}/health"
    try {
        $r = Invoke-RestMethod -Uri $url -TimeoutSec 3 -ErrorAction Stop
        if ($null -ne $r -and $r.ok -eq $true) {
            return @{ ok = $true; error = $null }
        }
        return @{ ok = $false; error = "health ok!=true" }
    } catch {
        return @{ ok = $false; error = $_.Exception.Message }
    }
}

function Test-CooldownElapsed($lastRestartAt, [int]$Minutes) {
    if (-not $lastRestartAt) { return $true }
    try {
        $t = [datetime]::Parse([string]$lastRestartAt, $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
        return ((Get-Date).ToUniversalTime() - $t.ToUniversalTime()).TotalMinutes -ge $Minutes
    } catch {
        return $true
    }
}

$port = Get-HubPort
$state = Read-WatchState
$health = Test-HubHealth $port

if ($health.ok) {
    $state.consecutiveFails = 0
    $state.lastOkAt = (Get-Date).ToUniversalTime().ToString("o")
    $state.lastError = $null
    Write-WatchState $state
    WLog "OK port=$port"
    exit 0
}

$state.consecutiveFails = [int]$state.consecutiveFails + 1
$state.lastError = $health.error
Write-WatchState $state
WLog "FAIL port=$port fails=$($state.consecutiveFails)/$MaxFails err=$($health.error)"

if ($state.consecutiveFails -lt $MaxFails) {
    exit 0
}

if (-not (Test-CooldownElapsed $state.lastRestartAt $CooldownMinutes)) {
    WLog "SKIP restart (cooldown ${CooldownMinutes}m lastRestartAt=$($state.lastRestartAt))"
    exit 0
}

$restartScript = Join-Path $Root "restart-hub.ps1"
if (-not (Test-Path -LiteralPath $restartScript)) {
    WLog "ERROR missing restart-hub.ps1"
    exit 0
}

WLog "RESTART invoking restart-hub.ps1 -KeepAgent"
try {
    $psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $psExe)) { $psExe = "powershell.exe" }
    $p = Start-Process -FilePath $psExe -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $restartScript, "-KeepAgent"
    ) -WorkingDirectory $Root -Wait -PassThru -WindowStyle Hidden
    $code = if ($null -ne $p) { $p.ExitCode } else { -1 }
    $state.lastRestartAt = (Get-Date).ToUniversalTime().ToString("o")
    $state.consecutiveFails = 0
    Write-WatchState $state
    WLog "RESTART done exit=$code"
} catch {
    $state.lastError = $_.Exception.Message
    Write-WatchState $state
    WLog "RESTART error: $($_.Exception.Message)"
}

exit 0
