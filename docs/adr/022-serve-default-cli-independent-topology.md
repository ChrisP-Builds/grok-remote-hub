# ADR-022: Private agent serve remains default; stock CLI stays independent

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** topology, agent, serve, leader, cli, product

## Context

Grok Build CLI gained multi-agent and leader capabilities. Remote Hub must stay a
**thin phone/Tailscale control plane** without forcing stock CLI onto a shared
backend. Dual ACP clients on raw `agent serve` remain unreliable (ADR-003).
Leader multi-client is experimental and opt-in (ADR-021). Operators need a
single durable decision: what is the product default topology?

## Decision

1. **Default agent backend is private `agent serve`** (`Config.agent_backend = "serve"`).
2. **Stock CLI must not require `--leader`** for normal hub use; CLI and hub are
   independent processes unless the operator explicitly enables leader and uses
   `grok --leader`.
3. **Dual-live CLI + hub on raw `agent serve` is rejected** (not a roadmap item).
4. **Leader remains opt-in only** via `[agent] backend = "leader"` (ADR-021);
   never auto-promote; never ship example configs with leader enabled by default.
5. **Session continuity across surfaces** for the default topology is
   disk + `grok --resume <session-id>` (or continue-when-newest for that cwd),
   not shared live attach.

## Alternatives considered

- **Leader default:** rejected — shared outage and CLI coupling.
- **Dual ACP on serve:** rejected — proven unreliable fan-out (ADR-003).
- **PTY-share stock TUI:** rejected — poor mobile UX; not a thin control plane.

## Consequences

### Positive

- Clear product boundary: hub remote sole-writer on serve; CLI independent.
- Experiment path preserved without polluting production defaults.

### Negative

- True multi-client live stream requires opt-in leader experiment, not default.

### Neutral

- ADR-003 dual-hub (phone + desktop browsers to same hub) remains the supported
  multi-device live topology for remote UI.

## Validation

- Config load without backend key yields `agent_backend == "serve"`.
- Structural tests assert default and invalid backend coercion.
- Live health when hub is up uses serve path unless config explicitly sets leader.

## Related

- ADR-001 session lifecycle sole-writer
- ADR-003 dual-hub remote topology
- ADR-009 KeepAgent + no no-output fork
- ADR-021 leader multi-client experiment
