# ADR-015: CLI-aligned turn ownership (cancel frees agent; server is idle authority)

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** turn, cancel, session, acp, ui, cli-parity

## Context

Hub and CLI must share the same mental model for a turn: **one owner**, interrupt cancels the agent, idle means you can send, and reconnect re-syncs rather than leaving a stuck "working" strip. Prior work already cancels the agent on stop, stall force-clear, and admin reset-turn (`notify_agent_cancel` / `session/cancel`). Gaps remained after **sleep/wake**, **network online**, and **WS soft reconnect**: the client could still show turn-running while the server was idle, or leave a dead turn (stale/zombie ACP, long silence) without a CLI-style interrupt.

## Decision

1. **Cancel frees the agent whenever hub ends a turn** — stop, stall force-clear, admin reset-turn, no-output recovery, and wake dead-turn clear all cancel first (never unlock-only orphan turns).
2. **Server is sole authority for `turnRunning`** — client must not keep Send/strip locked as running when `/health` or status WS report idle (`turnRunning=false`, empty `liveTurns`).
3. **Wake / reconnect re-sync** — on `visibilitychange` (visible), `pageshow` (bfcache), `window` `online`, and after successful WS open reconnect path, call `reconcileTurnAfterWake` (debounced):
   - Server idle → `setTurnRunning(false)` / `clearStaleLiveTurns` if client still running.
   - Server running **and** (`acpQuality` in `stale|zombie|down` **or** (`turnSilenceSeconds` >= 120 **and** no open tools/plan)) → same path as Stop (WS cancel + `reset-turn`); toast: "Turn cleared after reconnect (CLI-style interrupt)."
   - Healthy running turn (including quiet mid-tool with quality ok) → re-seed timers via `applyServerTurnTimers`; unlock only if not actually running.
4. After reconcile: `setComposerEnabled(composerConnected())` and `updateTurnStrip`.
5. Pure policy helper `should_clear_turn_on_wake` (Python + JS mirror) encodes the dead-turn clear matrix for tests. **`has_open_tools`:** when true and quality is ok/empty, silence alone does not clear (stale/zombie/down still clear).

## Alternatives considered

- **Client-only timers for unlock after wake:** Rejected — multi-viewer and process restarts make server status the source of truth; client clocks desync on sleep.
- **Force-clear without `session/cancel`:** Rejected — orphans agent turns; next prompt stays blocked (ADR path already fixed for stall/reset; wake must not reintroduce it).
- **Always cancel any running turn on visibility:** Rejected — healthy mid-turn work (tools, long agentic) must survive tab focus and brief offline.

## Consequences

### Positive
- CLI parity: interrupt/end always cancels agent; idle is honest after wake.
- Fewer stuck Send/stop UI states after laptop sleep or Tailscale blips.
- Testable pure policy for wake clear rules.

### Negative
- Extra `/health` fetch on wake/online/reconnect (cheap; in-flight coalesced).
- Silence threshold (120s) still clears a quiet turn when quality is ok **and** the selected pane shows no open tools/plan; open-tool detection is client-DOM residual counts (best-effort after hard refresh).

### Neutral
- Does not fix heavy-session slowness or Auth worker death (still needs restart-agent).

## Validation

- Unit tests: `should_clear_turn_on_wake` matrix in `tests/test_session_policy.py`.
- Structural: `static/app.js` defines `reconcileTurnAfterWake` / `shouldClearTurnOnWake`; visibilitychange and online wire them.

## Related

- ADR-001: Hub session lifecycle and sole-writer model
- ADR-009: KeepAgent default + view-first continuity
- ADR-013: ACP quality beyond process-up and connected boolean
- ADR-014: In-hub agent restart from hung status pill
