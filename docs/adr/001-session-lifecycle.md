# ADR 001: Hub session lifecycle and sole-writer model

**Status:** Accepted  
**Date:** 2026-07-10  

## Context

Safari/hub turns often showed `running` then died with no stream. Investigation proved:

1. `session/new` + `session/prompt` on the hub agent streams reliably.
2. `session/load` of a desktop TUI (foreign) session + `session/prompt` often emits **zero** ACP updates and hangs until force-clear.
3. Dual clients on one ACP stream do not both receive updates reliably.
4. Disk `updates.jsonl` is shared history; live agent process is not.

Users need seamless multi-turn chat on phone + desktop hub, not TUI parity.

## Decision

### 1. Sole writer: hub agent process only for live remote turns

- Live prompts never use `session/load` of foreign/CLI session ids.
- Live prompts only use hub-created sessions (`session/new` in this process, tracked in `acp_created_sessions`).
- Per-project (`cwd`) at most one **active remote session** reused across multi-turn chat.

### 2. Attach-on-open (seamless), not switch-on-first-send

When the client opens a session for a project:

1. Load **disk history** for the selected id (context / catch-up).
2. Ensure a **live remote session** for that cwd (reuse or `session/new`).
3. If live id differs from viewed id, emit `session_switch` immediately with clear reason.
4. Subscribe and tail the **live** session for streaming.
5. All subsequent prompts target the live session only.

Opening history of a TUI session becomes: "see past work, then continue live on a hub session for the same project."

### 3. Turn state machine

```
idle -> ensuring_session -> running -> idle
                \-> error -> idle
running -> stalled (no ACP update N seconds) -> recover -> idle|running
```

- Broadcast `turn` running/idle on every path (success, error, stall, disconnect).
- Client never stays in `running` without a server idle/error within timeout.
- Prompt queue: one active turn; additional prompts return clear busy + idle unlock (no silent drop).

### 4. Recovery

| Failure | Recovery |
|---|---|
| No ACP updates after prompt start (60s) | Force-clear turn; error UI; next send may create fresh remote session |
| Mid-turn stall (600s since last ACP activity) | Force-clear; idle broadcast; user resends (see TUI-aligned amendment) |
| Max turn duration (1800s) | Force-clear; idle broadcast |
| Stuck before new prompt | Force-clear only if watchdog would (mid-stall / max wall / no-output) |
| ACP disconnect mid-turn | Clear turn; reconnect agent; `status` down/up; user resends |
| `session/new` fails | Error + idle; no fake running |

### 5. Observability

- Health: `turnRunning`, `turnAgeSeconds`, `hubVersion`, `cliVersion`, `compatOk`, `loadedSessionId`
- Logs: prompt start/end, session ensure/switch, force-clear reason
- `logs/last-remote-session.txt` for desktop follow

### 6. Explicit non-goals

- Driving the stock Grok TUI live from the hub
- Pretending foreign session ids are live-promptable
- Full TUI feature parity in the browser

## Consequences

- Multi-turn remote chat is reliable on hub-owned sessions.
- Desktop TUI and hub remote threads for the same project may diverge (by design).
- Users must be told via banner when live remote != viewed history id.
- After CLI upgrades, structural compat checks still apply; live path remains hub-owned.

## Amendment (2026-07-10): non-blocking WebSocket prompt handler

Long `session/prompt` must **not** run inline in the aiohttp WS receive loop. Doing so starves client ping/pong and produces `keepalive ping timeout` mid multi-turn (observed: turn 1 OK, turn 2 connection closed).

**Rule:** dispatch `prompt` and `cancel` via `asyncio.create_task`; keep receive loop free. ACP serializes turns with its own lock.

## Amendment (2026-07-10): continuous mid-turn stall watchdog

### Problem

The original no-output watchdog returned permanently after the first ACP `session/update`:

```python
if self.turn_saw_update:
    return  # stopped watching forever
```

Observed failure: tools/thinking streamed, then agent hung mid-turn. Hub kept `turnRunning=true` until the 600s `session/prompt` request timeout. Client independently unlocked at 90s with no server notify → UI idle, server busy (desync).

### Decision

| Threshold | Meaning |
|---|---|
| `NO_OUTPUT_SECONDS` (60s) | Zero ACP updates after prompt start → force-clear |
| `MID_TURN_STALL_SECONDS` (600s) | No ACP activity since last update → force-clear |
| `MAX_TURN_SECONDS` (1800s) | Hard wall even with activity → force-clear |
| `STUCK_TURN_SECONDS` (1800s) | Documented wall; new-prompt stuck is activity-aware |

- Continuous stall watchdog runs for the whole turn (never exits after first update).
- Pure policy: `should_force_clear_turn(saw_update, age_since_start, age_since_activity) -> reason|None`.
- New-prompt stuck: `is_turn_stuck_for_new_prompt` ≡ `should_force_clear_turn is not None` (no short healthy kill).
- Force-clear records `last_force_clear_reason` / `last_force_clear_session`; fails pending ACP futures so `session_prompt` exits and Hub broadcasts idle.
- Hub re-broadcasts `status` every 10s while `turnRunning` so clients re-sync.
- Client treats **server `status.turnRunning` as source of truth**; soft warn at 120s quiet only (never auto reset-turn); if status says running while client idle, re-lock and toast.

### Recovery table (updated)

| Failure | Recovery |
|---|---|
| No ACP updates after prompt start (60s) | Force-clear; may prepare fresh remote session |
| Mid-turn stall (600s since last ACP activity) | Force-clear; idle broadcast; user resends |
| Max turn duration (1800s) | Force-clear; idle broadcast |
| Stuck before new prompt | Same as watchdog force-clear (activity-aware), not a short healthy wall |
| Client quiet stream | Soft warn at 120s only; **never** auto reset-turn / unlock |
| ACP disconnect mid-turn | Clear turn; reconnect agent; `status` down/up; user resends |

## Amendment (2026-07-10): TUI-aligned turn timeouts

### Problem

Aggressive hub timeouts (25s no-output, 90s mid-stall, 120s stuck, client 90s unlock)
killed healthy long agentic turns that the desktop TUI keeps open for many minutes
(tools, thinking, multi-step work). Client auto `POST /api/admin/reset-turn` at 90s
also desynced UI from a still-running server turn.

### Decision

| Threshold | Value | Meaning |
|---|---|---|
| `NO_OUTPUT_SECONDS` | 60s | Zero ACP updates after prompt accepted → force-clear |
| `MID_TURN_STALL_SECONDS` | 600s (10 min) | Silence after activity → force-clear (tools can be quiet) |
| `MAX_TURN_SECONDS` | 1800s (30 min) | Hard wall; `session/prompt` request timeout matches |
| `STUCK_TURN_SECONDS` | 1800s | Documented wall; new-prompt clear uses activity policy |
| New-prompt stuck | `is_turn_stuck_for_new_prompt` ≡ `should_force_clear_turn is not None` | No short healthy-activity kill |
| `CLIENT_STALL_WARN_SECONDS` | 120s | Soft toast only: "Still working… Use Stop to cancel." |
| `CLIENT_STALL_UNLOCK_SECONDS` | 0 (disabled) | **Never** auto reset-turn or unlock from client |

- Continuous stall watchdog remains; thresholds only lengthened.
- Status rebroadcast every 10s while running remains.
- Client: turn strip copy is `running` / `idle`; `data-state=stalled` is visual quiet cue only.
- Only user Stop/Cancel or server idle/error ends the client turn lock.

## Amendment (2026-07-10): full ACP client surface for advertised capabilities

### Problem

Hub `initialize` advertises:

```json
"clientCapabilities": {
  "fs": {"readTextFile": true, "writeTextFile": true},
  "terminal": true
}
```

The agent then issues **client** JSON-RPC requests during a prompt turn:

- `session/request_permission` (options like `proceed_once`, `proceed_always_tool`, kinds `allow_always` / `allow_once`)
- `fs/read_text_file`, `fs/write_text_file`
- `terminal/create`, `terminal/output`, `terminal/wait_for_exit`, `terminal/kill`, `terminal/release`

The hub previously only auto-replied to permission with a hardcoded `optionId: "allow-always"` (often not in the offered list) and did **not** implement fs/terminal handlers. Unanswered requests hang the agent tool forever → UI stuck on `running` (turns aged 500s+). Agent logs also show workers dying with `Auth(AuthorizationRequired)` when permission/transport fails.

### Decision

1. **Hub must implement the full ACP client surface it advertises** (fs + terminal + permission). Never advertise a capability without a request handler that always replies (result or JSON-RPC error).
2. **Permission auto-approve** via pure `pick_permission_option(options)`:
   - Prefer `kind == allow_always` or optionId containing `always` / `proceed_always`
   - Else `allow_once` / `proceed_once`
   - Else first non-cancel/reject option
   - Respond: `{"outcome":{"outcome":"selected","optionId":"..."}}`
3. **Client request dispatch** in `_handle_raw`: any message with `method` + `id` and no `result`/`error` is an agent→client request; handle and reply before fanout. Unknown methods return error `-32801` so the agent cannot hang forever.
4. Terminals are subprocess-backed with capped output (~1MB) and a wait-for-exit cap (120s).
