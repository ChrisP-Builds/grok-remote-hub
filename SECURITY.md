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
3. Keep the agent on **`127.0.0.1`** (default). The agent secret file `data/agent.secret` is gitignored.
4. Run the hub only on a machine you trust; treat Tailscale membership like physical access.
5. Never commit `config.toml`, `data/`, `logs/`, or `~/.grok/auth.json`.
6. Binary uploads (`POST /api/fs/upload`) are sandboxed to the session cwd, extension-allowlisted (image/video), and size-capped; treat the hub token as write access to that project tree.

## Reporting a vulnerability

Please **do not** open a public issue for security-sensitive reports.

1. Prefer a **private vulnerability report** on this GitHub repository (Security tab → Advisories), if available on your account.
2. Or open a **minimal public issue** titled `security: contact request` with no exploit detail, and we will follow up.

Include: affected version/commit, impact summary, and reproduction steps if safe to share.

## Supported versions

Security fixes target the latest `master` of this repository. There is no long-term support branch yet for a first public release.
