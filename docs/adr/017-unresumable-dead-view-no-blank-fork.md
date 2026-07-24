# ADR-017: Unresumable dead view forces pin (no blank fork)

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** session, resume, attach, client, reliability

## Context

After hub restart or map drift, the UI can stay pinned on a **dead view** session id (missing on disk, permanent `session/load` FS_NOT_FOUND). Ensure correctly falls through to process-live `byCwd` (`reuse_cwd`), but the attach switch reason was soft-mapped as `cli_or_foreign_session`. The client kept `grh.selectedSession` on the corpse: next open/restart re-attached the dead id, felt blank, and historically contributed to `session/new` storms (untitled stubs).

ADR-009 already requires view-first continuity and no no-output fork. It does not specify client pin policy when the open view is permanently dead.

## Decision

1. **Permanent load failures** are recorded in process-local `_unresumable_session_ids` and dropped from hubIds/byCwd (`_drop_unresumable_from_hub_ids`). Retry/sleep is skipped for permanent path/FS errors (`is_permanent_session_load_failure`).
2. **Bootstrap at hub start:** after loading the remote sessions map, any hubId/byCwd id whose session directory is missing under `sessions_root` is marked unresumable and batch-dropped once (`session_on_disk` + `_bootstrap_unresumable_missing_dirs`).
3. **Ensure single-flight** per cwd so concurrent attach/prompt share one load/new.
4. **byCwd after load fail:** prefer process-live or loadable map id before minting (`next_ensure_after_load_fail`).
5. **Switch reason when view is dead:** if ensure switches and the view is missing on disk (`find_session` None) or in `_unresumable_session_ids`, set switch reason to **`dead_view`** even when ensure reason was `reuse_cwd` / `resume_cwd`. Keep `resume_failed` when load fell through without treating the view as missing/unresumable.
6. **Client force pin:** soft-map exclusion and `openSession` treat `dead_view` and `resume_failed` like `force_ui_switch` — pin UI and durable selection to `liveId`, not the corpse.

## Alternatives considered

- **Soft-map only (cli_or_foreign_session):** Rejected — leaves pin on corpse; restart re-opens blank dead view.
- **Always session/new on dead view:** Rejected — mints untitled sessions and abandons live byCwd (ADR-009).
- **UI-only filter of untitled rows:** Rejected — masks root cause; legitimate New sessions are valid.

## Consequences

### Positive

- Dead attach reuses live byCwd and forces durable pin to live id.
- Hub start drops map corpses before first attach.
- No soft-map trap reopening permanent FS failures.

### Negative

- User who clicked a dead rail row is moved to live id (intentional; no transcript on disk).
- Bootstrap walks hubIds/byCwd against disk layout once at start.

### Neutral

- KeepAgent default unchanged. No-output still never session/new (ADR-009).

## Validation

- Unit: `session_on_disk`, unresumable + byCwd reuse, permanent load markers.
- Structural: ensure `dead_view` switch; client soft-map / openSession force pin; bootstrap missing dirs.
- E2E attach (hub up): dead view attach reuses same live; reason `dead_view` or `resume_failed` after hub restart with this code.

## Related

- [009-hub-restart-keep-agent-one-live-cwd.md](009-hub-restart-keep-agent-one-live-cwd.md)
- [016-quiet-period-load-suppress-before-reprompt.md](016-quiet-period-load-suppress-before-reprompt.md)
- `hub/session_policy.py`, `hub/server.py`, `static/app.js`
