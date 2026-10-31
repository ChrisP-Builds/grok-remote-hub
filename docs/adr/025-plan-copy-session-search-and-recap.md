# ADR-025: Mobile plan copy/mermaid and session search/recap continuity

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** plan, resume, recap, search, mobile, ux

## Context

Phone users need plan content usable without desktop TUI, and session
findability/catch-up after sleep or many projects—without reopening the
foreign-session hang path or no-output `session/new` forks (ADR-001/009/017).
CLI has title resume, recap, Mermaid plan previews; hub had approve handshake
(ADR-012) but weaker mobile affordances.

## Decision

1. **Plan modal:** Copy plan markdown control; re-run existing Mermaid render
   when the plan body is filled (lazy vendor mermaid; do not block initial SPA load).
2. **Session discovery:** Pure `session_matches_query(title, id, cwd, q)` and
   optional `GET /api/sessions?q=`; client search also matches id/title/cwd basename.
3. **Catch up:** `GET /api/sessions/{id}/recap` builds a bounded last-user /
   last-assistant style summary from session disk history (`hub/recap_view.py`);
   SPA “Catch up” control shows it without starting a new agent turn.
4. **Non-goals:** In-hub plan.md editor; `exit_plan_mode` RPC; full TUI plan chrome;
   live-prompt of foreign/CLI session ids; no-output or dead-view `session/new`.

## Alternatives considered

- **Only client-side filter without API `q`:** weaker for future multi-device; pure
  helper still unit-tested either way. API `q` kept thin.
- **Auto-inject recap as a user prompt:** rejected — burns a turn; Catch up is read-only.
- **Full timeline/jump UI:** deferred (TUI-scale).

## Consequences

### Positive

- Mobile plan review and session re-entry closer to CLI continuity.
- Pure helpers keep tests free of SPA flakiness.

### Negative

- Recap quality depends on history files present on disk; empty sessions return empty recap.

### Neutral

- Plan approve/request_changes/quit disk handshake unchanged (ADR-012).

## Validation

- `tests/test_session_search.py`, `tests/test_recap_view.py`, plan viewer UI contracts.
- Structural SPA: plan copy, Catch up, mermaid render on plan open.

## Related

- ADR-012 hub plan approval handshake
- ADR-009 / ADR-017 resume and unresumable pin
- ADR-023 CLI resume handoff for desktop
