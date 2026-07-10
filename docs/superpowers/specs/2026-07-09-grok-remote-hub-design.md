# Grok Remote Hub — Design Spec

**Date:** 2026-07-09  
**Status:** Approved (adversarially revised)  
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
- `session/load` barely replays history (setup notifications only)  
- Therefore the hub must own **one** ACP connection, fan out to all UIs, and **hydrate transcripts from disk**

---

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| Access | Tailscale only (primary) |
| Availability | Always-on on PC |
| Architecture | Single hub process + local `grok agent serve` |
| UI | Responsive web shell (desktop session rail, mobile sessions sheet) |
| Slash commands | Agent-driven via ACP `available_commands_update` |
| Port | `8787` |
| Install path | `D:\Projects\Grok Remote Hub` |
| Desktop surface | Same web UI as phone (not stock Grok TUI mirror) |
| Transcript history | Read `updates.jsonl` from disk on open; live ACP for new turns |

---

## 3. Adversarial review fixes (applied)

| Attack / failure | Design response |
|---|---|
| Dual ACP clients steal stream | Hub is sole ACP client; UI clients only talk to hub |
| Load does not replay chat | `GET /api/sessions/{id}/history` parses `updates.jsonl` |
| Two devices prompt same session | Per-session async lock; second prompt queues or 409 busy |
| Switch session mid-turn | Block switch while turn running; show toast |
| Junk temp sessions flood list | Filter: valid UUID id, exclude `Temp\oracle-grok`, require summary or updates |
| Tailscale down / IP changes | Resolve `tailscale ip -4` each start; if missing, bind `127.0.0.1` + UI banner “local only” |
| Mobile keyboard covers composer | `visualViewport` resize; composer pinned above keyboard; `safe-area-inset` |
| Backgrounded iOS kills WS | Auto-reconnect with backoff; resubscribe; banner “Reconnecting…” |
| Empty slash list at open | Palette only after commands event; placeholder “Load a session for / commands” |
| Agent crash | Supervisor restart; all clients get `status.agent=down/up` |
| always-approve remote risk | README warning; optional `hub_token` |
| Huge session dirs | Cap list at 80 by `updated_at`; search hits disk index |
| Concurrent multi-session on one ACP | Single `loaded_session_id`; load-on-demand before prompt; cache UI transcripts |

---

## 4. Architecture

```
Phone / Desktop browser
        │
        │  http://<tailscale-ip>:8787
        ▼
┌─────────────────────────────────────────────┐
│              Grok Remote Hub                │
│  static UI │ Hub WebSocket │ REST           │
│  history   │ fan-out       │ session index  │
│            └──────┬────────┘                │
│                   │ sole ACP client         │
│                   ▼                         │
│        grok agent serve (127.0.0.1:2419)    │
└─────────────────────────────────────────────┘
                    │
                    ▼
         ~/.grok/sessions/<encoded-cwd>/<id>/
              summary.json + updates.jsonl
```

### Processes

1. **Hub** (long-lived): HTTP + UI WebSocket + ACP bridge + session index  
2. **Agent child**: hub-managed `grok agent serve --bind 127.0.0.1:2419 --secret …`  
3. Agent death → restart with backoff → reconnect ACP → notify UIs  

### Network binding

- Prefer Tailscale IPv4 from `tailscale ip -4` (full path on Windows)  
- Config override `bind_host` / `bind_port`  
- Fallback `127.0.0.1` if Tailscale unavailable (local debug; banner)  
- Agent always `127.0.0.1`  
- Optional `hub_token`: require `Authorization: Bearer` or `?token=` on HTTP/WS  

### Stack

- Python 3.11+ / asyncio  
- `aiohttp` (HTTP + WS server) + `websockets` or aiohttp WS client to agent  
- Static SPA: `static/index.html`, `app.css`, `app.js` (no build step)  
- Config: `config.toml`  

---

## 5. UI / UX (polished)

### Visual direction

**Not** generic “AI purple gradient.” Direction: **ops control room for a coding agent**.

| Token | Value | Role |
|---|---|---|
| `--bg` | `#0e1116` | App background |
| `--surface` | `#161b22` | Rail, cards, composer |
| `--surface-2` | `#1c2330` | Elevated / hover |
| `--border` | `#2a3341` | Hairlines |
| `--text` | `#e7ecf3` | Primary text |
| `--muted` | `#8b98a8` | Meta, timestamps |
| `--accent` | `#f0a202` | Live pulse, primary actions (amber signal) |
| `--accent-dim` | `#8a5a00` | Accent borders |
| `--user` | `#1a3a4a` | User bubble |
| `--assistant` | `#141a22` | Assistant bubble |
| `--danger` | `#f07178` | Errors / stop |
| `--ok` | `#3dd68c` | Connected |

- **Type:** UI sans = `IBM Plex Sans` (Google fonts); mono meta/tools = `IBM Plex Mono`  
- **Signature:** 2px amber “live” bar on the active session row + subtle pulse on connection pill  
- Motion: 150–200ms ease; honor `prefers-reduced-motion`  
- Focus: visible amber ring on keyboard focus  
- Density: comfortable chat, compact session rows  

### 5.1 Responsive shell

**Desktop (≥900px)**

```
┌──────── session rail ────────┬──────── main ─────────────────────┐
│ Hub · search · New           │ header: title · model · cwd · ●  │
│ [session rows…]              │ transcript (scroll)                │
│                              │ composer + / palette               │
└──────────────────────────────┴────────────────────────────────────┘
```

**Mobile (&lt;900px)**

- Full-width chat; top bar: ☰ Sessions · title · status  
- Sessions = full-height sheet (slide from left), backdrop dismiss, focus trap  
- Composer sticky bottom with `env(safe-area-inset-bottom)`  
- Tap session → sheet closes, chat focuses  

### 5.2 Session list

Row content:

- Title (generated_title → session_summary → “Untitled session”)  
- Project basename + truncated path  
- Relative time (`2h ago`)  
- Live amber dot if this is hub’s loaded session  
- Optional model chip  

Filters:

- Search box (title, path, id prefix)  
- Hide: non-UUID folders, paths containing `oracle-grok`, empty dirs  
- Sort: `updated_at` desc, max 80  

New session:

- Modal: list recent project roots (`D:\Projects\*` dirs + distinct cwds from sessions)  
- Confirm → `session/new`  

### 5.3 Transcript

| Source | UI |
|---|---|
| History from `updates.jsonl` | Hydrate on open before live events |
| `user_message_chunk` | User bubble (right-aligned soft) |
| `agent_message_chunk` | Assistant bubble, stream append |
| `agent_thought_chunk` | Collapsed “Thinking” disclosure (open while streaming thought) |
| `tool_call` / updates | Card: tool title, status pill, expandable detail |
| errors | Inline danger toast + system line |

Auto-scroll: stick to bottom if user was near bottom; break stick if they scroll up (Claude-like).

### 5.4 Composer & slash

- Textarea auto-grow (max ~8 lines)  
- Desktop: Enter send, Shift+Enter newline  
- Mobile: Send button always visible; Enter can newline  
- Stop button visible while turn running (best-effort cancel; if unsupported, disables input until turn completes)  
- `/` at line start → palette anchored above composer  
- Arrow keys + Enter to select; Esc closes  
- Commands from last `available_commands_update` for loaded session  
- Selecting inserts `/name` or `/name ` and optional arg placeholder  

### 5.5 Dual-device

- All UI clients subscribed to `sessionId` get the same live events  
- Each browser has its own selected session  
- Hub serializes ACP operations (load/prompt) on one connection  
- If session A is mid-turn, prompts to A queue or reject with clear error; switching away is blocked until idle  

### 5.6 Connection UX

- Pill: Connected / Reconnecting / Agent down / Local only  
- Offline: disable send, keep transcript readable  
- On reconnect: resubscribe selected session; do not duplicate history (history load once per open)  

---

## 6. API surface

### HTTP

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | SPA |
| GET | `/health` | hub + agent + bind mode |
| GET | `/api/sessions` | filtered global list |
| GET | `/api/sessions/{id}/history` | normalized messages from disk |
| POST | `/api/sessions` | `{ "cwd": "..." }` → new session |
| POST | `/api/sessions/{id}/load` | load/resume on ACP |
| GET | `/api/projects` | project roots for New Session |

### WebSocket `/ws`

Client → hub:

```json
{"type":"hello"}
{"type":"subscribe","sessionId":"..."}
{"type":"unsubscribe","sessionId":"..."}
{"type":"prompt","sessionId":"...","text":"..."}
{"type":"cancel","sessionId":"..."}
```

Hub → client:

```json
{"type":"status","agent":"up|down","bind":"tailscale|local","tailscaleIp":"..."}
{"type":"sessions","items":[...]}
{"type":"history","sessionId":"...","messages":[...]}
{"type":"acp","sessionId":"...","message":{}}
{"type":"commands","sessionId":"...","commands":[...]}
{"type":"turn","sessionId":"...","state":"running|idle","error":null}
{"type":"error","message":"..."}
```

---

## 7. ACP bridge

Startup: spawn agent → connect WS → `initialize`.

**load(sessionId, cwd):**

1. Acquire global ACP lock  
2. `session/load`  
3. Cache commands from updates  
4. Set `loaded_session_id`  

**prompt(sessionId, text):**

1. If another turn running on any session → error busy (v1 simple)  
2. Ensure loaded  
3. `session/prompt`  
4. Fan out all ACP messages; mark turn idle on prompt result / `prompt_complete`  

**History:** never rely on ACP replay; always disk.

---

## 8. History normalization

Parse `updates.jsonl` lines; extract `session/update` payloads into:

```json
{"role":"user|assistant|thought|tool|system","text":"...","meta":{}}
```

Merge consecutive same-role chunks. Cap hydrate at last ~200 messages for MVP performance.

---

## 9. Always-on packaging

- `start-hub.ps1` / `stop-hub.ps1` / `install-startup.ps1`  
- Logs under `logs/hub-YYYYMMDD.log`  
- `config.example.toml` + local `config.toml` (gitignored)  
- README: Tailscale URL, always-approve warning, phone home screen tip  

---

## 10. Security

- Tailscale primary  
- Optional hub token  
- Agent secret file `data/agent.secret` mode user-only when possible  
- No full prompt logging by default  
- Document: remote = full machine agent power under current Grok permissions  

---

## 11. MVP scope

**In:** hub, agent supervisor, session list/search/new/resume, disk history, live fan-out, responsive UI, slash palette, start scripts, reconnect, busy locking  

**Out (v2):** stock TUI mirror, @ file fuzzy picker, permission UI, subagent tree, multi-turn queue UI, HTTPS  

---

## 12. Success criteria

1. iPhone on tailnet opens hub, resumes a real project session, streams a reply  
2. Desktop browser on same session sees the same stream live  
3. Opening a session shows prior transcript from disk, not empty  
4. `/` shows agent commands after load  
5. Session list spans multiple projects; junk temps hidden  
6. Startup task brings hub back after login  
7. Without Tailscale, local bind works with clear banner  

---

## 13. Implementation order

1. Config + session index + history parser + tests  
2. Agent supervisor + ACP client  
3. aiohttp server (REST + WS fan-out)  
4. SPA (shell, sessions, transcript, composer, slash, polish)  
5. Scripts + README + smoke run on Tailscale IP  
