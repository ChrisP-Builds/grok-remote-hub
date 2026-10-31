# Security Policy

## What this software can do

Grok Remote Hub is a **remote control surface** for a local [Grok Build](https://x.ai/) agent. By design it:

- Auto-approves agent tools (`--always-approve`) so a turn can run without clicking through every permission prompt.
- Can reach files, shell, and network **with the same power as a local Grok session** on the host PC.
- Exposes an HTTP + WebSocket UI (default port **8787**), typically over **Tailscale**.

If someone can open your hub URL without a token, they can drive that agent.

## Hardening checklist (recommended)

1. **Do not bind `0.0.0.0`.** Defaults dual-bind localhost + Tailscale IPv4 only.
2. Set **`hub_token`** in `config.toml` and open the UI with `?token=…` or `Authorization: Bearer …`.
   - **Empty `hub_token` is refused at startup** when any bind host is non-loopback (including Tailscale dual-bind). Loopback-only binds still allow tokenless local dev.
   - With a token set, unauthenticated `GET /health` returns **liveness only** (`ok`, `bootId`, `uptimeSeconds`, `pid`, `port`, `hubVersion`) so the external watchdog can still probe without secrets. The **static SPA shell** (`/`, `/app.js`, `/app.css`, `/vendor/*`, favicons, similar static assets) is also public so the UI can load after `?token=` document navigation; **API**, **WebSocket**, and **preview** still require the token when configured.
   - Full diagnostics: authenticated `/health` (Bearer or `?token=`) or `GET /api/admin/diagnostics`.
3. Keep the agent on **`127.0.0.1`** (default). The agent secret file `data/agent.secret` is gitignored.
4. Run the hub only on a machine you trust; treat Tailscale membership like physical access.
5. Never commit `config.toml`, `data/`, `logs/`, or `~/.grok/auth.json`.
6. Binary uploads (`POST /api/fs/upload`) are sandboxed to the session cwd, extension-allowlisted (image/video), and size-capped; treat the hub token as write access to that project tree.

## Always-approve remote risk

The hub starts the agent with **`--always-approve`** so remote turns can run without a permission dialog on every tool. That is intentional for phone/Tailscale control, and it means:

- A caller who can authenticate to the hub (or who can reach a tokenless non-loopback bind) can run shell, read/write project files, and use network tools **with the same power as you at the keyboard**.
- Treat the hub token like a root-of-trust for that machine’s agent. Prefer Tailscale membership + strong token; never bind `0.0.0.0` without a token; do not share hub URLs in chat logs.

This is not a limited “chat only” surface. See [ADR-023](docs/adr/023-permissions-always-approve-and-cli-resume-handoff.md).

## Desktop handoff (`grok --resume`)

Default topology keeps stock CLI independent of the hub’s private `agent serve` (ADR-022). To continue a hub session on the desktop terminal:

```powershell
grok --resume <session-id>
```

Copy the session id from the hub UI (session chip under the chat title). For the newest session in a project folder you can also open that cwd in the CLI and continue when prompted. Do **not** expect dual-live CLI + hub streams on raw `agent serve`; use resume/disk continuity, or opt-in leader only if you accept the experiment (ADR-021).

## Reporting a vulnerability

Please **do not** open a public issue for security-sensitive reports.

1. Prefer a **private vulnerability report** on this GitHub repository (Security tab → Advisories), if available on your account.
2. Or open a **minimal public issue** titled `security: contact request` with no exploit detail, and we will follow up.

Include: affected version/commit, impact summary, and reproduction steps if safe to share.

## Supported versions

Security fixes target the latest `master` of this repository. There is no long-term support branch yet for a first public release.
