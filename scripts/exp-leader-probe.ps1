# Probe grok agent leader IPC surface (experiment only).
# Does NOT kill hub-owned `agent serve` — only leftover leader processes.
$ErrorActionPreference = "Continue"
$Root = "D:\Projects\Grok Remote Hub"
$grok = Join-Path $env:USERPROFILE ".grok\bin\grok.exe"
$log = Join-Path $env:TEMP "grok-leader-exp-probe.log"
$outDir = Join-Path $Root "logs\exp-leader"
$out = Join-Path $outDir "probe-out.txt"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
"" | Set-Content -LiteralPath $out -Encoding utf8

function W([string]$s) {
    Add-Content -LiteralPath $out -Value $s -Encoding utf8
    Write-Host $s
}

if (-not (Test-Path -LiteralPath $grok)) { W "FAIL no grok"; exit 1 }

W ("ts={0}" -f (Get-Date -Format o))
W ("grok={0}" -f $grok)

# Stop leftover LEADER only — never kill serve (running hub uses serve).
Get-Process -Name grok -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        $c = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        if ($c -and ($c -match 'leader') -and ($c -notmatch 'serve')) {
            W "stop leader grok pid=$($_.Id)"
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        } elseif ($c) {
            W ("keep grok pid={0} cmd_snip={1}" -f $_.Id, ($c.Substring(0, [Math]::Min(120, $c.Length))))
        }
    } catch {}
}
Start-Sleep -Seconds 2
if (Test-Path $log) { Remove-Item $log -Force }

$p = Start-Process -FilePath $grok -ArgumentList @(
    "agent", "--always-approve", "leader",
    "--no-exit-on-disconnect", "--relay-on-demand",
    "--debug-file", $log
) -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 5
W ("leader_pid={0} exited={1}" -f $p.Id, $p.HasExited)

if (Test-Path $log) {
    W "=== log hits ==="
    Select-String -Path $log -Pattern "socket|IPC|pipe|leader|listen|path|ready|ACP|forward" -CaseSensitive:$false |
        Select-Object -Last 50 | ForEach-Object { W $_.Line }
    W "=== log tail ==="
    Get-Content $log -Tail 30 | ForEach-Object { W $_ }
} else {
    W "WARN no debug log at $log"
}

W "=== .grok leader-ish files ==="
Get-ChildItem (Join-Path $env:USERPROFILE ".grok") -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "leader|sock|pipe|ipc" } |
    ForEach-Object {
        $content = ""
        try {
            $raw = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
            if ($null -ne $raw) {
                $content = ([string]$raw -replace "\s+", " ").Trim()
            }
        } catch {}
        W ("{0} len={1} content={2}" -f $_.Name, $_.Length, $content)
    }

W "=== named pipes grok/leader ==="
try {
    [System.IO.Directory]::GetFiles("\\.\pipe\") |
        Where-Object { $_ -match "grok|leader|xai" } |
        Select-Object -First 20 |
        ForEach-Object { W $_ }
} catch { W ("pipe enum: {0}" -f $_.Exception.Message) }

W "=== leader list ==="
& $grok leader list 2>&1 | ForEach-Object { W "$_" }

# Can stdio --leader attach? NDJSON JSON-RPC initialize.
W "=== test stdio --leader initialize (5s) ==="
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $grok
$psi.Arguments = "agent --always-approve --leader stdio"
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$client = [System.Diagnostics.Process]::Start($psi)
$init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":true,"writeTextFile":true},"terminal":true},"clientInfo":{"name":"hub-exp","version":"0.0.1"}}}' + "`n"
$client.StandardInput.Write($init)
$client.StandardInput.Flush()
Start-Sleep -Seconds 3
if (-not $client.HasExited) {
    $outTask = $client.StandardOutput.ReadLineAsync()
    if ($outTask.Wait(5000)) {
        $line = $outTask.Result
        W ("stdio_line_len={0}" -f $(if ($null -eq $line) { 0 } else { $line.Length }))
        W ("stdio_line={0}" -f $line)
        if ($line -match '"result"' -and $line -match 'protocolVersion') {
            W "stdio_initialize=OK (NDJSON JSON-RPC)"
        } else {
            W "stdio_initialize=UNEXPECTED_SHAPE"
        }
    } else {
        W "stdio_line=timeout (no line yet)"
        W "stdio_initialize=FAIL"
    }
    try { $client.Kill() } catch {}
} else {
    $err = $client.StandardError.ReadToEnd()
    $so = $client.StandardOutput.ReadToEnd()
    W ("stdio exited code={0}" -f $client.ExitCode)
    W ("stdout={0}" -f $so)
    W ("stderr={0}" -f $err)
    W "stdio_initialize=FAIL"
}

# Cleanup leader only (leave serve alone)
W "=== cleanup leader ==="
if (-not $p.HasExited) {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    W ("stopped leader_pid={0}" -f $p.Id)
}
& $grok leader kill 2>&1 | ForEach-Object { W "$_" }
Start-Sleep -Seconds 1
# Confirm serve still up if it was
Get-Process -Name grok -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        $c = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        W ("post_cleanup grok pid={0} cmd_snip={1}" -f $_.Id, $(if ($c) { $c.Substring(0, [Math]::Min(100, $c.Length)) } else { "?" }))
    } catch {}
}
W "PROBE_DONE"
W ("wrote {0}" -f $out)
