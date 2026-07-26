# ADR-020: Process-local ops metrics and unified diagnostics

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** ops, observability, health, diagnostics, windows

## Context

The hub already had daily `logs/hub-YYYYMMDD.log`, structured ACP trace
(`logs/acp-trace-YYYYMMDD.jsonl` + ring + `GET /api/admin/acp-trace`), and
rich `/health` / WS status fields (`acpQuality`, liveTurns, heal, bootId).
The external watchdog keeps process state in `logs/watch-hub-state.json`
(ADR-019) but that file was not exposed over HTTP.

Operators still could not answer “what happened in the last hour?” without
grepping logs: no process-level counters for force-clear, no-output retries,
ensure outcomes, WS connect/disconnect, heal/restart, or a single admin
endpoint that joins health + ops + ACP trace + watch state.

## Decision

1. **Process-local `OpsMetrics`** (`hub/ops_metrics.py`) — integer counters
   (always-present defaults) plus a small event ring (deque maxlen 200).
   Optional daily JSONL `logs/ops-YYYYMMDD.jsonl` when `log_dir` is set.
   `inc` / `emit` / `snapshot*` never raise. Field redaction reuses the same
   secret/prompt-style sanitization as ACP trace (no full prompts).

2. **Wire at high-value call sites only** — WS connect/disconnect/client
   reset, ensure reuse/load/new/resume_failed, prompt ok/error/no_output,
   turn force-clear, ACP reconnect/heal attempt/ok/fail, agent restart begin,
   bg working/stuck/cleared (phase transitions), session_switch, hub_start.

3. **Enrich `/health`** with `uptimeSeconds`, `pid`, `opsCounters`,
   `opsRecent` (10). WS `status` stays leaner: counters + uptime, no ops
   event dump.

4. **`GET /api/admin/diagnostics`** (same auth as other admin routes) —
   one JSON snapshot: health summary, ops counters+events (`?n=`), ACP trace
   (`?trace=`), watch-hub-state.json if present (no secrets), log file path
   hints, and map sizes (byCwd, hubIds, acpCreated, unresumable, bg working/stuck).

5. **No external metrics backend** — no Prometheus, OpenTelemetry, or
   remote push. Phone/desktop operators use HTTP + log files on the hub host.

6. **Client light touch** — status pill tooltip includes short `bootId` and
   uptime when present from status/health.

## Alternatives considered

- **Prometheus / OpenTelemetry stack:** useful in multi-host fleets; rejected
  for a single Windows always-on hub (ops cost, deps, no scrape path from phone).
- **Log-only grepping:** already failed operators during remote sessions.
- **Exposing full prompts in ops events:** rejected (redact like acp_trace).
- **Auto-KillAgent from metrics:** rejected (ADR-014 / ADR-019 KeepAgent).

## Consequences

### Positive

- One authenticated GET answers recent force-clears, heals, ensures, and
  WS churn without grepping.
- Counters survive only for the process life (aligned with bootId); restarts
  reset cleanly and `hub_start` marks the boundary.
- Watchdog state is visible next to in-process health without a second tool.

### Negative

- Counters are not durable across hub restarts (JSONL helps only if read from disk).
- High-rate events are intentionally not every ACP recv (ring would thrash).

### Neutral

- ACP trace remains the deep wire lifecycle log; ops is coarser process narrative.

## Validation

- `tests/test_ops_metrics.py` — pure counter/emit/snapshot/redact.
- Structural: server registers `/api/admin/diagnostics`; health body includes
  `opsCounters`.
- Live (optional): `curl -s http://127.0.0.1:8787/api/admin/diagnostics?n=20`
  (with hub token if configured).

## Related

- ADR-011: agent process vs ACP health
- ADR-013: ACP quality zombie/stale
- ADR-014: in-hub agent restart
- ADR-019: external hub health watchdog
- `hub/ops_metrics.py`, `hub/acp_trace.py`, `GET /api/admin/acp-trace`
