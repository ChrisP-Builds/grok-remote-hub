# ADR-014: In-hub agent restart from hung status pill

- **Status:** Accepted
- **Date:** 2026-07-14
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** ops, agent, acp, ui

## Context

After ACP heal exhaustion the pill said **“Agent hung — restart”** with no in-product action. Operators had to run `restart-hub.ps1 -KillAgent` from a shell. Phone/remote users had no path. Auto-reconnect alone cannot fix a wedged `grok agent serve` process that still holds the port.

## Decision

1. **POST `/api/admin/restart-agent`:** force-clear turns, close ACP, **force-kill** the agent listener (including attached/external serve on the configured port), wait for supervisor respawn, reconnect ACP, broadcast status. Hub process stays up.
2. **AgentSupervisor.force_kill_agent / force_restart:** kill pid file and/or port occupant (Windows `taskkill /T /F` when needed). Port-open alone is **not** success; force_restart only verifies TCP.
3. **ACP reconnect after recycle:** after port-up, settle briefly, then multi-attempt `AcpClient.reconnect(..., reset_maintain=True)`. Reset cancels any maintain loop stuck in exponential backoff (failed connects during kill) so backoff restarts at 1s. Success requires `agent_recycle_complete(process_up, acp_connected)` — process up **and** ACP chat-connected. On failure, heal attempts stay at 0 so auto-heal budget remains usable.
4. **UI:** when pill state is `acp-hung` or `agent-down`, the status pill is a clickable control (confirm → restart). Not an automatic restart.

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
- Pure helpers: `agent_recycle_complete`, `agent_recycle_reconnect_plan`.
- `reconnect(reset_maintain=True)` cancels stalled maintain and waits for connected.
- Live: hung health → pill click → acpConnected true.

## Related

- ADR-011, ADR-013
- Scripts: `restart-hub.ps1 -KillAgent`
- Commits: `bbbc2f3`
- Helpers: `hub.harden_policy.agent_recycle_complete` / `agent_recycle_reconnect_plan`
