# ADR-030: Keep per-session runtime state in one object per session

- **Status:** Accepted
- **Date:** 2026-11-01
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** session, turn, background, compact, status, honesty, refactor

## Context

Turn state, stall watchdogs, end-turn locks, background-activity stamps,
pending user questions, session/load suppress state, the hub-owned /compact
gate and the ops phase and status debounce bookkeeping lived in about twenty
dictionaries and sets keyed by ACP session id, split across `AcpClient` and
`Hub`. Each status fix had to keep every map in step by hand (ADR-015,
ADR-016, ADR-024), and several maps were never emptied.

## Decision

1. **One object.** `hub/session_runtime.py` defines `SessionRuntime`: `turn`
   (the meta dict `active_turns` exposed), `stall_watchdog`, `end_turn_lock`,
   `background` (`BackgroundActivity`: last_at, started_at, kind, rich_at),
   `pending_question_ids`, `load` (`LoadState`: inflight, loading,
   suppress_count, release_handle, suppress_deadline), `warm`, `compact`
   (`CompactState`: gate_until, agent_tokens), `ops_bg_phase` and
   `bg_status_debounce_until`.
2. **One registry.** `Hub` creates a `SessionRuntimes` and passes it to
   `AcpClient`; both read and write the same entries. Ids are stored as given
   and empty ids are refused.
3. **Views, not copies.** `AcpClient.active_turns` and `Hub._hub_compact_gate`
   are `RuntimeFieldMap` views over the registry, so callers, tests and the
   pure gate helpers in `hub/compact.py` keep their dict-shaped API.
4. **Lifecycle.** `is_busy()` is turn, running watchdog, held end-turn lock,
   background stamps, pending question or active load state. `is_live(now)` is
   busy, or a sticky fact (warm, ops phase, stashed compact tokens) or an
   unexpired deadline (compact gate, status debounce). Every code path that
   clears a field calls `prune(sid)`, which drops the entry only when it is not
   live; `prune_expired_bg_activity` also sweeps all entries. The delete
   handler drops the entry outright unless it is busy, in which case the
   session's work is left untouched and reclaimed when it ends.
5. **Watchdog ownership is unchanged.** Watchdogs are cancelled at the same
   call sites as before; a watchdog that exits on its own clears its slot.

Left where they were, deliberately: `_session_cwds`, `acp_created_sessions`,
`hub_owned_session_ids` and `_unresumable_session_ids` are ownership facts
about ids, not runtime status; `_pending` and `_pending_session` are keyed by
JSON-RPC request id; prompt queues, ensure locks and `remote_agent_session`
are keyed by cwd.

## Alternatives considered

- **Typed turn meta:** a dataclass instead of the meta dict would change the
  public `active_turns[sid]["saw_update"]` shape that Hub and tests read.
- **Keep the private maps as compatibility views:** keeps tests untouched but
  leaves two ways to read every field; tests were adapted instead.
- **Do nothing:** every future status fix repeats the manual bookkeeping.

## Consequences

### Positive

- Status honesty questions ("is this session doing anything?") are answered
  by one object, and a session's state cannot be half-cleared.
- End-turn locks, suppress counts and watchdog slots no longer grow for the
  life of the process.

### Negative

- A second `end_turn` arriving after a session's entry was pruned gets a fresh
  lock, so two redundant end-turns can interleave their (idempotent) cancel
  RPCs; before, every end-turn for a session serialised forever.
- Sticky facts (warm, ops phase) keep an entry alive until their own clear
  path runs, exactly as the old maps did; deleting a session reclaims them.

## Validation

- `tests/test_session_runtime.py`: registry semantics, each live field keeps
  an entry, expired deadlines do not, field views, and one invariant per moved
  group (force-clear, end_turn, background expiry, load release, pending
  question, delete handler, debounce and ops phase).
- Existing suites unchanged in intent; structural string checks adapted.

## Related

- ADR-001 turn state machine; ADR-015 cancel-before-clear; ADR-016 load
  suppress; ADR-024 stop clears bg activity; ADR-020 ops metrics
