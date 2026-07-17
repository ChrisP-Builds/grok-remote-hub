# ADR-016: Quiet-period session/load suppress before re-prompt

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** session, acp, load, suppress, reliability, cli-parity

## Context

After `session/load`, the agent often flushes thousands of historical `session/update` frames for seconds after the RPC returns. A fixed short release (e.g. 0.3s) let that flood hit the UI as live stream (scroll thrash, fake compact toasts). No-output heal that force-released suppress in `finally` reintroduced the same flood. Worse, `should_suppress_session_load_fanout` skipped suppress when the session had an **active turn**, so heal re-prompt registered a turn while residual history still arrived: false `saw_update` / TTFB, then mid-turn stall (600s) instead of honest no-output (60s/90s).

## Decision

1. **Quiet-period suppress** after `session/load`: keep the session in `_loading_sessions` until **1.5s silence** between suppressed frames (rearm on each frame), with a **20s max** hold; then release.
2. **While loading, always suppress** historical session/update fanout for that session (except `available_commands_update`), **even if** the session is in `active_turns`. Active-turn bypass applies only when the session is **not** loading.
3. **Heal / re-prompt order:** no-output heal must `forget_warm_session` and perform a real `session/load` (never warm-skip on silence); **must not** force-release suppress in `finally`. After successful load, `await wait_load_suppress_settled`. `session_prompt` waits settle, then releases, then registers the active turn and sends the prompt.
4. Single-flight `session/load` per sid so concurrent attach/ensure does not double-RPC.

## Alternatives considered

- **Fixed short delay only (0.3s):** Rejected — multi-second history flush common on fat sessions; flood returns after release.
- **Active-turn bypass during load (prior behavior):** Rejected — residual history poisons live turn telemetry and stall policy.
- **Force-release suppress in heal finally before re-prompt:** Rejected — cancels quiet period exactly when residual frames arrive.
- **Do nothing:** Rejected — hub unusable on large sessions (scroll thrash, false “working”, false mid-turn stall).

## Consequences

### Positive
- Load residual does not thrash the transcript or invent compact success.
- Re-prompt after heal does not inherit history as first-byte activity.
- Quiet settle is testable (`wait_load_suppress_settled`, suppress policy matrix).

### Negative
- Heal path may wait up to quiet + max hold before re-prompt (bounded; needed for correctness).
- True agent work that arrives only as load residual during suppress is delayed until quiet ends (acceptable; history is not live).

### Neutral
- Model prefill cost of large history after load remains agent-side; hub only stops lying about stream ownership.

## Validation

- Unit: loading + active_turn → suppress True; not loading + active → False.
- Structural: heal has `wait_load_suppress_settled`, no `finally: release_load_suppress`.
- Structural: `session_prompt` order settle → release → register → send.
- Live: fat-session heal smoke (real load, suppress count, settle, silent re-prompt fails no-output not mid-turn).

## Related

- ADR-001: session lifecycle and sole-writer model
- ADR-009: KeepAgent + no-output same-session retry (no fork)
- ADR-015: CLI-aligned turn ownership
- CHANGELOG Unreleased: quiet-period suppress, heal settle, compact vs signals honesty
