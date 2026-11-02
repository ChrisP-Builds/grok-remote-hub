# ADR-031: Stop sends the ACP cancel notification and ends the command the agent waits on

- **Status:** Accepted; amended 2026-11-02 (tree kill, wait semantics, hub-ended turns, turn identity)
- **Date:** 2026-11-01
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** stop, cancel, end_turn, acp, terminal, watchdog

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

Correction (2026-11-02): with `wait_for_exit` served off the receive loop
(decision 5), Grok answers the cancel at once even mid-command. The turn stalled
because the hub was not reading the reply. The kill in decision 3 is still what
stops the command itself; Grok left it running.

A correctness review of the first version found four more problems that the
amendment fixes:

- Once the agent honoured a cancel, `session/prompt` returned normally. A
  watchdog end therefore looked like success: no stall message, no no-output
  retry, a "turn done" push for a turn that produced nothing. Worse, the
  prompt task's cleanup cancelled the watchdog task, which was the task running
  `end_turn`, so the bg-activity clear never ran either.
- Killing the top-level process only killed the PowerShell wrapper. `npm test`
  style grandchildren kept running and kept the stdout pipe open, so
  `wait_for_exit` sat at its 120 s cap. `asyncio` `Process.wait()` also waits
  for every pipe to close, so it cannot be used to notice the exit.
- `terminal/wait_for_exit` was awaited inside the ACP receive loop. While one
  session's command ran, no other session's frames were read, and a Stop on
  another session could not see the cancelled reply.
- `end_turn` and the prompt cleanup acted on whichever turn the session held at
  that moment, which could be a follow-up prompt the queue had just started.

## Decision

1. `notify_agent_cancel` sends the `session/cancel` notification only. The
   fallback methods are gone.
2. `end_turn` then waits up to `cancel_timeout` (2s) for the session's turn to
   end, via a waiter on the session runtime (ADR-030) that resolves whenever the
   turn is removed. `cancelled` now means "the agent ended the turn", not "a
   cancel call returned".
3. After sending the cancel, the hub kills that session's commands that the
   agent is blocked on (open `terminal/wait_for_exit`). The kill is the whole
   process tree: `taskkill /PID <pid> /T /F` on Windows, `killpg` of the
   command's own process group on POSIX, then `Process.kill()` as the fallback.
   `terminal/kill` from the agent kills the same way. Commands nobody is
   waiting on, such as a dev server started in the background, keep running.
4. `terminal/wait_for_exit` completes when the process itself has exited (its
   `returncode` is polled every 50 ms, because `Process.wait()` blocks on the
   output pipe) plus up to 1 s for the output pump to drain. A child that
   inherited the pipe and outlives the command no longer pins the reply.
5. Agent requests that block for a command's lifetime (`terminal/wait_for_exit`)
   are served as tasks, like `ask_user_question` already was. Replies correlate
   by request id; the tasks are cancelled when the ACP connection drops.
6. `end_turn` stamps `end_reason` on the turn meta before it cancels. When
   `session_prompt` finishes and its turn carries an `end_reason`, it raises
   `TurnEndedError(reason)` instead of returning the agent's result. The prompt
   path then reports a hub-ended turn exactly as it did when force-clear threw:
   the same user message, the no-output auto-retry, no success push. A Stop or
   admin reset ends idle without an error. The watchdog shields its `end_turn`
   so the prompt task's cleanup cannot abort it halfway.
7. Turn identity. `end_turn` snapshots the turn it means to end and force-clears
   only while that object is still the session's turn; if a newer turn has
   replaced it, it returns `cleared: False` and leaves the newer turn alone.
   `session_prompt` pops only its own turn. `_execute_prompt` never ends a turn
   after `session_prompt` returns or raises, because by then its own turn is
   gone and any turn on the session belongs to a newer prompt. The old
   "prompt exception" and "finally safeguard" end_turns were removed.
8. Force-clear runs only if the turn is still there after the wait.
9. The browser's reset-turn fallback fires after 4s, past the hub's own wait.
   `POST /api/admin/reset-turn` broadcasts "Turn reset by admin" only for
   turns it actually cleared, since the fallback can land as a turn ends.

Amends ADR-024 decision 1 (cancel mechanics). Its bg-activity and residual
subagent points stand.

## Alternatives considered

- **Keep the request-style cancel with more fallback names:** rejected; none
  exist in Grok, and the spec is explicit.
- **Kill every terminal of the session on Stop:** rejected; it would take down
  background servers the user asked for.
- **Leave terminal handling to the agent:** rejected for now; Grok does not do
  it, and Stop must stop.
- **A Windows Job Object for terminals:** more robust than `taskkill /T` for
  re-parented children, but needs ctypes or pywin32; deferred until a live run
  shows a survivor.
- **Decide "hub-ended" from the agent's `stopReason`:** rejected; the hub's
  own record of why it cancelled is authoritative, and a Grok that stopped
  answering `cancelled` would silently reintroduce the silent-watchdog bug.
- **Keep the `is_session_active` safeguards in `_execute_prompt`:** rejected;
  after `session_prompt` has cleaned up its own turn, those calls can only ever
  hit a turn they did not start.

## Consequences

- Stop ends the agent's turn in well under a second in live runs, with no
  force-clear and no "agent did not confirm cancel" notice.
- Stop during `npm test`, `pytest` or any shell-wrapped command ends the test
  run itself, and the agent's `wait_for_exit` returns within about a second.
- Other sessions keep streaming while one session's command runs.
- A turn that completes in the same instant a cancel lands is reported as
  hub-ended and its result is dropped. This matches what force-clear did before
  and is accepted: the watchdog only fires after 60 s of silence, 600 s without
  activity or 1800 s of wall time, so the window is tiny, and for Stop both
  outcomes read as idle.
- A no-output retry that races a newer prompt on the same session now fails
  with "busy" instead of killing the newer prompt.
- `wait_for_exit` returns about 1 s after exit, not at EOF, when a command
  leaves a background child holding the pipe; before it waited the full 120 s.
- If Grok later kills awaited commands itself, step 3 becomes a harmless no-op.

## Validation

- `tests/test_acp_cancel_on_clear.py`: notification shape, wait for turn end,
  kill of the awaited command, force-clear when the agent ignores cancel,
  follow-up turn survives `end_turn`, replacement turn survives prompt cleanup.
- `tests/test_acp_stall_watchdog.py`: a watchdog end the agent honours raises
  `TurnEndedError` with the stall reason, clears bg stamps, force-clears nothing.
- `tests/test_turn_lifecycle.py`: the prompt path retries a no-output end,
  shows the stall message, ends idle for Stop and admin reset, never ends a
  newer turn, and reset-turn reports an error only when it cleared.
- `tests/test_acp_terminal.py`: a shell command's sleeping grandchild dies on
  `kill_awaited`; a command that leaves a child holding the pipe still returns.
- `tests/test_acp_client_requests.py`: `wait_for_exit` leaves the receive loop
  free and still answers with its request id.
- Live: Stop during text streaming and during `Start-Sleep -Seconds 45`;
  Stop during an `npm test` style command and a stalled turn still to be run.

## Related

- ADR-015 CLI-aligned turn ownership
- ADR-024 Stop cancels the turn then clears hub bg activity honesty
- ADR-030 per-session runtime state
