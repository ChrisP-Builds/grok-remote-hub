# ADR-029: Sequence session events and resume reconnects from an in-memory log

- **Status:** Accepted (amended 2026-11-02)
- **Date:** 2026-11-01
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** streaming, websocket, reconnect, mobile, reliability

## Context

Events reach browsers through `Hub.broadcast`; nothing carries a sequence
number, and a socket that drops misses whatever was broadcast meanwhile. The
client compensates by reloading full history (capped at 800 merged messages)
from about fifteen places: every subscribe, `resumeAfterReconnect`, the wake
handler, turn idle, a 4 s poll. On a phone that wakes often this means
re-rendering whole transcripts, losing in-progress tool rows mid-turn, and
flicker. Agent events carry a stable `_meta.eventId`, which `EventDedupe`
already uses to merge the live ACP and disk-tail copies.

## Decision

1. **Sequence.** One hub-wide counter, reset per hub boot. `broadcast()`
   stamps `seq` on every session-scoped payload (acp, turn, system,
   terminal_out, compact, usage, commands) sent with a `session_id`. Hub-built
   user echoes are sent scoped so they are stamped too.
2. **Log.** `hub/event_log.py` keeps, per session, the stamped payloads in
   order: at most 4000 events or 4 MB per session and 64 sessions (least
   recently used evicted). It records the highest evicted seq per session so
   gaps are detectable.
3. **Resume.** `subscribe` may carry `sinceSeq` and `bootId`. If the boot
   matches and the log still holds every event for that session after
   `sinceSeq`, the server sends `{"type":"replay","sessionId","fromSeq",
   "toSeq","count"}` followed by those payloads unchanged, and no history.
   Otherwise it sends the history snapshot as today, now with `seq` = the
   log head captured before reading disk.
4. **Client.** Keeps `lastSeq` per session; drops any stamped event with
   `seq <= lastSeq` (exact dedupe); a history snapshot raises `lastSeq` to its
   `seq`. On reconnect within the same boot it subscribes with `sinceSeq`. A
   session that resumed by replay skips the forced history reloads of
   `resumeAfterReconnect` and the wake handler. The turn-idle reload and the
   idle poll stay: they cover stream-merge errors, a separate concern.
5. **Observability.** Ops counters `ws_resume_replay` and
   `ws_resume_fallback` (with reason: boot, gap, unknown).

## Alternatives considered

- **Replay from disk (`updates.jsonl` offsets):** disk lags the live stream
  and hub-built messages are not on disk.
- **Per-session counters:** equivalent; one counter is simpler and orders
  events across sessions.
- **Persist the log:** a hub restart already forces a page reload; memory is
  enough.

## Consequences

### Positive

- Phone wake and network blips catch up in place: no transcript rebuild, no
  lost tool rows, exact dedupe instead of time and prefix heuristics.

### Negative

- Memory: bounded by the caps above.
- First open of a session still uses the snapshot, with its existing races
  (events interleaving with the snapshot, first-watch tail gap).

## Validation

- Unit: log append, eviction and gap detection; resume decision.
- Live: drop a client's network mid-turn, restore it; the client receives a
  `replay`, no `history`, and ends with the same transcript as a fresh load.

## Related

- ADR-001 session lifecycle; ADR-016 quiet-period load suppress; ADR-020 ops metrics

## Amendment (2026-11-02): dedupe by membership, ignore replays after a redraw

Review found two ways the client lost or doubled events:

1. The hub sends to each viewer in turn, so with two viewers a later event
   can reach one of them first; `seq <= lastSeq` then dropped the earlier
   one. Decision 4 now dedupes by a bounded set of recently seen seqs (512)
   plus a floor raised by snapshots. `lastSeq` stays the highest seq seen and
   is still what resume sends.
2. A replay that arrived after the 1.5 s wait landed on top of a pane already
   redrawn from disk. A replay is now dropped when its pane was wiped and
   redrawn after the request; it is still applied when the disk reload found
   nothing new and left the pane alone.

The replayed mark is consumed by one wake instead of lasting for the socket's
life. Validation: `tests/test_ui_client_races.py` (runs the real functions in
node).
