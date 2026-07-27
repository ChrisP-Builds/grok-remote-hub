# Register logon start + recurring health watchdog for Grok Remote Hub.
# Watch runs via wscript + VBS (window style 0) so no console flash for end users.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $Root "start-hub.ps1"
$WatchScript = Join-Path $Root "watch-hub.ps1"
$StartVbs = Join-Path $Root "start-hub-hidden.vbs"
$WatchVbs = Join-Path $Root "watch-hub-hidden.vbs"
$TaskNameStart = "GrokRemoteHub"
$TaskNameWatch = "GrokRemoteHubWatch"

if (-not (Test-Path $StartScript)) { throw "Missing $StartScript" }
if (-not (Test-Path $WatchScript)) { throw "Missing $WatchScript" }
if (-not (Test-Path $StartVbs)) { throw "Missing $StartVbs" }
if (-not (Test-Path $WatchVbs)) { throw "Missing $WatchVbs" }

$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
if (-not (Test-Path -LiteralPath $wscript)) {
    $wscript = "wscript.exe"
}

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Hidden

# --- Logon: start-hub (silent via VBS) ---
$actionStart = New-ScheduledTaskAction -Execute $wscript `
    -Argument "//B //Nologo `"$StartVbs`"" `
    -WorkingDirectory $Root
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
try {
    Register-ScheduledTask -TaskName $TaskNameStart -Action $actionStart `
        -Trigger $triggerLogon -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Scheduled task '$TaskNameStart' registered for user $env:USERNAME at logon."
    Write-Host "  Launcher: $StartVbs (silent, no console)"
} catch {
    Write-Host "WARN: could not register '$TaskNameStart': $($_.Exception.Message)"
}

# --- Every 2 minutes: watch-hub (silent via VBS) ---
$actionWatch = New-ScheduledTaskAction -Execute $wscript `
    -Argument "//B //Nologo `"$WatchVbs`"" `
    -WorkingDirectory $Root
$triggerWatch = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 9999)
try {
    Register-ScheduledTask -TaskName $TaskNameWatch -Action $actionWatch `
        -Trigger $triggerWatch -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Scheduled task '$TaskNameWatch' registered (every 2 minutes, silent)."
    Write-Host "  Launcher: $WatchVbs -> watch-hub.ps1 -Quiet (no console flash)"
} catch {
    Write-Host "WARN: could not register '$TaskNameWatch': $($_.Exception.Message)"
}
Write-Host "Task names: $TaskNameStart , $TaskNameWatch"
Write-Host "Desktop buttons (visible window on purpose): .\install-desktop-shortcuts.ps1"
