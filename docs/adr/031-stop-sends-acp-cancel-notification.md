# ADR-031: Stop sends the ACP cancel notification and ends the command the agent waits on

- **Status:** Accepted
- **Date:** 2026-11-01
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** stop, cancel, end_turn, acp, terminal

## Context

Since at least July, Stop never stopped the agent. The hub sent `session/cancel`
as a JSON-RPC request and tried three made-up fallbacks (`session/prompt/cancel`,
`x.ai/session/cancel`, `_x.ai/session/cancel`). Grok rejected all of them, so
the hub force-cleared its own state while the agent kept generating; a live run
streamed for 29 seconds after Stop and finished the turn.

The ACP spec defines `session/cancel` as a notification (no `id`, no reply). The
agent acknowledges by answering the open `session/prompt` with
`stopReason: "cancelled"`.

A second gap showed up live: when Stop lands while the agent waits on
`terminal/wait_for_exit`, Grok does not kill the command, so the turn cannot end
until the command does.

## Decision

1. `notify_agent_cancel` sends the `session/cancel` notification only. The
   fallback methods are gone.
2. `end_turn` then waits up to `cancel_timeout` (2s) for the session's turn to
   end, via a waiter on the session runtime (ADR-030) that resolves whenever the
   turn is removed. `cancelled` now means "the agent ended the turn", not "a
   cancel call returned".
3. After sending the cancel, the hub kills that session's commands that the
   agent is blocked on (open `terminal/wait_for_exit`). Commands nobody is
   waiting on, such as a dev server started in the background, keep running.
4. Force-clear runs only if the turn is still there after the wait.
5. The browser's reset-turn fallback fires after 4s, past the hub's own wait.

Amends ADR-024 decision 1 (cancel mechanics). Its bg-activity and residual
subagent points stand.

## Alternatives considered

- **Keep the request-style cancel with more fallback names:** rejected; none
  exist in Grok, and the spec is explicit.
- **Kill every terminal of the session on Stop:** rejected; it would take down
  background servers the user asked for.
- **Leave terminal handling to the agent:** rejected for now; Grok does not do
  it, and Stop must stop.

## Consequences

- Stop ends the agent's turn in well under a second in live runs, with no
  force-clear and no "agent did not confirm cancel" notice.
- Process kill is the top-level shell only; children a command spawned outside
  PowerShell may survive (same as `terminal/kill` today).
- If Grok later kills awaited commands itself, step 3 becomes a harmless no-op.

## Validation

- `tests/test_acp_cancel_on_clear.py`: notification shape, wait for turn end,
  kill of the awaited command, force-clear when the agent ignores cancel.
- `tests/test_acp_terminal.py`: only awaited commands of the session are killed.
- Live: Stop during text streaming and during `Start-Sleep -Seconds 45`.

## Related

- ADR-015 CLI-aligned turn ownership
- ADR-024 Stop cancels the turn then clears hub bg activity honesty
- ADR-030 per-session runtime state
