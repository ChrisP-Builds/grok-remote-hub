# Create desktop shortcuts to start / stop / restart Grok Remote Hub.
# Run once:  powershell -File .\install-desktop-shortcuts.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $Root "start-hub.ps1"
$StopScript = Join-Path $Root "stop-hub.ps1"
$RestartScript = Join-Path $Root "restart-hub.ps1"

foreach ($p in @($StartScript, $StopScript, $RestartScript)) {
    if (-not (Test-Path -LiteralPath $p)) {
        throw "Missing $p"
    }
}

$Desktop = [Environment]::GetFolderPath("Desktop")
if (-not $Desktop -or -not (Test-Path -LiteralPath $Desktop)) {
    $Desktop = Join-Path $env:USERPROFILE "Desktop"
}
if (-not (Test-Path -LiteralPath $Desktop)) {
    throw "Desktop folder not found"
}

$Wsh = New-Object -ComObject WScript.Shell
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $psExe)) {
    $psExe = "powershell.exe"
}

function New-HubShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string]$ScriptArgs = "",
        [string]$Description = ""
    )
    $lnkPath = Join-Path $Desktop "$Name.lnk"
    # Show window so the user sees success/failure; closes after Enter.
    $argList = @(
        "-NoProfile"
        "-ExecutionPolicy Bypass"
        "-NoExit"
        "-Command"
        "& { Set-Location -LiteralPath '$Root'; & '$ScriptPath' $ScriptArgs; Write-Host ''; Write-Host 'Done. Press Enter to close...' -ForegroundColor Cyan; [void][Console]::ReadLine() }"
    )
    $shortcut = $Wsh.CreateShortcut($lnkPath)
    $shortcut.TargetPath = $psExe
    $shortcut.Arguments = ($argList -join " ")
    $shortcut.WorkingDirectory = $Root
    $shortcut.WindowStyle = 1
    $shortcut.Description = if ($Description) { $Description } else { $Name }
    # Prefer hub favicon as icon when possible (ICO not available; use powershell)
    $shortcut.IconLocation = "$psExe,0"
    $shortcut.Save()
    Write-Host "Created: $lnkPath"
}

New-HubShortcut -Name "Start Grok Hub" -ScriptPath $StartScript `
    -Description "Start Grok Remote Hub (keeps agent if already up)"

# Full stop of hub + agent (default stop-hub behavior)
New-HubShortcut -Name "Stop Grok Hub" -ScriptPath $StopScript `
    -Description "Stop Grok Remote Hub and agent serve"

# Soft restart: hub bounce, keep agent sessions
New-HubShortcut -Name "Restart Grok Hub" -ScriptPath $RestartScript -ScriptArgs "-KeepAgent" `
    -Description "Restart hub only (KeepAgent: multi-turn continuity)"

Write-Host ""
Write-Host "Desktop folder: $Desktop"
Write-Host "Shortcuts: Start Grok Hub, Stop Grok Hub, Restart Grok Hub"
