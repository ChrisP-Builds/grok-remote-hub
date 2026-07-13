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
| 010 | File-first site preview, not cloud or chat artifacts | Accepted | 2026-07-12 |
| 011 | Separate agent process health from ACP connection | Accepted | 2026-07-13 |
