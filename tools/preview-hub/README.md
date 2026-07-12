# Preview Hub

Lightweight local companion for viewing **static sites** (and optional SPAs) in browser chrome: URL bar, iframe, device presets. Built for workflows where the editor has no Simple Browser (for example Grok Build TUI).

**Not** the Python Remote Hub agent UI (that process listens on `:8787`). Preview Hub is a separate Node process.

## Prefer in-hub preview when possible

If you already have the Python hub open with a session on your project: in the **Files** tree, double-click an `.html` / `.htm` file or tap **Preview**. The hub serves that folder same-origin (relative CSS/JS work; Tailscale/phone friendly). Closing the modal stops the preview. No Node required for that path.

Use this CLI companion when you want a standalone preview outside the hub, SPA fallback flags, or a fixed port without a live hub session.

Node **stdlib only**. No extra `npm install` for runtime. Requires **Node 18+** (ES modules).

## Desktop (recommended)

From the Grok Remote Hub repo root:

```bash
npm run preview
# or
node tools/preview-hub/server.mjs --open
```

Serve a specific folder:

```bash
npm run preview:static
# or
node tools/preview-hub/server.mjs --root static --open
```

Opens **Preview Hub** at `http://127.0.0.1:4567/__hub`.  
Site direct: `http://127.0.0.1:4567/`.

Default bind is **127.0.0.1** (loopback only). Default file root is the current working directory (`process.cwd()`).

## SPA fallback (optional)

For client-routed static SPAs, unknown non-file paths can fall back to root `index.html`:

```bash
node tools/preview-hub/server.mjs --root dist --spa --open
# or
set PREVIEW_SPA=1
node tools/preview-hub/server.mjs --root dist --open
```

Default is **off** (clean 404 for missing paths).

## LAN / remote machine on the same network

```bash
npm run preview:lan
# or with a token (recommended):
node tools/preview-hub/server.mjs --host 0.0.0.0 --token YOUR_SECRET --open
```

Then on another device:

```text
http://<this-machine-ip>:4567/__hub?token=YOUR_SECRET
```

You can also pass the token via header: `X-Preview-Token: YOUR_SECRET`.

### True remote (outside LAN)

Use a tunnel, not raw port-forwarding without care:

- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) quick tunnel
- [ngrok](https://ngrok.com/) http 4567

Always keep `--token` (or `PREVIEW_TOKEN`) enabled when the process is reachable beyond your desk.

## Security

| Setting | Risk |
|--------|------|
| `127.0.0.1` (default) | Local only — fine for daily use |
| `0.0.0.0` **without** token | Anyone on the network can browse the static root |
| `0.0.0.0` **with** token | Shared secret required on every route (except health) |

**Never expose `0.0.0.0` without a token.** This server is a preview tool, not a hardened production host.

## Environment and flags

| Env / flag | Default | Meaning |
|------------|---------|---------|
| `PREVIEW_PORT` / `--port` | `4567` | Listen port |
| `PREVIEW_HOST` / `--host` | `127.0.0.1` | Bind address |
| `PREVIEW_ROOT` / `--root` | `process.cwd()` | Static files root |
| `PREVIEW_TOKEN` / `--token` | _(empty)_ | Shared secret for all routes |
| `PREVIEW_SPA=1` / `--spa` | off | Unknown non-file paths → root `index.html` |
| `PREVIEW_OPEN=1` / `--open` | off | Open hub URL in the system browser |

Examples:

```bash
# Windows PowerShell
$env:PREVIEW_PORT = "5000"
node tools/preview-hub/server.mjs --open

node tools/preview-hub/server.mjs --port 8080 --root ./dist --spa
```

## Health check

```text
GET /__hub/health  →  { "ok": true }
```

Unauthenticated by design (for probes).

## How this relates to Grok Remote Hub

| Process | Port (default) | Role |
|---------|----------------|------|
| Python Remote Hub | `8787` | Agent control surface, sessions, ACP |
| Preview Hub (this tool) | `4567` | Static / SPA browser chrome companion |

Preview Hub does **not** replace Simple Browser inside the Python hub. It is a separate companion process you start when you need a real browser viewport while working in the TUI or another editor. Stop it with Ctrl+C when finished.

## Files

```text
tools/preview-hub/
  server.mjs   # HTTP server (static files + hub routes)
  hub.html     # Dark chrome UI (iframe, device presets)
  README.md    # This file
```
