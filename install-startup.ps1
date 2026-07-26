# Register logon start + recurring health watchdog for Grok Remote Hub
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $Root "start-hub.ps1"
$WatchScript = Join-Path $Root "watch-hub.ps1"
$TaskNameStart = "GrokRemoteHub"
$TaskNameWatch = "GrokRemoteHubWatch"

if (-not (Test-Path $StartScript)) {
    throw "Missing $StartScript"
}
if (-not (Test-Path $WatchScript)) {
    throw "Missing $WatchScript"
}

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

# --- Logon: start-hub ---
$actionStart = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`"" `
    -WorkingDirectory $Root
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName $TaskNameStart -Action $actionStart `
    -Trigger $triggerLogon -Settings $settings -Principal $principal -Force | Out-Null

# --- Every 2 minutes: watch-hub -Quiet ---
$actionWatch = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WatchScript`" -Quiet" `
    -WorkingDirectory $Root
# Once + 2-minute repetition for ~27 years (indefinite enough for Task Scheduler)
$triggerWatch = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 9999)
Register-ScheduledTask -TaskName $TaskNameWatch -Action $actionWatch `
    -Trigger $triggerWatch -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Scheduled task '$TaskNameStart' registered for user $env:USERNAME at logon."
Write-Host "  Start script: $StartScript"
Write-Host "Scheduled task '$TaskNameWatch' registered (every 2 minutes)."
Write-Host "  Watch script: $WatchScript -Quiet"
Write-Host "Task names: $TaskNameStart , $TaskNameWatch"
