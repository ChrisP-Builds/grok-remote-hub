# ADR-008: Persist hub renames as hub_title in summary.json

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** Project owner + Grok Remote Hub session
- **Tags:** sessions, rename, summary.json, rest-api

## Context

Session titles come from Grok-generated fields (`generated_title`, `session_summary`). Users need to rename sessions in the hub (rail pencil and topbar) without waiting for the agent to regenerate a title, and renames must survive reloads and not vanish when the CLI later rewrites generated fields.

The hub already reads and lists `summary.json`; it is the durable per-session metadata file on disk.

## Decision

We persist user renames by writing **`hub_title`** on the session's `summary.json`, and we mirror the same string into **`generated_title`** so other UIs that only read that field stay consistent.

Display title order in the hub:

1. `hub_title`
2. `generated_title`
3. `session_summary`
4. else "Untitled session"

The HTTP surface is **`PATCH /api/sessions/{id}`** with body `{ "title": "..." }` (non-empty, max 200 chars). Writes use an atomic temp+replace when possible. Successful renames may rebroadcast the sessions list to connected clients.

## Alternatives considered

- **Client-only localStorage titles:** lost across devices and reloads of the machine's other UIs; rejected.
- **Separate hub-side SQLite/JSON map of id→title:** another source of truth to keep in sync with disk session deletion; rejected for v1.
- **Overwrite only `generated_title`:** works until the agent regenerates the title; rejected as sole authority.
- **Do nothing:** no rename UX; rejected.

## Consequences

### Positive

- Renames stick across hub restarts and multi-client fan-out.
- Single file Grok already owns; no new DB.

### Negative

- Hub mutates CLI-owned `summary.json` (coordinate carefully with concurrent agent writes).
- Other tools unaware of `hub_title` still need the mirrored `generated_title`.

### Neutral

- Empty titles rejected; long titles truncated to 200 characters.

## Validation

- Unit tests: `hub_title` wins for display; rename writes fields; empty/missing rejected.
- Manual: pencil rename updates rail + topbar after PATCH; refresh keeps the title.

## Related

- `hub/session_index.py` (`_title_from_summary`, `rename_session`)
- `hub/server.py` (`handle_rename_session`)
- `static/app.js` (rename UX)

## Notes

Stale hub processes return **405** on PATCH until restarted — same class of issue as new routes 404ing without a process restart.
