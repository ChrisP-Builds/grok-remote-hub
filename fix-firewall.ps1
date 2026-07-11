# Run once in an elevated PowerShell (right-click -> Run as administrator)
# Allows Tailscale / LAN clients to reach the hub on TCP 8787.
$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

$rules = @(
    @{
        Name     = "Grok Remote Hub 8787"
        Params   = @{
            DisplayName = "Grok Remote Hub 8787"
            Direction   = "Inbound"
            Action      = "Allow"
            Protocol    = "TCP"
            LocalPort   = 8787
            Profile     = "Any"
        }
    }
)

if (Test-Path $venvPython) {
    $rules += @{
        Name   = "Grok Remote Hub Python"
        Params = @{
            DisplayName = "Grok Remote Hub Python"
            Direction   = "Inbound"
            Action      = "Allow"
            Program     = (Resolve-Path $venvPython).Path
            Profile     = "Any"
        }
    }
}

foreach ($r in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $r.Params.DisplayName -ErrorAction SilentlyContinue
    if ($existing) {
        Set-NetFirewallRule -DisplayName $r.Params.DisplayName -Enabled True -Action Allow
        Write-Host "Updated: $($r.Params.DisplayName)"
    }
    else {
        New-NetFirewallRule @r.Params | Out-Null
        Write-Host "Created: $($r.Params.DisplayName)"
    }
}

Write-Host ""
Write-Host "Done. From your phone (Tailscale on), open:"
Write-Host "  http://100.110.172.25:8787"
Write-Host "Or MagicDNS:"
Write-Host "  http://r10.taile6a47f.ts.net:8787"
Write-Host ""
Write-Host "Optional HTTPS (better Safari): enable Tailscale Serve at"
Write-Host "  https://login.tailscale.com/f/serve?node=naxhXah3J521CNTRL"
Write-Host "Then on this PC run:"
Write-Host '  & "C:\Program Files\Tailscale\tailscale.exe" serve --bg http://127.0.0.1:8787'
Write-Host "Phone URL becomes: https://r10.taile6a47f.ts.net"
