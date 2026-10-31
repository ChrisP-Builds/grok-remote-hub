# ADR-026: Guard Host and Origin, and serve previews as sandboxed capability URLs

- **Status:** Accepted
- **Date:** 2026-10-31
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** security, auth, websocket, preview, csrf

## Context

The agent runs with `--always-approve` (ADR-023), so anything that can send a
`prompt` over `/ws` or call a mutating API controls the host. An architecture
review found three paths around the token:

1. **Cross-site WebSocket hijacking.** Browsers do not apply CORS to
   WebSockets, and `/ws` never checked `Origin`. With a loopback-only, tokenless
   hub (allowed for local dev), any web page open in the desktop browser could
   connect to `ws://127.0.0.1:8787/ws` and send prompts. The same gap allowed
   cross-site `POST`s (e.g. plan approve, restart agent) via simple requests.
2. **DNS rebinding.** No `Host` check, so a hostile domain re-pointed at
   127.0.0.1 could read API responses as same-origin.
3. **Same-origin preview.** `/preview-site/` served project HTML on the hub
   origin with no sandbox, and the iframe used `allow-scripts allow-same-origin`
   (which lets the framed page remove its own sandbox). A previewed page could
   read the hub token from `localStorage` and drive the agent. In token mode
   preview subresources also got 401, since relative URLs carry no `?token=`.

## Decision

1. **Host allowlist** on every request: loopback names, bind hosts, the
   Tailscale IP, this machine's Tailscale DNS name (full and short label), the
   OS hostname, plus `hub.allowed_hosts`. Missing `Host` passes (non-browser).
2. **Same-origin check** on `/ws` and every non-GET/HEAD/OPTIONS request: when
   a browser sends `Origin`, its host[:port] must equal the `Host` header, or
   appear in `hub.allowed_origins` (for proxies that rewrite `Host`).
   `Origin: null` is rejected. Absent `Origin` passes (curl, watchdog).
3. **Tokenless loopback stays allowed.** With checks 1 and 2, browsers cannot
   reach a tokenless hub cross-site, so no new setup step is forced.
4. **Preview is a capability URL**: `/preview-site/<random key>/<entry>`. The key
   changes on every preview start and dies on stop. The token gate skips
   preview GETs; the handler checks the key.
5. **Preview runs in an opaque origin**: response header
   `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups
   allow-modals allow-downloads` (no `allow-same-origin`), plus
   `Access-Control-Allow-Origin: *` so module scripts, fonts and `fetch()` of
   sibling files still load. The iframe drops `allow-same-origin`.
6. **Raw file responses** (`/api/fs/raw`) for HTML, SVG and XML carry
   `Content-Security-Policy: sandbox`, so opening one in a tab runs no script.
7. Token comparison uses `secrets.compare_digest`. All responses get
   `Referrer-Policy: same-origin`, `X-Content-Type-Options: nosniff` and
   `frame-ancestors 'self'` unless the handler set its own CSP.

**Not changed:** `?token=` stays in the address bar. iOS Home Screen web apps
have storage separate from Safari, so the start URL is how the token reaches
them. Revisit with cookie auth once the hub is served over HTTPS.

## Alternatives considered

- **Require a token on loopback too:** closes path 1 but adds setup friction
  and still leaves paths 2 and 3 open.
- **Serve preview on a second port:** a real separate origin keeps
  `localStorage` working for previewed apps, but needs another firewall rule
  and a second Tailscale Serve mapping. The standalone `tools/preview-hub`
  already covers apps that need storage.
- **HttpOnly cookie instead of a JS-readable token:** stronger, but breaks the
  iOS Home Screen bootstrap without HTTPS and a manifest. Deferred.

## Consequences

### Positive

- Drive-by pages, rebinding domains and previewed HTML can no longer reach
  `/ws` or mutating APIs.
- Preview CSS/JS now loads in token mode (Tailscale), which was broken.

### Negative

- Previewed apps that use `localStorage`, cookies or IndexedDB throw in the
  opaque origin. Use `tools/preview-hub` for those.
- Access through an unlisted name (custom DNS, reverse proxy) gets 403 until it
  is added to `allowed_hosts` / `allowed_origins`. The hub logs the rejected
  value and the accepted host list at startup.

### Neutral

- Amends the same-origin serving detail in ADR-010; the file-first product
  decision there stands.

## Validation

- `tests/test_http_guard.py`: host, origin, token, rebinding, CSWSH, headers.
- `tests/test_site_preview.py`: capability key, key rotation, sandbox headers,
  iframe sandbox has no `allow-same-origin`.
- `tests/test_fs_browser.py`: raw responses sandbox script-capable types.

## Related

- ADR-010 file-first site preview
- ADR-023 always-approve remote risk

## Amendment (2026-10-31): redact agent messages at the browser boundary

A permission probe showed the agent broadcasts `_x.ai/mcp/servers_updated`
with MCP server `env` values (API keys) in plain text, and `terminal/create`
carries an `env` block. Agent messages without a `sessionId` are fanned out
to every browser. `_emit_acp` now sends `redact_for_browser(msg)`: values in
`env` / `headers` name-value lists and string fields with secret-like names
become `[redacted]`. Numbers pass, so token counts for the usage bars survive.
Validation: `tests/test_harden_policy.py` redaction tests.