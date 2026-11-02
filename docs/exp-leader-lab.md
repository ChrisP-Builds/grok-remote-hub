# Experiment lab: leader multi-client (opt-in)

**Status:** Experimental. Default hub path is still `agent serve`.  
**Do not** enable for normal production use unless you accept shared outage with CLI `--leader` clients.

See also: [ADR-021](adr/021-leader-multi-client-experiment.md), `logs/exp-leader/FINDINGS.md`.

## Prerequisites

- Windows (named pipe IPC).
- `grok` at `%USERPROFILE%\.grok\bin\grok.exe` (or configured `hub.grok_bin`).
- Stop or accept that **leader mode does not use** `agent serve` on `:2419`.

## Enable

In `config.toml` (copy from `config.example.toml` if needed):

```toml
[agent]
bind = "127.0.0.1"
port = 2419
# EXPERIMENT only — shared leader backend
backend = "leader"
```

Restart the hub so config reloads:

```powershell
.\restart-hub.ps1 -KillAgent
```

Confirm:

```powershell
# Authenticated health should include agentBackend
# (add ?token=... if hub_token is set)
Invoke-RestMethod "http://127.0.0.1:8787/health"
```

Expect `"agentBackend": "leader"` when authenticated / full health body.

## Start order (recommended)

1. **Hub with `backend = "leader"`** — supervisor spawns leader if none is up, then
   opens ACP via `grok agent --always-approve --leader stdio`.
2. **Desktop CLI on the same leader:**

   ```powershell
   grok --leader
   ```

   Or attach only the agent stdio client (debug):

   ```powershell
   grok agent --always-approve --leader stdio
   ```

3. Open hub UI; start or resume a session; send a prompt.
4. In CLI `--leader`, list/resume the same session if the product exposes it;
   confirm both sides see coherent history / live updates (manual check).

## Probe only (no hub config change)

Leaves running `agent serve` alone:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\exp-leader-probe.ps1
```

Output: `logs\exp-leader\probe-out.txt`.

## Disable (back to default)

```toml
[agent]
# backend = "serve"   # or omit entirely
```

```powershell
.\restart-hub.ps1 -KillAgent
# optional cleanup if a leader was left running:
grok leader kill
```

## Safety notes

- **Shared outage:** killing leader drops hub ACP and any `grok --leader` clients.
- **Default serve** keeps normal CLI (without `--leader`) independent of hub.
- Force restart in hub uses `grok leader kill` + pid tree when backend is leader.
- Do not commit `config.toml` with `backend = "leader"` as a team default.
