# ADR-002: Implement full advertised ACP client surface

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** acp, agent-serve, permissions, reliability

## Context

Hub initializes ACP with `clientCapabilities` including `fs.readTextFile`, `fs.writeTextFile`, and `terminal: true`. Early hub only partially auto-replied to permission requests (hardcoded `optionId: "allow-always"`). Agent logs showed:

```text
worker quit with fatal: Transport channel closed, when Auth(AuthorizationRequired)
```

Live sessions hung mid-turn after tools started (list_dir/grep/shell pending forever). Desktop TUI implements the full client surface; a half-client ACP host is not production-grade.

## Decision

We implement every ACP client method we advertise:

1. `session/request_permission` — pick a real option from `params.options` (prefer `allow_always` / `proceed_always*`, then once-allow; never invent `allow-always` if not offered).
2. `fs/read_text_file` / `fs/write_text_file` — sandbox-aware absolute path IO.
3. `terminal/create|output|wait_for_exit|kill|release` — subprocess lifecycle with output caps.

Unknown client methods with `id` must return JSON-RPC error (never leave the agent waiting). Modules: `hub/acp_permissions.py`, `hub/acp_fs.py`, `hub/acp_terminal.py`, dispatched from `hub/acp_client.py`.

## Alternatives considered

- **Advertise no fs/terminal:** shrink capabilities so agent runs tools server-side only. Rejected: Grok agent serve often uses client terminal for shell; still need correct permission outcomes.
- **Hardcode one optionId:** rejected after hang; real option ids are agent-supplied.
- **Do nothing:** continued mid-turn freezes; not viable.

## Consequences

### Positive
- Tool-using turns complete (list_dir + shell e2e under ~seconds when healthy).
- Behavior closer to TUI autonomy under `--always-approve`.

### Negative
- Hub process has full filesystem and shell power (must stay Tailscale-bound / token-gated).
- More code surface to maintain when ACP evolves.

### Neutral
- Still not full TUI UX; only the protocol surface required for tools to run.

## Validation

- Unit tests for `pick_permission_option` with ACP-shaped options.
- Live e2e: prompt that uses list_dir/shell reaches `end_turn` and `turnRunning=false`.
- No multi-minute hangs on simple tool turns without activity.

## Related

- ADR-001: Hub session lifecycle and sole-writer model
- Commits: ACP client surface work under `hub/acp_*.py`
