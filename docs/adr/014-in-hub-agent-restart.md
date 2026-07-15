# ADR-014: In-hub agent restart from hung status pill

- **Status:** Accepted
- **Date:** 2026-07-14
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** ops, agent, acp, ui

## Context

After ACP heal exhaustion the pill said **“Agent hung — restart”** with no in-product action. Operators had to run `restart-hub.ps1 -KillAgent` from a shell. Phone/remote users had no path. Auto-reconnect alone cannot fix a wedged `grok agent serve` process that still holds the port.

## Decision

1. **POST `/api/admin/restart-agent`:** force-clear turns, close ACP, **force-kill** the agent listener (including attached/external serve on the configured port), wait for supervisor respawn, reconnect ACP, broadcast status. Hub process stays up.
2. **AgentSupervisor.force_kill_agent / force_restart:** kill pid file and/or port occupant (Windows `taskkill /T /F` when needed).
3. **UI:** when pill state is `acp-hung` or `agent-down`, the status pill is a clickable control (confirm → restart). Not an automatic restart.

## Alternatives considered

- **Docs-only “run KillAgent script”:** Fails remote/phone UX.
- **Auto KillAgent after heal exhaust:** Risky mid-session; user must confirm.
- **Hub process restart only:** Heavier; KeepAgent default would leave the same hung agent.

## Consequences

### Positive
- One-tap hard recovery from the same surface that reports hung.
- Matches ops KillAgent semantics without leaving the UI.

### Negative
- Drops in-flight turns; must confirm.
- Force-kill of port listener can kill a non-hub agent if it shares the port (documented ops risk).

## Validation

- Structural tests for route and pill wiring.
- Live: hung health → pill click → acpConnected true.

## Related

- ADR-011, ADR-013
- Scripts: `restart-hub.ps1 -KillAgent`
- Commits: `bbbc2f3`
