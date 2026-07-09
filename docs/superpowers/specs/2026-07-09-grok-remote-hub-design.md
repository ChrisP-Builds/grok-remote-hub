# Grok Remote Hub — Design Spec

**Date:** 2026-07-09  
**Status:** Draft for user review  
**Goal:** Claude-like remote control for Grok Build: phone + desktop web UI, shared live session stream, resume any local project session, slash-command palette, always-on on the PC via Tailscale.

---

## 1. Problem

Grok Build has no product “Remote Control” like Claude Code. Users need to:

- Drive agents from a phone browser while the PC is the execution host  
- See the same live responses/tool status on desktop and phone  
- Resume existing project sessions (not one project only)  
- Use slash commands and expanding UI affordances, not a bare text box  

Spike (2026-07-09) proved:

- `grok agent serve` + ACP over WebSocket works  
- `session/load` / `session/new` / `session/prompt` work  
- Dual native ACP clients do **not** both receive `session/update` (last load owns the stream)  
- Therefore the hub must own **one** ACP connection and fan out to all UIs  

---

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| Access | Tailscale only |
| Availability | Always-on on PC |
| Architecture | Single hub process + local `grok agent serve` |
| UI | Responsive web shell (desktop session rail, mobile sessions sheet) |
| Slash commands | Agent-driven via ACP `available_commands_update` |
| Port | `8787` |
| Install path | `D:\Projects\Grok Remote Hub` |
| Desktop surface | Same web UI as phone (not stock Grok TUI mirror) |

---

## 3. Architecture

```
Phone / Desktop browser
        │
        │  http(s)://100.110.172.25:8787   (Tailscale IP only)
        ▼
┌─────────────────────────────────────────────┐
│              Grok Remote Hub                │
│  Static UI  │  Hub WS/SSE  │  REST API      │
│             │  fan-out     │  sessions      │
│             └──────┬───────┘                │
│                    │ single ACP client      │
│                    ▼                        │
│         grok agent serve (127.0.0.1:2419)   │
└─────────────────────────────────────────────┘
                    │
                    ▼
         ~/.grok/sessions/<cwd>/<id>/
```

### Processes

1. **Hub** (long-lived): HTTP server + UI client WebSocket + ACP bridge  
2. **Agent child**: spawned/managed by hub (`grok agent serve --bind 127.0.0.1:2419 --secret …`)  
3. Restart agent if it dies; reconnect ACP; notify UIs  

### Network binding

- Hub binds **only** to the machine’s Tailscale IPv4 (discover via `tailscale ip -4`, fallback config)  
- Never bind `0.0.0.0` by default  
- Agent binds `127.0.0.1` only  
- Optional `HUB_TOKEN` for extra auth (header or query); default off for pure Tailscale trust in v1, documented to enable  

### Stack (implementation)

- **Runtime:** Python 3.11+ (stdlib + `websockets` / `aiohttp` or similar already available on machine)  
- **UI:** Single-page app (static HTML/CSS/JS) served by hub — no separate Node build required for MVP  
- **Config:** `config.toml` or `.env` in install dir  

Rationale: few dependencies, easy always-on on Windows, matches spike tooling.

---

## 4. UI / UX

### 4.1 Responsive shell

**Desktop (≥900px)**

- Left rail (~280px): session list, search, New Session  
- Main: header (model/cwd/status) + transcript + composer  
- Optional right drawer later (tools detail); not MVP  

**Mobile (&lt;900px)**

- Full-width chat  
- Top bar: menu (☰) → Sessions sheet (full height, slide over)  
- Same session list content as desktop rail  
- Composer sticky bottom; safe-area insets for iPhone  

### 4.2 Session list

Each row shows:

- Title (generated title or first-prompt fallback)  
- Project path (shortened)  
- Relative updated time  
- Live badge if currently loaded in hub  
- Model id (if known)  

Actions:

- Tap/click → resume (`session/load` with stored cwd)  
- New Session → pick cwd from recent project roots (seed: `D:\Projects\*` + sessions’ cwds)  
- Search filters title/path/snippet  

Data sources:

1. Disk index: scan `~/.grok/sessions/**/summary.json`  
2. Live: sessions opened through hub this process lifetime  
3. Refresh on interval + on `_x.ai/sessions/changed` if agent emits it  

### 4.3 Transcript

Render ACP updates:

| Update | UI |
|---|---|
| `user_message_chunk` | User bubble |
| `agent_message_chunk` | Assistant bubble (stream append) |
| `agent_thought_chunk` | Collapsible “Thinking” block |
| `tool_call` / `tool_call_update` | Expandable tool card (name, status, summary) |
| queue / turn complete notifications | Status strip / subtle system line |

Multiple browser clients share one hub subscription: every event is broadcast.

### 4.4 Composer & slash commands

- Textarea: Enter send (Shift+Enter newline) on desktop; mobile uses Send button primary  
- **Slash palette:** typing `/` opens overlay list from last `available_commands_update` for the active session  
- Filter by name/description  
- Select command: insert `/name ` or execute if no args  
- If command has argument hints (when provided by agent), show secondary field or chips  
- Unknown `/foo` still sendable as plain text to the agent  

Commands are **not** hardcoded TUI lists; they track what the agent advertises so the menu stays honest.

### 4.5 Dual-device behavior (Claude-like)

- Phone and desktop open same hub URL  
- Both receive live stream for the active session  
- Prompt from either device appears as user message on both  
- Session switch on one device updates active session for that client; optional “follow global active session” can be v2 — **v1:** each client has its own selected session id, but all clients subscribed to a session receive its events  

Clarification for v1:

- Hub may keep **multiple** sessions loaded if ACP allows sequential load; if agent is single-active-session, hub switches load on demand and caches transcripts client-side / from disk replay  

Spike showed one stream owner per session connection; hub is that owner and can multiplex many UI sessions by loading the needed session before prompt (with short switch cost).

---

## 5. API surface (hub ↔ browser)

### HTTP

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | SPA |
| GET | `/health` | liveness + agent up/down |
| GET | `/api/sessions` | global session list |
| POST | `/api/sessions` | create new `{ cwd }` |
| POST | `/api/sessions/{id}/load` | load/resume |
| GET | `/api/projects` | recent/known project roots |

### WebSocket `/ws`

Client → hub:

- `{ "type": "subscribe", "sessionId": "..." }`  
- `{ "type": "prompt", "sessionId": "...", "text": "..." }`  
- `{ "type": "cancel", "sessionId": "..." }` if supported  

Hub → client:

- `{ "type": "acp", "sessionId", "message": <raw acp json> }`  
- `{ "type": "sessions", "items": [...] }`  
- `{ "type": "status", "agent": "up"|"down", ... }`  
- `{ "type": "error", "message": "..." }`  

---

## 6. ACP bridge (hub ↔ grok)

On startup:

1. Ensure agent serve listening  
2. Connect `ws://127.0.0.1:2419/ws?server-key=...`  
3. `initialize`  
4. Ready for `session/new` / `session/load` / `session/prompt`  

On UI prompt:

1. Ensure target session loaded on ACP connection  
2. `session/prompt` with text blocks  
3. Forward all subsequent ACP messages with that `sessionId` to subscribed UI clients  

On UI session open:

1. `session/load`  
2. Forward setup notifications + any replay  
3. Update commands from `available_commands_update`  

---

## 7. Always-on packaging (Windows)

- Repo: `D:\Projects\Grok Remote Hub`  
- Scripts:  
  - `start-hub.ps1` — start hub if not running, log to `logs/`  
  - `stop-hub.ps1`  
  - `install-startup.ps1` — Task Scheduler at user logon  
- Logs: rotate simple daily files  
- Config defaults committed as `config.example.toml`  

---

## 8. Security

- Tailscale mesh is primary authz  
- Hub bind: Tailscale IP only  
- Agent secret local-only  
- No prompt content sent to third parties beyond normal Grok/xAI API path the CLI already uses  
- Do not log full prompts by default (optional debug)  
- User’s Grok permission mode remains as configured (`always-approve` today → remote is high privilege; document clearly)  

---

## 9. MVP scope

**In**

- Always-on hub + managed agent serve  
- Tailscale bind :8787  
- Session list (disk + live), search, new, resume  
- Responsive shell (rail / sheet)  
- Live multi-client stream fan-out  
- Composer + agent-driven slash palette  
- Thought/tool expandable blocks  
- Health endpoint + start/stop scripts  

**Out (v2)**

- Stock Grok TUI mirror  
- Full `@` fuzzy file picker  
- Permission approval UI  
- Subagent dashboard  
- HTTPS beyond Tailscale  
- Multi-user ACLs  

---

## 10. Success criteria

1. From iPhone on tailnet, open hub URL, resume an existing Circana session, send a prompt, see streaming reply.  
2. Desktop browser on same session sees the same stream live.  
3. `/` shows commands from the agent for that session.  
4. Session list shows multiple projects’ sessions; switch works.  
5. PC reboot + logon: hub comes back (with startup task).  
6. Hub not reachable from non-tailnet LAN without Tailscale.  

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| ACP session switch loses context | Load before each prompt; test multi-session thrash |
| `available_commands` incomplete | Still allow free-text `/cmd` |
| Large transcript history | Virtualize list; paginate load from updates.jsonl later |
| Agent serve crash | Supervisor restart + UI banner |
| User expects stock TUI | Explicit product copy: “Remote Hub UI” |

---

## 12. Implementation order (preview)

1. Scaffold project + config + agent supervisor  
2. ACP client bridge + fan-out WS  
3. Session index API  
4. SPA shell (rail/sheet + transcript + composer)  
5. Slash palette wired to `available_commands_update`  
6. Startup scripts + smoke test on Tailscale from phone  

Detailed plan follows after spec approval.
