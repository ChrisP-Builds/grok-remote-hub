# ADR-006: Session-cwd sandboxed file browser over REST

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** Project owner + hub implementers
- **Tags:** filesystem, security, ui, rest

## Context

Remote users need to inspect and lightly edit project files without relying on the agent for every open. ACP already exposes agent-side `fs/read_text_file` / `fs/write_text_file` for the model; the browser needs a separate, authenticated path that cannot escape the project the session is attached to.

Early designs gated all UI paths under `projects_root`. Real sessions often use that root, but the authoritative boundary for “this chat’s files” is the **session working directory (cwd)**.

## Decision

We provide a **hub REST file browser** independent of ACP agent fs:

1. Module `hub/fs_browser.py` resolves paths under the **session root (cwd)** only (sandbox primary key = root; `projects_root` is not required as outer bound for list/read/write).
2. Endpoints: `GET /api/fs/list`, `GET /api/fs/read`, `PUT /api/fs/write`, `GET /api/fs/raw` (binary for images).
3. UI: Sessions | Files rail tabs; lazy tree; text edit/save; markdown Preview (+ Mermaid); image preview + lightbox; insert path into composer.
4. Reject `..`, absolute rel paths, and resolve escapes; size/binary guards on read/write/raw.

ACP `acp_fs` remains for the agent process; the UI does not call agent fs methods for browsing.

## Alternatives considered

- **Reuse ACP fs only:** couples UI to agent load state; poor for history-only attach.
- **projects_root-only sandbox:** rejects valid session cwds outside the configured projects folder.
- **Full IDE CRUD (create/rename/delete):** deferred; higher risk and scope.

## Consequences

### Positive
- Any hub session with a real cwd can browse its tree.
- Auth matches other `/api/*` (optional hub_token).

### Negative
- Write over Tailscale is powerful; sandbox mistakes are high impact.
- Images/files outside session cwd (e.g. Imagine under `~/.grok/sessions`) are not in the tree unless copied into the project.

## Validation

- Unit tests: escape, list/read/write, binary/oversize (`tests/test_fs_browser.py`).
- Manual: expand folders, open/edit/save, preview md/image.

## Related

- Spec: `docs/superpowers/specs/2026-07-10-file-tree-and-mobile-composer-design.md`
- Commits: `3732a6d`, `0019b2a`, `04eed74`, `154da5d`, `fc7460c`, `101f863`
