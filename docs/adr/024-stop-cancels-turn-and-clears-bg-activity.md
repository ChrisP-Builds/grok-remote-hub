# ADR-024: Stop cancels the turn then clears hub bg activity honesty

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** stop, cancel, end_turn, background, subagent, honesty

## Context

Remote users press Stop expecting the phone UI to stop “working.” ACP
`session/cancel` targets the **parent** `sessionId`. Nested/background subagent
work may continue on the agent if the protocol does not cascade cancel. Hub
also tracked `subagent_progress` / bg stamps that could keep Working/Stuck UI
after local unlock.

Product why: thin remote plane must not lie about work after Stop; must not
KillAgent on Stop (would break multi-turn continuity).

## Decision

1. **Stop / reset-turn / stall paths use `end_turn`:** await `notify_agent_cancel`
   (bounded) then `force_clear_turn` (cancel-before-clear; ADR-015).
2. **After clear, drop hub bg activity stamps** for that session
   (`_pop_bg_activity`) so Working/Stuck does not outlive the stopped turn.
   All-sessions clear uses `clear_bg_activity()`.
3. **WS cancel** rebroadcasts idle + full `status_payload` after `end_turn` so
   clients refresh bg fields. Soft client message only when cancel failed but
   local clear succeeded (unlock still happens).
4. **Residual (explicit):** cancel is parent session only. Nested subagent
   process work ends only if the agent honors cancel cascade. Hub must not
   claim “all subagents killed” without ACP evidence. Do not KillAgent on Stop.

## Alternatives considered

- **Stop = KillAgent:** rejected — too heavy; multi-turn / KeepAgent product.
- **Cancel every known child session id:** no reliable id graph in hub today;
  deferred.
- **Leave bg stamps after stop:** rejected — false Working after unlock.

## Consequences

### Positive

- Stop is honest at hub UI layer for parent turn + bg stamps.
- Aligns with CLI intent that stop ends the active turn’s work.

### Negative

- Residual agent-side nested work may still run briefly after parent cancel.

### Neutral

- Tests drive real `end_turn` bg clear (`tests/test_stop_bg_honesty.py`).

## Validation

- `end_turn` clears `_bg_activity_*` for the session under test.
- Cancel-before-clear order still covered by `tests/test_acp_cancel_on_clear.py`.

## Related

- ADR-015 CLI-aligned turn ownership
- ADR-009 no no-output session/new
- ADR-014 restart-agent (separate from Stop)
