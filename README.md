# Grok Remote Hub

Always-on Tailscale web UI for **Grok Build**: resume any local project session from phone or desktop, stream live replies on every connected browser, hydrate transcripts from disk.

## Requirements

- Windows PC with Grok Build (`grok` on PATH or `%USERPROFILE%\.grok\bin\grok.exe`)
- Python 3.11+
- Tailscale (recommended) for phone access on your tailnet

## Quick start

```powershell
cd "D:\Projects\Grok Remote Hub"
.\start-hub.ps1
```

Open the printed URL (Tailscale IP on port `8787`), for example:

```text
http://100.x.y.z:8787
```

Stop:

```powershell
.\stop-hub.ps1
```

Start at logon:

```powershell
.\install-startup.ps1
```

## Use from your phone

1. Install Tailscale on the PC and phone; same account / tailnet.
2. Start the hub on the PC (`start-hub.ps1` or the logon task).
3. On the phone browser, open `http://<pc-tailscale-ip>:8787`.
4. Optional: Add to Home Screen for an app-like shell.
5. Pick a session, wait for history + load, then chat.

Without Tailscale the hub binds `127.0.0.1` only and the UI shows **Local only**.

## Security

**This hub auto-approves agent tools** (`grok agent --always-approve serve`). Anyone who can reach the hub can drive an agent with the same power as a local Grok session on your machine (files, shell, network).

Mitigations:

- Prefer **Tailscale only** (default: never bind `0.0.0.0`).
- Set optional `hub_token` in `config.toml` and open `http://…:8787?token=YOUR_TOKEN` (or send `Authorization: Bearer …`).
- Agent secret lives in `data/agent.secret` (gitignored); agent listens on `127.0.0.1` only.

Copy `config.example.toml` to `config.toml` for local overrides.

## Architecture (short)

- Hub process: static SPA + REST + UI WebSocket fan-out
- Sole ACP client to `grok agent serve` on `127.0.0.1:2419`
- Session list from `~/.grok/sessions/**/summary.json`
- Transcript hydrate from `updates.jsonl` (ACP load does not replay chat)

## Dev

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m hub
```

## Known limits (MVP)

- One ACP connection: one active loaded session; mid-turn session switch blocked
- Concurrent prompts rejected while a turn is running
- Cancel is best-effort (agent may not support cancel)
- History capped at ~200 normalized messages
- Session list capped at 80 recent useful sessions
