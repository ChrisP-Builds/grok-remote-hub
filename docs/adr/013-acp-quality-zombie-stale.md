# ADR-013: ACP quality beyond process-up and connected boolean

- **Status:** Accepted
- **Date:** 2026-07-14
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** acp, health, status, reliability

## Context

ADR-011 split agent process health from ACP connected. That still allowed **false green**: process up and `acpConnected=true` while the WebSocket was half-open (`Cannot write to closing transport`), heal exhausted, and the UI said Connected until the channel fully flipped. Silent TTFB hangs and mid-turn quiet after tools also looked like “no action.”

## Decision

1. Track ACP **liveness** on the client: last successful send, last recv, consecutive send failures.
2. Map **acpQuality**: `ok | stale | zombie | down` via pure helpers in `hub/status_view.py`.
3. **Chat-ready** `agent: up` only when process is up **and** quality is `ok` (quality-adjusted `acpConnected`).
4. **Zombie:** ≥2 consecutive send failures → force disconnect so maintain/heal can recover.
5. **Stale:** pending RPC with no recv for ~45s → not chat-ready; idle connected with no pending stays ok.
6. Expose `acpQuality` (and ages) on `/health` and WS status; pill labels hung/stale distinctly.
7. Heal uses quality-adjusted connected so zombie/stale can trigger reconnect.

## Alternatives considered

- **Connected boolean only (ADR-011 as-is):** Insufficient for half-open sockets.
- **Immediate KillAgent on any send error:** Too aggressive; transient blips would thrash serve.
- **Client-only timers:** Cannot force server reconnect or honest health for multi-viewer.

## Consequences

### Positive
- Fewer false “Connected” states during transport death.
- Heal and UI can act on zombie/stale without waiting for full disconnect.

### Negative
- More status fields to keep in sync across health/WS/pill.
- Thresholds (2 failures, 45s stale) may need tuning.

### Neutral
- Does not replace mid-turn/no-output stall watchdogs (session_policy).

## Validation

- Unit tests for quality matrix in `tests/test_status_view.py`.
- Live hung state shows non-ok quality and hung pill before KillAgent.

## Related

- ADR-011: Separate agent process health from ACP connection
- Commits: `bbbc2f3` (control plane bundle)

## Notes

In-hub **Restart agent** (POST `/api/admin/restart-agent`) is the user-facing hard recovery when heal exhausts; see ADR-014.

Stale threshold is 90s (must exceed `NO_OUTPUT_SECONDS`); heal skips `stale` while a turn is active so reconnect does not kill live `session/prompt` (stall watchdog owns silent prompts).
