# ADR-019: External hub health watchdog (long-lived pythonw daemon)

- **Status:** Accepted
- **Date:** 2026-07-26
- **Amendment:** 2026-07-26 — primary mechanism is long-lived `python -m hub.watchdog` via `pythonw` at logon (not every-2-minute PowerShell)
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** windows, ops, reliability, restart, watchdog

## Context

Long-running remote use needs the hub process to stay up without a human re-running CLI every ~20 minutes. Failure modes observed:

1. UI looks dead while hub is still UP (WS ConnectionReset, blank page, stale strip).
2. Hub process can die with no auto-restart until next logon (`install-startup` only runs `start-hub` at logon).
3. Port conflict on manual start when a stale hub process still holds the port.
4. Agent Auth worker deaths (separate; restart-agent only when ACP is zombie; not this ADR).
5. Noisy ERROR logs for normal client disconnects (asyncio ConnectionReset / WinError 10054).

In-process self-heal cannot restart a dead hub process. An external poller that can call `restart-hub.ps1` is required.

The original shipping shape was Task Scheduler every 2 minutes → `watch-hub-hidden.vbs` → `watch-hub.ps1`. That still risked console flash / focus steal under Interactive logon for some PowerShell paths, and added unnecessary process churn.

## Decision

1. **Long-lived silent daemon** — `python -m hub.watchdog` (stdlib only, no aiohttp server import) polls `http://127.0.0.1:<bind_port>/health` (port from `config.toml`, default 8787) every **60s** by default. On **N consecutive failures** (default 2) and after a **cooldown** (default 5 minutes since last restart), invoke `restart-hub.ps1 -KeepAgent` via PowerShell with **CREATE_NO_WINDOW** (`0x08000000`). Log to `logs/watch-hub-YYYYMMDD.log`; persist state in `logs/watch-hub-state.json` (original keys: `consecutiveFails`, `lastRestartAt`, `lastOkAt`, `lastError`; optional ops keys: `mode`, `pid`, `pollIntervalSeconds`, `lastCheckAt`). Single-instance via `logs/watchdog.pid`. Unauthenticated `/health` still returns **`ok: true`** on public liveness when `hub_token` is set (watchdog does not need a token on loopback).

2. **Logon Task Scheduler only** — `install-startup.ps1` keeps logon task `GrokRemoteHub` (`start-hub-hidden.vbs`) and registers `GrokRemoteHubWatch` as **AtLogOn** launching `watchdog-hidden.vbs` → preferred `.venv\Scripts\pythonw.exe -m hub.watchdog` (no console). Not a recurring 2-minute PowerShell task. `start-hub.ps1` also ensures the watchdog is running after hub is healthy (WMI detach; warn-only on failure).

3. **Intentional stop vs restart** — `stop-hub.ps1` kills main hub processes matching `-m hub` **excluding** `hub.watchdog`. On full stop (default / not `-KeepAgent`), it then stops the watchdog so the hub stays down. On `-KeepAgent` (restart path used by the watchdog), the watchdog process is left running so it does not kill itself mid-restart.

4. **One-shot compat** — `python -m hub.watchdog --once` and thin `watch-hub.ps1` wrapper for manual debug; always exit 0.

5. **KeepAgent only from watchdog** — never auto-`-KillAgent` from the watchdog. Agent Auth / ACP zombie recovery remains manual or in-hub restart-agent paths (ADR-014).

6. **Client hard recovery** — when WS reconnect attempts exceed a threshold and `/health` has been unreachable for >30s, the SPA does **one** `location.reload()` per page life (`state._didHardReload`). When health returns ok but WS stays down, force `connectWs`. Existing `bootId` change still hard-reloads static once (`grh.bootReload.*`).

7. **Quieter proactor resets** — hub asyncio exception handler demotes `ConnectionResetError` / WinError 10054 (normal client close) to debug.

## Alternatives considered

- **In-process only watchdog:** cannot revive a dead process; rejected as sole mechanism.
- **Every-2-minute Task Scheduler + PowerShell one-shot** (original ADR): works, but rejected as primary for shipped UX (console flash / focus steal risk and process churn); retained only as optional manual `watch-hub.ps1 --once` wrapper.
- **Windows service / NSSM:** more production-grade; deferred (WMI + Task Scheduler already in use, ADR-004).
- **Watchdog auto-KillAgent:** breaks multi-turn continuity; rejected (ADR-009 KeepAgent default).
- **Aggressive client reload every disconnect:** thrash; rejected (one hard reload + reconnect force).

## Consequences

### Positive

- Hub death between logons is detected within ~2 minutes (60s poll × 2 fails) and restarted with agent kept.
- No periodic PowerShell console; `pythonw` long-lived process is silent.
- Operators need fewer manual `start-hub` / `restart-hub` runs for ordinary crashes.
- UI recovers from prolonged dead-hub without requiring phone CLI.
- Same state file keys so server diagnostics (`watch-hub-state.json`) still parse.

### Negative

- False-positive health blips can trigger a KeepAgent restart after cooldown (mitigated by MaxFails=2 and 5m cooldown).
- Logon task must be installed once per machine (`install-startup.ps1`); `start-hub.ps1` also starts the daemon if missing.
- Intentional full `stop-hub` must stop the watchdog (implemented). Restart uses `-KeepAgent` and keeps the watchdog.

### Neutral

- Agent Auth worker death remains out of scope for auto-restart from this watchdog.
- Does not replace in-hub ACP heal or restart-agent pill.
- Legacy `watch-hub-hidden.vbs` may remain for old installs; new primary is `watchdog-hidden.vbs`.

## Validation

- `python -m hub.watchdog --once` while healthy logs `OK` and exits 0.
- `.\install-startup.ps1` registers `GrokRemoteHub` and `GrokRemoteHubWatch` (AtLogOn → watchdog-hidden.vbs → pythonw).
- Structural tests: `hub/watchdog.py` has daemon / state / KeepAgent / health / CREATE_NO_WINDOW; pure helpers for port/cooldown/state; app.js has `_didHardReload` / `location.reload`; `__main__` has ConnectionReset handler.
- Kill hub process (leave watchdog) → within a few poll cycles health returns ok with new bootId (agent still up if KeepAgent).
- Full `stop-hub.ps1` stops hub and watchdog; `stop-hub.ps1 -KeepAgent` keeps the watchdog.

## Related

- ADR-004: detached hub start via WMI
- ADR-009: KeepAgent default on hub restart
- ADR-011: agent process vs ACP health
- ADR-014: in-hub agent restart
- `hub/watchdog.py`, `watchdog-hidden.vbs`, `install-startup.ps1`, `restart-hub.ps1`, `start-hub.ps1`, `stop-hub.ps1`, `watch-hub.ps1` (one-shot wrapper)
