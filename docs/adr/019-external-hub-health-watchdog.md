# ADR-019: External hub health watchdog (scheduled watch-hub.ps1)

- **Status:** Accepted
- **Date:** 2026-07-26
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

## Decision

1. **External scheduled watchdog** — `watch-hub.ps1` polls `http://127.0.0.1:<bind_port>/health` (port from `config.toml`, default 8787). On **N consecutive failures** (default 2) and after a **cooldown** (default 5 minutes since last restart), invoke `restart-hub.ps1 -KeepAgent`. Log to `logs/watch-hub-YYYYMMDD.log`; persist state in `logs/watch-hub-state.json`. Always exit 0 so Task Scheduler does not spam failure history.

2. **Task Scheduler registration** — `install-startup.ps1` keeps logon task `GrokRemoteHub` and adds `GrokRemoteHubWatch` every 2 minutes. Both run via **`wscript.exe` + `*-hidden.vbs`** (`WScript.Shell.Run …, 0`) so no PowerShell console flashes for end users. Direct `powershell -WindowStyle Hidden` is insufficient under Interactive logon (still steals focus briefly). Same user Interactive Limited, allow on batteries / start when available.

3. **KeepAgent only from watchdog** — never auto-`-KillAgent` from the watchdog. Agent Auth / ACP zombie recovery remains manual or in-hub restart-agent paths (ADR-014).

4. **Client hard recovery** — when WS reconnect attempts exceed a threshold and `/health` has been unreachable for >30s, the SPA does **one** `location.reload()` per page life (`state._didHardReload`). When health returns ok but WS stays down, force `connectWs`. Existing `bootId` change still hard-reloads static once (`grh.bootReload.*`).

5. **Quieter proactor resets** — hub asyncio exception handler demotes `ConnectionResetError` / WinError 10054 (normal client close) to debug.

## Alternatives considered

- **In-process only watchdog:** cannot revive a dead process; rejected as sole mechanism.
- **Windows service / NSSM:** more production-grade; deferred (WMI + Task Scheduler already in use, ADR-004).
- **Watchdog auto-KillAgent:** breaks multi-turn continuity; rejected (ADR-009 KeepAgent default).
- **Aggressive client reload every disconnect:** thrash; rejected (one hard reload + reconnect force).

## Consequences

### Positive

- Hub death between logons is detected within ~4 minutes (2 min poll × 2 fails) and restarted with agent kept.
- Operators need fewer manual `start-hub` / `restart-hub` runs for ordinary crashes.
- UI recovers from prolonged dead-hub without requiring phone CLI.

### Negative

- False-positive health blips can trigger a KeepAgent restart after cooldown (mitigated by MaxFails=2 and 5m cooldown).
- Scheduled task must be installed once per machine (`install-startup.ps1`).

### Neutral

- Agent Auth worker death remains out of scope for auto-restart from this watchdog.
- Does not replace in-hub ACP heal or restart-agent pill.

## Validation

- `.\watch-hub.ps1` while healthy logs `OK` and exits 0.
- `.\install-startup.ps1` registers `GrokRemoteHub` and `GrokRemoteHubWatch`.
- Structural tests: script contains MaxFails / restart-hub / KeepAgent / health; app.js has `_didHardReload` / `location.reload`; `__main__` has ConnectionReset handler.
- Kill hub process → within a few watch cycles health returns ok with new bootId (agent still up if KeepAgent).

## Related

- ADR-004: detached hub start via WMI
- ADR-009: KeepAgent default on hub restart
- ADR-011: agent process vs ACP health
- ADR-014: in-hub agent restart
- `watch-hub.ps1`, `install-startup.ps1`, `restart-hub.ps1`, `start-hub.ps1`
