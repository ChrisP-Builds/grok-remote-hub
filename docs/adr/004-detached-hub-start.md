# ADR-004: Detached hub process start via WMI

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** windows, ops, process, automation

## Context

Starting the hub with PowerShell `Start-Process` from agent/automation shells often left the hub "healthy" during start, then **dead** moments later. Phone Safari showed reconnecting; nothing listened on port 8787. Root cause: the automation harness job object tears down descendant processes when the start command ends.

## Decision

We start the hub with **WMI `Win32_Process.Create`** (detached) from `start-hub.ps1`, record PID in `logs/hub.pid`, and health-poll localhost (and Tailscale IP when present) before reporting success. `stop-hub.ps1` kills the hub PID tree and the hub-owned `grok agent serve` on `127.0.0.1:2419`.

## Alternatives considered

- **Start-Process only:** fails under agent shells; rejected as default.
- **Windows service / NSSM:** more production-grade long term; deferred.
- **Do nothing:** remote unusable after "restart"; rejected.

## Consequences

### Positive
- Hub survives after `start-hub.ps1` exits, including when launched from automation.
- Clear start/stop contract for ops.

### Negative
- WMI start is Windows-specific.
- Must keep stop logic aware of venv launcher parent/child PIDs.

### Neutral
- Optional Task Scheduler logon install remains separate (`install-startup.ps1`).

## Validation

- After `.\start-hub.ps1`, wait, then process still listening and `/health` returns ok without the start shell staying open.
- After `.\stop-hub.ps1`, ports 8787 and hub-owned 2419 are free.

## Related

- ADR-001: session lifecycle
- `start-hub.ps1`, `stop-hub.ps1`
