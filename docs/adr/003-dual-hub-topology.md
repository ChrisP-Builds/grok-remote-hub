# ADR-003: Dual-hub remote topology, not TUI multi-client

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** product, remote, tui, tailscale

## Context

Users want Claude-like remote control: phone and desktop seeing the same live agent. Experiments showed:

1. Dual ACP clients on `agent serve` do not both receive `session/update` reliably.
2. `session/load` of a live desktop TUI session + `session/prompt` often emits zero updates.
3. Disk `updates.jsonl` is shared history; live agent process is not multi-writer.

A custom hub will also lag full TUI feature depth. Product scope must stay honest.

## Decision

We use **dual-hub** as the supported remote topology:

- Phone browser + desktop browser both talk to the same local hub over Tailscale (or localhost).
- Live chat uses **hub-owned** sessions (`session/new` in the hub process).
- Stock Grok TUI remains the full local product; the hub does not inject into it.
- Optional desktop `follow.ps1` tails hub session logs for a terminal-style mirror.

Hub is a **thin remote agent stream**, not a second full CLI.

## Alternatives considered

- **Leader-mode shared backend with TUI:** highest upside for true attach; unproven; deferred as a spike, not the default path.
- **PTY-share the real TUI to the phone:** exact chrome, poor mobile UX; rejected as primary product.
- **Do nothing / keep fighting same session id:** repeated hangs; rejected.

## Consequences

### Positive
- Reliable multi-device live stream when both clients are hub UI.
- Clear ops model: one hub process, one agent serve, one live session per project cwd.

### Negative
- Desktop TUI and hub remote threads for the same project can diverge.
- Feature parity with TUI will lag; users must not expect every TUI surface.

### Neutral
- History from TUI sessions can still be *viewed* on hub; live continue attaches a hub remote session.

## Validation

- Multi-turn e2e on hub WS: two prompts on same live session both stream and idle.
- Health shows `productTag: remote-stream`, version/compat badge after CLI upgrades.

## Related

- ADR-001: session lifecycle
- ADR-002: ACP client surface
- README Product scope
