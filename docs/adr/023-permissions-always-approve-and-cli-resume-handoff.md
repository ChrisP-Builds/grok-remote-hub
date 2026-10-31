# ADR-023: Always-approve remote risk and CLI resume handoff

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** security, permissions, always-approve, resume, cli, handoff

## Context

The hub starts the agent with `--always-approve` so phone/Tailscale turns do not
block on every tool permission. That is required for usable remote control, but
it means anyone who can reach an unauthenticated (or token-leaked) hub URL can
drive shell, files, and network with the same power as a local Grok session.

Separately, the default topology keeps stock CLI independent of hub live ACP
(ADR-022). Operators still need a documented way to continue a hub session on
desktop: disk-backed resume, not dual-live attach on raw `agent serve`.

## Decision

1. **Always-approve is intentional** for the remote control plane. Operators must
   treat hub network access like handing someone the host keyboard. Mitigations
   are documented in SECURITY.md: Tailscale-only bind, required `hub_token` on
   non-loopback binds, agent on loopback, never expose `0.0.0.0` without token.
2. **Desktop handoff** for the default serve topology is:
   ```text
   grok --resume <session-id>
   ```
   (or continue-when-newest for that project cwd). Session id is visible in the
   hub UI (copyable chip). Do not rely on dual-live CLI + hub on the same serve.
3. **Leader multi-client** remains opt-in only (ADR-021) and is not the default
   handoff path.

## Alternatives considered

- **Prompt every tool remotely:** rejected for mobile usability; stalls on every
  write/shell.
- **PTY-share stock TUI over Tailscale:** rejected (ADR-003); poor phone UX.
- **Default leader so CLI attaches live:** rejected (ADR-022).

## Consequences

### Positive

- Clear security posture: remote is powerful by design; token + Tailscale are
  mandatory ops.
- Clear resume path for desktop without breaking stock CLI independence.

### Negative

- Token leak or open bind is high impact (full agent power).
- Resume is disk/continue semantics, not a second live multi-writer stream.

### Neutral

- UI may surface Catch up / plan copy to make mobile resume usable; does not
  change agent permission model.

## Validation

- SECURITY.md and README document always-approve risk and `grok --resume`.
- Default backend remains serve (ADR-022 tests).
- Hub does not enable leader by default.

## Related

- ADR-003 dual-hub remote topology
- ADR-012 plan-mode handshake
- ADR-021 leader multi-client experiment
- ADR-022 serve default / CLI independent
- SECURITY.md
