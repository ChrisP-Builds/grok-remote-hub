# ADR-012: Durable Hub plan-mode approval via plan_mode.json

- **Status:** Accepted
- **Date:** 2026-07-14
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** plan-mode, hub-ui, session-state

## Context

Grok plan mode leaves `plan_mode.json` with `awaiting_plan_approval: true` / `state: Active` until the stock TUI clears the gate (a-key / `exit_plan_mode`). Hub has no TUI, so soft Approve only injected composer text and left the gate set. That blocked implementation and made `exit_plan_mode` fail with client disconnected.

## Decision

1. **Hub owns a disk handshake** for plan approval under the session directory:
   - `POST /api/sessions/{id}/plan/action` with `{"action":"approve"|"request_changes"|"quit"}`
   - Writes `plan_mode.json` (atomic temp + `os.replace`) after merging known fields and preserving unknown keys.
2. **Semantics:**
   - `approve` → `awaiting_plan_approval=false`, `state=Inactive`, then client auto-sends continue inject text.
   - `request_changes` → `awaiting_plan_approval=false`, `state=Active` (agent may revise `plan.md`), inject request-changes prompt for the user to edit/send.
   - `quit` → clear gate, `state=Inactive`, no auto-send.
3. **UI:** View plan modal hard actions + optional “Plan awaiting approval” banner; auto-open modal once per session id when awaiting.
4. **Not** the stock TUI a-key / `exit_plan_mode` RPC. Hub does not call that RPC.

## Alternatives considered

- **Composer inject only:** Already shipped; left gate stuck.
- **Call `exit_plan_mode` over ACP:** Fails without TUI client (“client disconnected”).
- **Edit plan.md from Hub:** Out of scope; agent still owns plan content.

## Consequences

### Positive

- Approve unblocks implementation without a TUI.
- Gate state is durable on disk and re-readable via `GET .../plan`.
- Request changes keeps plan mode active for revisions.

### Negative

- Hub and stock TUI can both write `plan_mode.json`; last writer wins.
- Agents that only watch TUI RPC may not notice Hub disk clears until next session signal.

### Neutral

- `plan.md` remains read-only from Hub; only `plan_mode.json` is written.

## Validation

- Unit: merge actions, apply roundtrip, UI contract for `/plan/action` and inject strings.
- Manual: awaiting plan → Approve → `awaitingApproval` false and continue prompt sent.

## Related

- ADR-001 (session lifecycle), Hub plan viewer Unreleased notes
