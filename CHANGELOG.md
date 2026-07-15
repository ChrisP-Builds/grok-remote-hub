# Changelog

All notable changes to **Grok Remote Hub** are documented here.  
Format inspired by [Keep a Changelog](https://keepachangelog.com/). Versions follow the hub `__version__` when bumped; otherwise entries are grouped by **release themes** with commit SHAs for GitHub.

This file is the **public narrative**. Session chat context is not required to understand history.

---

## [Unreleased]

### Added
- **ACP structured trace** — ring buffer + daily `logs/acp-trace-YYYYMMDD.jsonl`; `GET /api/admin/acp-trace?n=100`; last 5 on `/health` as `acpTraceRecent` (connect/send/recv/probe/heal/compact/quality, no secrets or full prompts).
- **ACP WebSocket ping probe** — after 45s idle silence (no pending RPC), hub pings the agent WS; probe fail forces unhealthy so heal reconnects (no auto-KillAgent).
- **Files media Share/Save** — video preview (mp4/mov/webm/m4v), Web Share API from Files, higher raw serve limit (150 MB), optional `?download=1` Content-Disposition (file-first; ADR 006/010).
- **Binary upload + attach** — `POST /api/fs/upload` into session `uploads/`; composer paperclip and Files Upload; path prefill; image 40 MB / video 150 MB caps + MIME allowlist.
- **Hub plan viewer** — `GET /api/sessions/{id}/plan` reads `plan.md` + `plan_mode.json`; View plan modal.
- **Hub plan-mode handshake** — `POST /api/sessions/{id}/plan/action` writes `plan_mode.json` (approve / request_changes / quit) so Approve clears `awaiting_plan_approval` without stock TUI `a`-key / `exit_plan_mode` (ADR 012); Approve also auto-sends continue inject text. Inline plan strip + View plan only when plan is awaiting/Active (not leftover `plan.md`).
- **ACP quality** — `acpQuality` (`ok`/`stale`/`zombie`/`down`); chat-ready requires quality ok; zombie send-fail disconnect (ADR 013).
- **Restart agent from pill** — when hung/down, click status pill → confirm → `POST /api/admin/restart-agent` (KillAgent-style serve recycle; hub stays up) (ADR 014).
- **Turn telemetry / capacity** — `liveTurns` age/silence/ttfb; capacity banner while work runs.
- **Soft context budget** — advisory when session `updates.jsonl` / tokens exceed soft thresholds (no hard gate).
- **Open-tool wait cues** — strip prefers running over quiet while tools open; local `waiting · Ns` heartbeat.
- **Live terminal_out** — hub terminal/* pump streams deltas to UI tool rows.
- **Tool-row site Preview** — when a tool summary/path ends in `.html`/`.htm`, a compact **Preview** control opens the existing in-hub site preview (file-first; ADR 010).
- **Sticky active user prompt** — scroll-linked You: pin; one-line collapsed by default; click expands; higher contrast sticky bar.

### Fixed
- **Orphan agent turn after hub force-clear** — stall watchdog, admin reset-turn, and no-output recovery now call `session/cancel` (via `notify_agent_cancel`) so the agent releases the old prompt; UI unlock no longer leaves the next message blocked forever.
- **Heavy-session no-output false kill** — scale stall threshold by `updates.jsonl` size (60s base / 180s soft / 300s heavy); never suppress ACP activity mid-turn; skip redundant `session/load` on no-output retry when already loaded; release load-suppress before re-prompt.
- **Turn elapsed/silence timers seed from server age** — strip `running · Ns` and tool `waiting · Ns` survive hard-refresh/reconnect (no more client-only `Date.now()` reset to 0s).
- **session/load historical replay no longer streamed live** — drops agent history flood during load (stops UI tool strobe); history still via REST/WS.
- **Stale ACP heal no longer kills mid-prompt** — heal skips `stale` while a turn is active (stall watchdog owns silent prompts); `ACP_STALE_SECONDS` raised to 90s so quality does not flip before the 60s no-output policy.
- **Compact token sanity** — reject absurd/non-finite compact token counts (>5M or negative) hub-side and in UI so bogus “375k context” / scroll thrash does not paint.
- **`/compact` context bar + feedback** — hub intercepts `/compact` via `_x.ai/compact_conversation`, broadcasts `compact`/`usage` from `_x.ai/session_notification` `auto_compact_*`, and the UI updates the context bar with before→after (or no-op) feedback instead of waiting on the 6s poll alone.
- **Live stream text doubling** — `mergeStreamText` matches history cumulative-vs-delta merge so mid-turn assistant/thought text no longer looks duplicated.
- Tools stay **collapsed** by default; empty expand no longer shows “No detail.”
- Thinking summary no longer doubles the word “Thinking.”
- Composer placeholder adapts to width (short vs slash-hint) with CSS ellipsis.
- Status pill distinguishes agent process up vs ACP disconnected; auto-reconnect ACP when process is up (capped retries).
- Mobile transcript GFM tables re-parse from raw stream text and scroll horizontally.
- Mobile: hide session-banner; sticky You flush to top of chat scroll.

---

## [0.3.2] — 2026-07-12

Stream feel, session source labels, and restart UX polish on top of the 0.3.x hub.

### Added
- **CLI / Hub source pills** — session rail labels hub-owned vs stock CLI/TUI sessions (`isCli` on the session API; distinct pill colors).
- **Optimistic user bubble** — composer submit paints your message immediately; server echo is deduped.
- **One-tap Resend after hub process restart** — last prompt kept in sessionStorage; Resend on the error strip when a live turn was interrupted (client-first; no server store of prompt text).
- **Stream parity** — clearer Thinking panels, richer tool detail, tools open while running/pending; subagent spawn/finish as system lines in history and live stream.

### Fixed
- **No-output auto-retry** — on first silent turn, hub reloads the same session (reconnect ACP if needed) and resends once; only then surfaces a soft failure. No `session/new`, no map rewrite.
- Nested ACP content shapes for thought/tool text extraction (history + live UI).

### Docs
- README gallery screenshots refreshed from a live hub (sanitized demo titles/paths).

---

## [0.3.1] — 2026-07-12

### Added
- **In-hub site preview** (Files tree): double-click or **Preview** on `.html` / `.htm` opens a same-origin modal; **Close** stops the preview instance (`hub/site_preview.py`, `/api/preview/*`, `/preview-site/*`).
- **Public CHANGELOG** — release-oriented history mapped to commits and ADRs.

### Fixed
- Device presets (mobile / tablet / desktop) set a **real iframe viewport** so CSS media queries reflow; no longer only max-width crop. Same fix in CLI Preview Hub chrome.

---

## [0.3.0] — 2026-07-12

Hub version bump to **0.3.0**. Concurrent multi-project turns, restart continuity, Preview Hub CLI, public-ready packaging polish.

### Added
- **Multi-turn across projects** — default up to 3 concurrent live turns on different project folders; same-cwd prompts still queue (`max_concurrent_turns`, multi-turn policy).
- **Session continuity after hub restart** — view-first ensure; KeepAgent default on `restart-hub.ps1`; no-output recovery **keeps the same session** (no surprise `session/new` fork). ADR 009.
- **Browser Preview Hub (CLI)** — Node stdlib companion under `tools/preview-hub/` for static/SPA preview when the editor has no Simple Browser (`npm run preview`).
- **Ask-user ACP shapes** aligned with agent `outcome` discriminant; stop/cancel force-clears hub turn state.

### Commits (newest first)
| SHA | Summary |
|-----|---------|
| `a39c428` | Browser Preview Hub companion (Node stdlib) |
| `cd6c738` | Multi-turn, session continuity, ask-user, KeepAgent restart |

---

## [0.2.x] — Public readiness & session UX — 2026-07-11

First public-facing packaging: MIT, SECURITY, CONTRIBUTING, scrubbed paths, session filters, ask-user UX, tests.

### Added
- Working / Subagent / All session filters; pin; residual idle status; meta popover; mobile delete.
- Session UX helpers; soft-attach live prompt session; ask-user UI path.
- Unit tests + Python Playwright smoke; release readiness notes.
- Public README gallery (sanitized screenshots), logo, architecture SVG.

### Fixed / chore
- Portable defaults (`~/Projects`); remove personal Tailscale examples from scripts.
- Untrack session lab notes / superpowers from git; ignore probe dumps.

### Commits
| SHA | Summary |
|-----|---------|
| `a79282d` | README gallery with safe product screenshots |
| `45feadb` | Public README, SECURITY, CONTRIBUTING, community files |
| `be7fa9b` | Portable quick start and release readiness notes |
| `8afa7ff` | UX helper tests + Python Playwright smoke |
| `7964be1` | SPA session filters, residual strip, mobile delete, meta popover |
| `daecd46` | Session UX helpers, ask-user ACP, residual idle status |
| `48fef5b` | Portable defaults; scrub personal paths; restart-hub.ps1 |
| `9504b23` | MIT license |
| `efad04a` | Keep personal notes and superpowers out of git |
| `898b4c4` | Docs handoff — ADRs 007–008 |

---

## Usage bars, subagents, plan UI — 2026-07-11

### Added
- Weekly **plan usage** bar (billing credits via CLI auth).
- Compact dual usage bars (session context + plan) with token counts / popovers.
- Subagent session kind pills, filter, pin-to-top.
- Collapsible tool rows; auto-expand active plan tasks.
- Collapsible session rail; smart Browse sessions control.

### Fixed
- JWT from `auth.json` when refresh is revoked.
- Context bar hover used/total tokens; session vs monthly clarity.

### Commits
| SHA | Summary |
|-----|---------|
| `5b0a04a` | Subagent pills, kind filter, pin-to-top, larger weekly bar |
| `e0c3501` | auth.json JWT for weekly plan when refresh revoked |
| `1744b7d` | Weekly plan usage bar from billing credits |
| `30bb06e` | Compact dual usage bar with counts and popovers |
| `bc34ada` | Context bar hover used/total; session vs monthly |
| `e2d7a18` | Docs handoff — ADRs 005–006 |
| `39b64f5` | Collapse tool rows; auto-expand active plan tasks |
| `4f3ae83` | Collapsible session rail; smart Browse sessions |

---

## Core hub reliability (ACP, sessions, ops) — 2026-07-10–11

Foundation for production remote use: full ACP client surface, sole-writer sessions, WMI start, desktop follow, prompt queue.

### Added
- Full ACP client: permissions, fs, terminal (tool turns complete).
- Sole-writer / hub-owned sessions; attach-on-open; dual-hub topology (not TUI multi-client). ADRs 001–004.
- Detached hub start via WMI; stop-hub process tree; follow.ps1 disk mirror.
- Prompt queue while a turn is running; composer stays usable.
- Slash command cache/rebroadcast; context usage bar.

### Fixed
- Slash palette mobile positioning / scroll jump / name-first matching.
- Composer unlock during queued turns.

### Commits
| SHA | Summary |
|-----|---------|
| `8aa18d8` | Slash palette mobile scroll jump / flash |
| `65f7ce1` | Slash name-first matching; skill palette listing |
| `b6b153e` | ACP full client, sole-writer sessions, WMI start, follow |
| `72832c9` | Docs handoff — ADRs 001–004, session log |
| `fd5aa0e` | Keep composer unlocked during turns for prompt queue |
| `44a180c` | Queue prompts while agent turn is running |
| `3a916af` | Cache and rebroadcast agent slash commands |
| `991ce20` | Slash palette mobile fixed positioning |
| `b91430d` | Slash palette + context usage bar |

---

## Files, markdown, mobile shell — 2026-07-10

### Added
- File tree rail; `/api/fs` list / read / write; image lightbox.
- Markdown edit/preview; Mermaid in markdown preview.
- Project chip; mobile composer / viewport fixes.

### Fixed
- Transcript scroll bounce; user echo dedupe; autoGrow height; iPhone focus zoom.

### Commits
| SHA | Summary |
|-----|---------|
| `154da5d` | Image preview and lightbox for files tree |
| `6a8cd84` | Always show project name chip |
| `7682583` | Mobile composer / iPhone focus zoom |
| `764bac5` | Top-align composer; stop tall empty input |
| `b792a75` | Transcript scroll bounce during stream |
| `dc18b3c` | Dedupe user message echo |
| `101f863` | Mermaid in markdown file preview |
| `fc7460c` | Markdown Edit/Preview for .md files |
| `515a3ae` | Composer scrollHeight before autoGrow constrain |
| `04eed74` | File tree rail and mobile composer fixes |
| `0019b2a` | Expose `/api/fs` list read write endpoints |

---

## Earlier foundation

Earlier commits (pre-file-tree) establish the aiohttp hub, agent supervisor, session index/tailer, SPA shell, Tailscale dual-bind, and first smoke paths. Use `git log --oneline` for the full linear history before `0019b2a`.

---

## Architecture decision records

Product “why” lives in `docs/adr/` (not only in chat):

| ADR | Topic |
|-----|--------|
| 001 | Session lifecycle / sole-writer |
| 002 | Full ACP client surface |
| 003 | Dual-hub topology (not TUI multi-client) |
| 004 | Detached hub start (WMI) |
| 005–006 | Prompt queue; session cwd file browser |
| 007–008 | Subagent kind; title/rename |
| 009 | KeepAgent + continuity (view-first; no no-output fork) |

---

## Reading this on GitHub

1. **Releases** — tag `v0.3.0` at `a39c428` (or after in-hub site preview is committed) and paste the matching section above into the release notes.
2. **Commits tab** — still the source of truth for diffs; this changelog is the human index.
3. **Do not rewrite published history** on `main` without a coordinated force-push plan; prefer forward commits + changelog.

### Not in git (by design)

Runtime and lab artifacts stay local: `config.toml`, `data/`, `logs/`, probe dumps, personal screenshots, `.playwright-mcp/`.
