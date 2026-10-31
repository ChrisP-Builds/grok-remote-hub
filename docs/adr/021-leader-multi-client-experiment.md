# ADR-021: Flag-gated leader multi-client experiment

- **Status:** Proposed / Experimental
- **Date:** 2026-07-30
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** experiment, agent, leader, acp, multi-client

## Context

ADR-003 deferred "leader-mode shared backend with TUI" as an unproven spike.
Probe evidence (`logs/exp-leader/FINDINGS.md`) shows:

1. `grok agent leader` exposes Windows named-pipe IPC and `~/.grok/leader.lock`.
2. `grok agent --always-approve --leader stdio` speaks **NDJSON JSON-RPC** ACP
   (initialize succeeds on one stdout line).
3. Desktop CLI can attach with `grok --leader` to the same leader process.

Goal: prove hub can opt into that shared backend **without** changing the
default path used by normal hub and CLI users.

## Decision

1. **Default remains `agent serve`** (`Config.agent_backend = "serve"`).
2. **Opt-in only:** `[agent] backend = "leader"` (or `hub.agent_backend`).
3. When leader:
   - `AgentSupervisor` spawns
     `grok agent --always-approve leader --no-exit-on-disconnect --relay-on-demand`
     (detached, same style as serve).
   - Readiness is process/lock/pipe, **not** TCP `:2419`.
   - `AcpClient` uses long-lived
     `grok agent --always-approve --leader stdio` with NDJSON stdin/stdout.
4. Health/status expose `agentBackend: "serve"|"leader"` for ops.
5. No auto-enable; no requirement that CLI users pass `--leader` for normal hub use.

## Alternatives considered

- **Make leader the default:** rejected. Breaks independence of stock CLI and
  current serve-based hub ops (port, secret, restart scripts).
- **Dual ACP on serve:** already rejected in ADR-003 (unreliable dual fan-out).
- **Content-Length LSP framing for stdio:** not needed; probe proved NDJSON.

## Consequences

### Positive

- Path to true multi-client (CLI `--leader` + hub) on one agent process.
- Serve path and existing tests stay the product default.

### Negative

- Leader mode means **shared outage**: leader death takes CLI `--leader` and hub
  together.
- Restart/kill semantics differ (no agent port; `grok leader kill` + pid tree).
- Experiment surface may lag serve hardening (probe, heal, recycle).

### Neutral

- Framing is NDJSON today; if Grok changes wire format, adapter must follow.

## Kill criteria

- stdio no longer accepts NDJSON initialize without a supported adapter.
- Manual dual-client lab cannot share a session between hub and `grok --leader`.
- Leader readiness flaky enough to thrash supervisor spawn/kill loops.
- Any pressure to enable by default without product sign-off → keep experimental.

## Validation

- Unit: default backend `serve`; `leader` accepted; supervisor/acp leader paths exist.
- Probe script: `scripts/exp-leader-probe.ps1` → `logs/exp-leader/probe-out.txt`.
- Manual: `docs/exp-leader-lab.md` dual-client runbook.
- Default config: hub continues on serve (live hub not switched by this ADR).

## Related

- ADR-003 dual-hub topology (leader deferred there)
- ADR-002 ACP client surface
- ADR-011 agent process vs ACP health
- ADR-014 in-hub agent restart
- `docs/exp-leader-lab.md`
- `logs/exp-leader/FINDINGS.md`
