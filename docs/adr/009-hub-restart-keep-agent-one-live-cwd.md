# ADR 009: KeepAgent default on hub restart + view-first continuity, one live per cwd

- **Status:** Accepted (amended 2026-07-12: view over byCwd; no-output must not session/new)
- **Date:** 2026-07-12
- **Tags:** session, resume, ops, restart

## Context

After hub process restart, multi-turn continuity depends on (1) the agent process still holding sessions and (2) ensure policy picking the session the user was working on.

An earlier byCwd-first order rewrote the project map away from the open conversation on map drift or no-output recovery (`session/new` + `session_switch` reason `no_output_retry`). That forked continuity (e.g. live hub session A → accidental new session B for the same cwd).

Ops: `restart-hub.ps1` defaults to KeepAgent so agent serve survives hub bounce.

## Decision

### 1. Hub-owned view first; byCwd fallback; one live per cwd via map update

`resolve_ensure_action` preference order:

1. view process-live → `reuse` / `hub_session` (caller records as byCwd on use)
2. view hub resume candidate (origin / hubIds / map) → `load` / `resume_view`
3. `remote_by_cwd[cwd]` process-live → `reuse` / `reuse_cwd`
4. `remote_by_cwd[cwd]` resume candidate → `load` / `resume_cwd`
5. else → `new` / `need_session_new`

Rationale: the session the user has open is the continuity source of truth after restart. byCwd is fallback when view is foreign/CLI or empty. One-live-per-cwd is preserved by updating byCwd when view is reused/loaded (`_record_hub_session`).

### 2. No-output must not fork

On zero ACP output after prompt: force-clear the turn on the **same** session id; do **not** pop byCwd, do **not** `session/new`, do **not** broadcast `session_switch` for `no_output_retry`. User message: same session kept — send again.

### 3. Load failure: retry before abandoning continuity

On `session/load` failure: retry once (~0.5s). If byCwd load fails while view is a different hub resume candidate, try load view before `session/new`. Log loudly when falling back to new.

### 4. KeepAgent default on hub restart

- `restart-hub.ps1` defaults to `-KeepAgent` into `stop-hub.ps1`.
- Opt-out: `-KillAgent` for full teardown (hub + agent).

## Alternatives considered

- **byCwd-first always:** one map id per cwd but pulls the user off the conversation they opened when map drifts (proven bad UX).
- **Kill agent on every restart:** loses multi-turn continuity on hub bounce.
- **no-output session/new:** looked like recovery; actually forked history and rewrote the project map.

## Consequences

### Positive

- Opening a hub-owned conversation continues on that id after restart/map drift.
- no-output no longer abandons multi-turn history.
- KeepAgent preserves agent sessions across hub-only restarts.
- byCwd still used when view is empty or foreign; map updates keep one live per cwd.

### Negative

- Two process-live ids for the same cwd can exist briefly if agent held both; ensure prefers the open view and rebinds byCwd to it.
- KeepAgent means a hung agent is not cleared by plain restart; operators use `-KillAgent`.

## Validation

- Unit: `tests/test_session_policy.py` (view preferred over byCwd; foreign view → byCwd; empty view → byCwd; hubIds → `resume_view`).
- Ops strings: `tests/test_ui_ux.py` asserts `KeepAgent` / `KillAgent` in `restart-hub.ps1`.

## Related

- [001-session-lifecycle.md](001-session-lifecycle.md) (lifecycle, resume amendments)
- [003-dual-hub-topology.md](003-dual-hub-topology.md) (one live session per project cwd)
- `hub/session_policy.py`, `hub/server.py`, `restart-hub.ps1`, `stop-hub.ps1`
