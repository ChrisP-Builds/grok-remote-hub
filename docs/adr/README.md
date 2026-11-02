# Architecture Decision Records

This directory holds Architecture Decision Records (ADRs) — short markdown
files capturing significant technical decisions and the reasoning behind them.

## Format

See [0000-template.md](0000-template.md) for the template when adding new ADRs.

## Why

ADRs preserve the *why* behind technical choices so future engineers don't
silently revert decisions whose original constraints they don't remember.
One file per decision. Body is immutable once Accepted; supersession is via
a new ADR.

## Conventions

- Filename: `<NNNN>-<short-kebab-title>.md` with zero-padded sequence number.
- Title: H1 in present tense. "Use X" not "Considered using X."
- Status: Proposed | Accepted | Deprecated | Superseded by ADR-NNNN.
- Body never changes once Accepted. Edit only the Status line (or append Notes).

## Index

| # | Title | Status | Date |
|---|---|---|---|
| 001 | Hub session lifecycle and sole-writer model | Accepted | 2026-07-10 |
| 002 | Implement full advertised ACP client surface | Accepted | 2026-07-10 |
| 003 | Dual-hub remote topology, not TUI multi-client | Accepted | 2026-07-10 |
| 004 | Detached hub process start via WMI | Accepted | 2026-07-11 |
| 005 | Queue prompts while a turn is running | Accepted | 2026-07-11 |
| 006 | Session-cwd sandboxed file browser over REST | Accepted | 2026-07-11 |
| 007 | Classify subagent sessions via session_kind | Accepted | 2026-07-11 |
| 008 | Persist hub renames as hub_title in summary.json | Accepted | 2026-07-11 |
| 009 | KeepAgent default + view-first continuity (no no-output fork) | Accepted | 2026-07-12 |
| 010 | File-first site preview, not cloud or chat artifacts | Accepted (amended by 026) | 2026-07-12 |
| 011 | Separate agent process health from ACP connection | Accepted | 2026-07-13 |
| 012 | Durable Hub plan-mode approval via plan_mode.json | Accepted | 2026-07-14 |
| 013 | ACP quality beyond process-up and connected boolean | Accepted | 2026-07-14 |
| 014 | In-hub agent restart from hung status pill | Accepted | 2026-07-14 |
| 015 | CLI-aligned turn ownership (cancel frees agent; wake re-sync) | Accepted | 2026-07-16 |
| 016 | Quiet-period session/load suppress before re-prompt | Accepted | 2026-07-17 |
| 017 | Unresumable dead view forces pin (no blank fork) | Accepted | 2026-07-24 |
| 018 | Cold ensure vs hot path attribution | Accepted | 2026-07-24 |
| 019 | External hub health watchdog (long-lived pythonw daemon) | Accepted | 2026-07-26 |
| 020 | Process-local ops metrics and unified diagnostics | Accepted | 2026-07-26 |
| 021 | Flag-gated leader multi-client experiment | Proposed / Experimental | 2026-07-30 |
| 022 | Private agent serve remains default; stock CLI stays independent | Accepted | 2026-08-08 |
| 023 | Always-approve remote risk and CLI resume handoff | Accepted (risk reduced by 027) | 2026-08-08 |
| 024 | Stop cancels the turn then clears hub bg activity honesty | Accepted (cancel mechanics amended by 031) | 2026-08-08 |
| 025 | Mobile plan copy/mermaid and session search/recap continuity | Accepted | 2026-08-08 |
| 026 | Guard Host and Origin, and serve previews as sandboxed capability URLs | Accepted (amended 2026-11-01) | 2026-10-31 |
| 027 | Approve risky agent actions from the phone via a hub-owned Grok hook | Accepted (amended 2026-11-02) | 2026-10-31 |
| 028 | Serve over Tailscale HTTPS, trust the Tailscale login, and send web push | Accepted | 2026-11-01 |
| 029 | Sequence session events and resume reconnects from an in-memory log | Accepted (amended 2026-11-02) | 2026-11-01 |
| 030 | Keep per-session runtime state in one object per session | Accepted | 2026-11-01 |
| 031 | Stop sends the ACP cancel notification and ends the command the agent waits on | Accepted (amended 2026-11-02) | 2026-11-01 |
