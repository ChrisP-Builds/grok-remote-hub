# ADR-011: Separate agent process health from ACP connection

- **Status:** Accepted
- **Date:** 2026-07-13
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** ops, acp, reliability, status

## Context

The hub reports a single `agent` field that was `"down"` whenever ACP was disconnected, even if `grok agent serve` was still listening. Operators and the status pill treated that as a dead agent and often reached for full KillAgent restarts. Logs showed long stretches of process-up + ACP-down after hub bounce, no-output heals, and auth/worker churn.

## Decision

1. **Expose two axes** in status/health (and UI):
   - `agentProcess` — process/port up or down
   - `acpConnected` — hub ACP WebSocket connected
   - `agent` — **chat-ready** only when both are up (backward-compatible gate for send)
   - `agentDetail` — `ok` | `acp-disconnected` | `process-down`
2. **Pill copy:** process down → “Agent down”; process up + ACP down → “Agent reconnecting…”; after capped heal failure → “Agent hung — restart”.
3. **Heal:** when process is up and ACP stays down, auto-call existing `AcpClient.reconnect` with capped attempts; do not auto-KillAgent; optional admin reconnect endpoint.

## Alternatives considered

- **Single `agent` field only:** Simple; conflates transport and process and misleads ops.
- **Auto KillAgent on ACP failure:** Often works; destroys healthy agent multi-turn state and is heavier than reconnect.
- **Do nothing:** Keep false “Agent down” and manual restarts.

## Consequences

### Positive

- Operators can tell process death from ACP drop.
- Soft reconnect recovers many KeepAgent hub bounces without killing the agent.
- Chat remains gated on true readiness (`agent === "up"`).

### Negative

- Clients must learn new fields; old clients still only see chat-ready `agent`.
- Heal can briefly block status-loop work during reconnect timeout.

### Neutral

- Multi-process agent pool remains out of scope; one serve + multi-cwd concurrency stays the model.

## Validation

- Unit: `map_agent_status` cases; UI contracts for pill strings.
- Manual: KeepAgent hub restart shows reconnecting then connected; process kill shows Agent down; heal exhaustion shows hung copy.

## Related

- ADR-001 (session lifecycle), ADR-009 (KeepAgent / continuity)
- Commits: `5606ce8`, `f6bcab1`, `e1d2b58`
