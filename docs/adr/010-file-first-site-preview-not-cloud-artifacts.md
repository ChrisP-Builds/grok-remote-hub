# ADR-010: File-first site preview, not cloud or chat artifacts

- **Status:** Accepted
- **Date:** 2026-07-12
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** preview, files, product-scope, security

## Context

Claude.ai Artifacts, Claude Code published pages, and Codex’s in-app browser all surface “open this in a browser” differently: chat-side SPA blobs, cloud-hosted private URLs with live republish, or embedded Chromium with CDP/agent browser use.

Grok Remote Hub already has sandboxed Files, markdown/image preview, in-hub HTML site preview (`hub/site_preview.py`), and optional Preview Hub. The product question was whether to clone artifact platforms or lean on disk files under the session cwd.

## Decision

**We treat project disk as the artifact store and in-hub (or companion) iframe preview as the renderer.**

1. Agent writes previewable files under the session cwd (HTML, md, images).
2. Users open them via Files → Preview, or **Preview** on tool rows when an HTML path is present.
3. We do **not** build: cloud artifact CDN/gallery/org share, chat-memory-only SPA side panels as primary, or hub-owned CDP/agent browser.

## Alternatives considered

- **Claude-style chat artifacts (blob in panel):** Fast for demos; fights file-first resume, dual clients, and sole-writer disk history.
- **Claude Code-style cloud publish URL:** Shareability; hosting, auth, ACL, and leak surface outside a thin Tailscale hub.
- **Codex-style embedded browser + CDP:** Agent QA of live apps; large security and maintenance surface on a remote-control hub (agent MCP can cover inspect when needed).
- **Do nothing:** Already had Files preview; missing discovery from tool rows (addressed as thin glue, not a new platform).

## Consequences

### Positive

- High chance of “see what the agent built” without a second product.
- Same security model as the hub (project roots, Tailscale, token).
- Preview stays same-origin and phone-friendly.

### Negative

- No one-click shareable public artifact URL.
- Ephemeral chat HTML without a file is not a first-class path.

### Neutral

- Preview Hub remains optional for SPA/static outside a live hub session.

## Validation

- HTML under session cwd opens via Files and via tool-row Preview.
- Relative CSS/JS load in in-hub preview.
- No new always-on cloud or Chromium process for the common case.

## Related

- ADR-003: Dual-hub remote topology (thin stream, not full TUI)
- ADR-006: Session-cwd sandboxed file browser
- Commits: `fe0c407` (in-hub preview), `d2d1dc5` (tool-row Preview), research session 2026-07-12
