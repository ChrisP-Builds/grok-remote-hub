# ADR-005: Queue prompts while a turn is running

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** Project owner + hub implementers
- **Tags:** sessions, acp, ux, queue

## Context

ADR-001 specified one active turn with additional prompts returning a clear **busy** error. That matched early reliability goals but diverged from desktop Grok TUI behavior, where users can type and queue while a turn is in progress. On the phone hub, a disabled composer during long tool runs made multi-step remote control feel broken.

ACP still serializes `session/prompt` (one turn at a time). The product need is not parallel prompts; it is **FIFO queueing** with immediate transcript feedback.

## Decision

We **queue** inbound prompts while `turnRunning` is true:

1. Hub maintains an in-memory FIFO queue (max 10) of `{view_session_id, text, cwd}`.
2. If a turn is active, the WS `prompt` handler enqueues, broadcasts `queued`, and echoes the user message into the transcript; it does **not** reject with busy.
3. When a turn ends, the hub drains the queue: pop → execute next `session/prompt` until empty or another turn is still running.
4. **Stop** cancels the active turn **and clears** the queue.
5. The browser composer stays **enabled** whenever a session is selected and the hub/agent is connected; `turnRunning` only drives Stop visibility and hints, not input lock.

This **amends** ADR-001’s “additional prompts return busy” line; sole-writer and hub-owned session rules still apply.

## Alternatives considered

- **Do nothing (busy reject):** simple, already shipped; rejects TUI-like multi-message remote control.
- **Client-only queue:** fails if the tab reloads mid-turn and does not share queue across devices.
- **True mid-turn inject into the model:** not supported as a first-class ACP path; would require agent protocol changes.

## Consequences

### Positive
- Phone/desktop users can stack follow-ups without waiting for idle.
- Multi-device: queue state lives on the hub; all UIs see echoes/status.

### Negative
- Queued text is committed before the prior turn’s result is known (same as TUI queue risk).
- In-memory queue is lost if the hub process dies mid-queue.

### Neutral
- Max depth 10 needs UX when full (error toast; turn continues).

## Validation

- Unit tests on `hub/prompt_queue.py` (enqueue/pop/full/clear).
- Manual: send while running → `Queued (#n)` toast, message in transcript, auto-run after idle; Stop empties queue.

## Related

- ADR-001: Hub session lifecycle and sole-writer model
- Commits: `44a180c`, `fd5aa0e`
