# Session log — Grok Remote Hub

Operational and session wrap-up notes. Newest first. Cross-project lessons live in the Brain; ADRs live in `docs/adr/`.

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
- Resume in `D:\Projects\Grok Remote Hub` (not Circana Connections cwd).
