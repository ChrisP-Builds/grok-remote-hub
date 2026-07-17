# Session log — Grok Remote Hub

Operational and session wrap-up notes. Newest first. Cross-project lessons live in the Brain; ADRs live in `docs/adr/`.

### ✅ Session 2026-07-17 — CLI-parity reliability: load suppress, heal, compact honesty

Large reliability/UX slice toward CLI-like hub behavior: hub pre-prompt is ms-scale (ensure ~100ms, send ~0ms); first-token delays after send_ok are agent/history. Fixed scroll thrash (quiet load-replay suppress, ADR-016), false compact vs CTX bar, no-output heal that no longer warm-skips or kills suppress, pending user restore on refresh, single-flight session/load, ADR-015 turn ownership. Live smoke: fresh session first thought ~1.5s turn ok ~2s; fat-session heal smoke real load + 4479 suppressed + settle then honest silence. **Code reliability slice still largely uncommitted on master** (docs handoff commits separately).

**Shipped (docs this handoff):**
- ADR-015 (accepted file), ADR-016 quiet-period load suppress
- This session log; README usage bar clarification; CHANGELOG Unreleased already in tree

**Shipped (code local / uncommitted unless committed separately):**
- Quiet-period load suppress + heal settle; loading always suppresses even with active turn
- Compact toast honesty; CTX bar from signals only
- No-output heal: forget_warm + real load; pending user + Resend
- Single-flight session/load; prompt path timing / agent TTFB
- Tests green (~466 non-e2e at session end)

**Operational mutations (all authorized):**
- Multiple `restart-hub.ps1 -KeepAgent` during debug (no cloud deploy)
- Live WS smokes and fat-session heal probe against local agent
- No git push this session (handoff docs commit only unless user asked otherwise)

**Lessons:**
- Fat `019f57d4` (~27MB) first-token cost is agent/session history, not hub warmup after send_ok
- Hub and CLI share the same session UUID space under `~/.grok/sessions`
- Auto KillAgent on no-output must stay off (kills other projects / thrash)
- Do not release load suppress in heal finally; wait quiet settle before re-prompt (ADR-016)
- Compact X→Y is compact-op metric; bar is signals.json context fill

**State at close / next session:**
- Docs handoff on master; large code dirty tree may remain, commit reliability slice when ready
- Hard-refresh + hub restart for latest static/Python
- Prefer New for snappy turns; fat session for resume only

### ✅ Session 2026-07-14 — Control plane, plan handshake, live terminal, sticky UX

Major reliability/UX slice: Hub plan-mode disk handshake (ADR-012), ACP quality zombie/stale (ADR-013), in-hub restart-agent from hung pill (ADR-014), turn telemetry + capacity/context-budget banners, open-tool wait cues + live `terminal_out`, scroll-linked sticky You with one-line collapse, mobile session-banner hide, View plan inline only when plan active. Pushed `bbbc2f3` to `origin/master`.

**Shipped (pushed):**
- `bbbc2f3` — hub control plane, plan approve, live terminal, sticky UX
- Handoff docs: ADR-013, ADR-014, this entry, CHANGELOG/README sync

**Operational mutations (all authorized):**
- `git push origin master` (`5ade385..bbbc2f3`)
- Multiple hub restarts / KillAgent during hung ACP debug
- `POST .../plan/action` approve used to clear stuck plan_mode
- No cloud deploy beyond GitHub

**Lessons (project-local):**
- View plan must not key off leftover `plan.md` alone — only awaiting/Active chrome
- Sticky You: one flex row, no “tap to expand” helper text; expand on click
- Open-tool silence ≠ dead turn — local heartbeat + strip copy (A/B); terminal_out for real shell (C)
- Do not call `exit_plan_mode` on Hub — use disk handshake (ADR-012)
- Hung pill without action is incomplete UX — restart-agent (ADR-014)

**State at close / next session:**
- `master` == `origin/master` at `bbbc2f3` before handoff docs commit
- Restart hub + hard-refresh for any machine not yet on this build
- Optional: hard context-budget gate later; CHANGELOG Unreleased still accumulates

### ✅ Session 2026-07-13 — ACP health honesty, self-heal, mobile tables

Planned reliability/UX slice (not multi-process agents). Subagent-driven plan: mobile GFM tables; honest agent vs ACP status; auto-reconnect ACP when process is up; hung pill after heal exhaustion. Peer research: closest open peer is grok-remote (multi stdio agents + PWA); GRH leads on Windows session OS + multi-browser sole-writer. Deferred multi-process pool until isolation is proven pain. Captured ADR-011.

**Shipped (local; push when ready):**
- `c500285` — mobile GFM tables
- `5606ce8` — honest agentProcess / acpConnected status
- `f6bcab1` — ACP auto-reconnect heal
- `e1d2b58` — Agent hung — restart after heal exhausted
- `dda2ab2` — CHANGELOG Unreleased notes

**Operational mutations:**
- Hub restarts during debug; no GitHub push this slice (master ahead by 5 as of implement)
- No cloud deploy

**Lessons (project-local):**
- Pill “Agent down” was often ACP-only; check agentProcess + acpConnected
- Heal reuses AcpClient.reconnect; KillAgent remains hard reset
- Multi-process agents are peer-valuable but second priority vs healthy single serve
- Tables: keep raw markdown; overflow-x hidden on transcript kills mobile table scroll

**State at close / next session:**
- Push `c500285..e1d2b58` when ready; restart hub + hard-refresh for smoke
- Optional manual health smoke (process/ACP/table) before tag

### ✅ Session 2026-07-12 — Release v0.3.2, UX stream polish, file-first preview path

Caught up on split-session history; clarified hub restart mid-turn physics (KeepAgent keeps agent; process death ends live stream; client Resend). Shipped public **v0.3.2** (CHANGELOG + GitHub Release) and pushed master including multi-project concurrency (already in 0.3.0). Stream UX: CLI/Hub pills, optimistic user bubble, sticky active You: line, tools collapsed (no “No detail”), Thinking label fix, responsive placeholder, tool-row **Preview** for HTML. Researched Claude/Codex artifacts: stay file-first; do not clone cloud artifacts or CDP browser. Captured ADR-010.

**Shipped (pushed):**
- `d356077` — v0.3.2 stream UX, CLI/Hub pills, Resend, gallery
- `d2d1dc5` — active prompt pin, tool polish, HTML preview from tools
- Earlier on remote: `cd6c738`..`2ea0d56` (multi-turn, site preview, no-output auto-retry)
- GitHub Release: https://github.com/ChrisP-Builds/grok-remote-hub/releases/tag/v0.3.2

**Operational mutations (all authorized):**
- `git push origin master` (through `d2d1dc5`)
- GitHub Release **v0.3.2** created
- Hub restarts during debug earlier in day; no cloud deploy beyond GH

**Lessons (project-local):**
- Hub process death ≠ soft WS reconnect; bootId clear + Resend, never claim mid-stream continuity
- Multi-project parallel is public (max 3 cwd); same-cwd still queues; not multi-agent pool
- Artifact path = Files/site preview + tool Preview; not Claude/Codex clone (ADR-010)
- Hard-refresh after static-only UI; KillAgent when agent hung after auto-retry

**State at close / next session:**
- `master` == `origin/master` at `d2d1dc5` before this handoff docs commit; probes stay untracked
- Optional next tag if Unreleased polish accumulates
- Resume: hard-refresh clients for latest static

### ✅ Session 2026-07-12 — Multi-session handoff (split recovery + v0.3.x)

Synthesized work across three hub session ids created by restart/no-output forking, plus git as ground truth. **No new ADR** (ADR-009 already covers KeepAgent + view-first + no no-output fork). Public history lives in `CHANGELOG.md`.

**Session ids (split narrative):**
| Id | Role |
|----|------|
| `019f4d9f-17c5-7720-8b40-d9fb8758b2be` | **Main** — early hub, ACP, files, mobile (archive) |
| `019f578a-4e88-7352-9ee3-85a3842b01ed` | **PRIMARY live** — restart continuity / debug (current `byCwd`) |
| `019f57a5-f9cd-7d30-baee-62fa1650ed4a` | Accidental **no-output fork** (do not use for new work) |

**Shipped (git, unpushed on master as of handoff):**
- `cd6c738` — multi-turn, session continuity, ask-user, KeepAgent restart (v0.3.0)
- `a39c428` — Browser Preview Hub CLI (`tools/preview-hub`)
- `fe0c407` — in-hub HTML site preview + real device viewports + CHANGELOG (v0.3.1)

**Also in tree (this wrap):** auto-retry on no-output (load/reconnect + same-session re-prompt once) so users are not stuck on “send again.”

**Operational mutations (all authorized):**
- Hub restarts (KeepAgent / recovery); force-clear stuck turns on `019f578a`
- No push; no cloud deploy

**Lessons (project-local):**
- Multi-session handoff: treat **git + CHANGELOG + ADRs** as ground truth; list all split session ids in SESSION_LOG; one wrap only
- No-output: **never** `session/new` / map rewrite (ADR-009); auto-retry same id after load/reconnect
- `019f57a5` is a fork stub; continue only on `019f578a`
- Silent ACP after prompt (60s) often follows multi-session load thrash or stale agent; auto-retry reduces friction

**State at close / next session:**
- Live map GRH → `019f578a…`; ignore fork for chat
- Push when ready: `cd6c738..fe0c407` (+ recovery commit if landed)
- Tag `v0.3.1` optional after push; paste CHANGELOG section into GitHub Release
- If hub chat still hangs after auto-retry: `.\restart-hub.ps1 -KillAgent` once, hard-refresh UI

### ✅ Session 2026-07-11 — Subagent classify, rename, tool density

Session rail and transcript polish: fixed subagent detection (`session_kind`, not path nesting), row tooltips (project / model / path), kind filter + pin (prior commit), rename via pencil + `PATCH` / `hub_title`, and mobile tool rows compacted to single-line collapsed summaries. Live probe: **0** path-nested subagents vs **~86** `session_kind` matches. Rename **405** was a stale hub process (route not loaded). Captured ADR-007 and ADR-008.

**Operational mutations (all authorized):**
- Hub restart required for Python routes (`PATCH /api/sessions/{id}`, session_index); static JS/CSS hard-refresh only
- No push; no cloud deploy

**Lessons (project-local):**
- Grok `session_kind` (`subagent` / `subagent_fork`) is authoritative for pills/filter; path `subagents/` may not exist; `agent_name` alone false-positives (`grok-build-plan` is main)
- Rename writes `hub_title` (+ mirrors `generated_title`); title order hub_title → generated_title → session_summary — see ADR-008
- Long-lived hub returns 405/404 on new routes until restart (also Brain: same-error-after-fix-suspect-stale-running-process)
- Mobile tools: short label + single-line ellipsis summary; explicit `:not([open])` hide for details bodies

**State at close / next session:**
- Docs handoff: ADRs 007–008, this entry, README UI/architecture notes
- Feature code still uncommitted on `master` (`session_index`, rename API, static UI, tests) unless committed separately
- Untracked probe/scratch files under repo root: keep uncommitted
- Resume: restart hub, hard-refresh phone, verify Subagent filter + rename + compact tools

### ✅ Session 2026-07-11 — File tree, mobile UX, slash/skills, queue, transcript density

Large UX/API pass on the sole-writer stream: Sessions|Files rail with sandboxed list/read/write (+ markdown/Mermaid/image preview), project chip + context usage bar, slash palette (fixed mobile scroll, name-first match, skills from disk), agent command cache, FIFO prompt queue with unlocked composer, collapsible desktop rail, and high-signal transcript (tools collapsed; plan auto-expands active tasks). Captured ADR-005 (queue) and ADR-006 (session-cwd file browser).

**Operational mutations (all authorized):**
- Local hub restarts for new routes (`/api/fs/*`, `/api/skills`, `/api/sessions/{id}/usage`)
- No push; no cloud deploy

**Lessons (project-local):**
- Restart the long-lived hub after adding routes or clients see empty 404s (usage bar, fs, skills)
- Slash `desc.includes` rewrote `/handoff` → `/doc-sync`; name-first ranking + submit typed exact names
- Composer must not use `disabled` during turns if queue is desired; force-unlock after status/turn events
- Tool noise: collapse by default; plan should open while items are pending/running (ADR-adjacent UX)
- See ADR-005 / ADR-006 for queue and file-browser decisions

**State at close / next session:**
- Feature commits on `master` through tool/plan transcript work; handoff docs ADRs 005–006 + this entry
- Untracked probe/scratch files under repo root should stay uncommitted
- Resume: hard-refresh clients after hub restart when testing queue / skills / usage

### ✅ Session 2026-07-11 — Hub reliability + handoff (ACP, dual-hub, WMI start)

Built and hardened the always-on remote hub: Tailscale dual-bind SPA, disk tail of `updates.jsonl`, hub-owned ACP sessions, full client surface (permissions/fs/terminal), attach-on-open, non-blocking WS prompts, TUI-aligned stall timeouts, file tree + create project, version/compat badge, WMI-detached start. Captured ADRs 001–004. Product scope fixed as thin remote agent stream (dual-hub), not stock TUI multi-client.

**Operational mutations (all authorized):**
- Local hub restarts via `start-hub.ps1` / `stop-hub.ps1` (WMI detached)
- Windows Firewall rule via `fix-firewall.ps1` (elevated, once)
- No remote push; no production cloud deploy

**Lessons (project-local):**
- Live prompts must never `session/load` foreign/CLI ids; only hub `session/new` (or reuse hub remote for that cwd) — see ADR-001
- Advertise only ACP methods you implement; inventing `optionId` hangs tools — see ADR-002
- Phone + desktop share one hub UI process; stock TUI is separate (follow.ps1 for disk mirror) — see ADR-003
- `Start-Process` from agent shells dies with the job object; use WMI `Win32_Process.Create` — see ADR-004
- WS receive loop must `create_task` long prompt/cancel handlers or multi-turn keepalive dies
- Mid-turn “stuck running” was often hub/agent dead or incomplete ACP client, not client timeout alone

**State at close / next session:**
- Docs handoff applied (ADRs, this log, README architecture). Large uncommitted code tree remains for a later feature commit if desired.
- Optional later: file-tree polish when hub is up; leader-mode TUI spike (deferred in ADR-003).
- Resume in this repo’s clone root (not another project’s cwd).
