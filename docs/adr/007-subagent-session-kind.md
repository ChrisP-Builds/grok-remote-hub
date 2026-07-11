# ADR-007: Classify subagent sessions via session_kind

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** Project owner + Grok Remote Hub session
- **Tags:** sessions, subagent, session-index, grok-metadata

## Context

The hub lists sessions from `~/.grok/sessions/**/summary.json` and exposes kind filters and "subagent" pills in the UI. An early detector treated a path segment named `subagents` (parent UUID → `subagents` → child UUID) as the signal for child agents.

On real installs, that nested layout was **absent** (0 summaries under `subagents/`), while dozens of sessions carried `"session_kind": "subagent"` or `"subagent_fork"`. Using `agent_name` alone also fails: main chats often use `agent_name: "grok-build-plan"`, which is not a subagent. The UI therefore showed **zero** subagent sessions despite most recent rows being child agents.

## Decision

We classify a session as a subagent when:

1. **Primary:** `summary.session_kind` is `subagent` or `subagent_fork` (case-insensitive), or
2. **Legacy fallback:** the session directory path contains a `subagents` segment (parent UUID is the prior path part when present).

We set `parentSessionId` from `summary.parent_session_id` when present, else from the path parent under the legacy layout. We surface `agent_name` as `agentName` for display only; it does **not** alone set `isSubagent`.

## Alternatives considered

- **Path-only nesting under `subagents/`:** matched zero live sessions on this machine; rejected as primary signal.
- **Treat non-empty / non-default `agent_name` as subagent:** false-positives on main `grok-build-plan` sessions; rejected.
- **Do nothing:** filters and pills stay empty; rejected.

## Consequences

### Positive

- Subagent filter and pills match Grok's on-disk truth (~80+ sessions correctly flagged in probes).
- Survives layout changes as long as CLI keeps writing `session_kind`.

### Negative

- Depends on Grok continuing to emit `session_kind`; older sessions without the field stay "standard" unless under a legacy path.
- Hub must re-scan summaries after CLI format changes.

### Neutral

- `agentName` remains available for meta lines and search without changing classification.

## Validation

- Unit tests cover path-nested legacy, sibling folders with `session_kind`, and forks.
- Live scan: `isSubagent` count is non-zero when subagent summaries exist (probe: 86 of 114 listed).

## Related

- `hub/session_index.py` (`scan_sessions`)
- UI: session kind chips, pills in `static/app.js`
- ADR-001: session lifecycle / sole-writer model

## Notes

Probe scripts under repo root are scratch only; do not commit them.
