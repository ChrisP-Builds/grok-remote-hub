# ADR-018: Cold ensure vs hot path attribution

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** session, attach, performance, acp, ttfb

## Context

Users experience multi-second silence after open/restart and read it as "disconnected" even when Send is unlocked. Hub work and agent work were conflated:

- **Hub hot path:** process-live ensure (`reuse` + ACP connected) is sub-second when healthy.
- **Hub cold path:** after hub restart, `acp_created_sessions` is empty; hub-owned resume needs `session/load` (and quiet-period suppress, ADR-016) before the first prompt.
- **Agent cold path:** first prompt after load pays model prefill proportional to history size. Hub cannot claim TTFB parity for multi-MB sessions.

Logs already timed ensure on the prompt path; attach responses did not expose timing or action, so clients could not show honest "loading/attaching" vs mute.

## Decision

1. **Process-live reuse is the hub hot path** — `is_live_hot_path(ensure_action="reuse", acp_connected=True)`. Cap for hub-imposed pre-prompt work on that path is documented as `HOT_PATH_MAX_HUB_PRE_PROMPT_S` (warn log only; not a sleep).
2. **session/load after hub restart is expected cold** — not a disconnect. Warm skip (`should_skip_session_load`) and single-flight load/ensure avoid thrash.
3. **Agent prefill is not hub TTFB** — no product claim that fat-session first token is fixed by hub alone.
4. **Attach response honesty:** include `ensureMs` (float ms for ensure), `ensureAction` (`reuse` | `load` | `new`), and `liveHotPath` (bool). Existing `reason` remains the **switch** reason (`hub_session`, `dead_view`, `resume_failed`, `cli_or_foreign_session`, …).
5. KeepAgent default and no no-output session/new unchanged (ADR-009).

## Alternatives considered

- **Client spinner with fixed delay:** Rejected — hides real cold load and invents false "connected mute" fixes.
- **Claim agent TTFB fixed for large histories:** Rejected — agent prefill dominates; out of hub scope.
- **KillAgent on every restart to avoid load:** Rejected — loses process-live continuity (ADR-009).

## Consequences

### Positive

- Clients and operators can distinguish hot attach (~ms) from cold load (seconds) without mistaking cold for disconnect.
- Unit matrix for `is_live_hot_path` stays the source of truth for hot definition.

### Negative

- Attach JSON grows three fields; older clients ignore them.
- Second resolve of "hot" after ensure is not re-run; action is the path taken during ensure.

### Neutral

- Mid-load UI copy remains a client concern; hub only supplies attribution fields.

## Validation

- Unit: `is_live_hot_path` matrix; structural attach response keys (`ensureMs`, `ensureAction`, `liveHotPath`).
- E2E attach when hub is up (optional): hot reuse reports low ensureMs and liveHotPath true when process-live.

## Related

- [009-hub-restart-keep-agent-one-live-cwd.md](009-hub-restart-keep-agent-one-live-cwd.md)
- [016-quiet-period-load-suppress-before-reprompt.md](016-quiet-period-load-suppress-before-reprompt.md)
- [017-unresumable-dead-view-no-blank-fork.md](017-unresumable-dead-view-no-blank-fork.md)
- `hub/session_policy.py`, `hub/server.py` `handle_attach_session`
