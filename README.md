# Grok Remote Hub

[![License: MIT](https://img.shields.io/badge/License-MIT-f5a524.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](#requirements)
[![Network: Tailscale](https://img.shields.io/badge/Network-Tailscale-242424.svg)](https://tailscale.com/)

<p align="center">
  <img src="docs/assets/banner.svg" alt="Grok Remote Hub — always-on Tailscale UI for Grok Build sessions" width="100%">
</p>

**Always-on web UI for [Grok Build](https://x.ai/)** on your PC. Resume project sessions from phone or desktop, stream live agent turns to every open browser, and hydrate chat history from disk.

> **Security first:** the hub auto-approves agent tools. Anyone who can reach it can drive an agent with the same power as a local Grok session. Prefer Tailscale + an optional `hub_token`. See [Security](#security).

---

## Why this exists

The stock Grok CLI TUI is excellent on the desktop, but it is a **single local process**. Remote Hub is the thin, always-on control surface when you want:

| You want… | Remote Hub |
|---|---|
| Chat from **Safari / phone** while the agent runs on the PC | Yes (over Tailscale) |
| **Phone + desktop browser** seeing the same live stream | Yes (WebSocket fan-out) |
| Resume **saved sessions** and project history | Yes (`~/.grok/sessions`) |
| Inject prompts into the **stock Grok TUI** | **No** (separate process) |

**Hub = remote control of the agent stream**, not full TUI parity.

---

## Features

- **Session rail** — Working / Subagent / All filters, search, pin, rename, delete
- **Live stream** — multi-browser WebSocket fan-out; mid-turn switch keeps continuity
- **History** — hydrate from `updates.jsonl` when you open a session
- **Composer** — multi-line input, slash palette, prompt queue while a turn runs
- **Files** — sandboxed tree for the session cwd (edit, markdown + Mermaid, images)
- **Usage** — session context bar + weekly plan bar (from local Grok login)
- **Ops scripts** — detached start/stop/restart, firewall helper, optional logon task
- **Terminal follower** — `follow.ps1` tails the same session in a desktop terminal

---

## Requirements

| Need | Notes |
|---|---|
| **Windows** PC | Start/stop scripts use WMI / firewall / scheduled tasks |
| **Grok Build** | `grok` on PATH or `%USERPROFILE%\.grok\bin\grok.exe` |
| **Python 3.11+** | Hub runtime |
| **Tailscale** (recommended) | Phone / remote browser on your tailnet |

---

## Quick start

```powershell
git clone https://github.com/ChrisP-Builds/grok-remote-hub.git
cd grok-remote-hub

python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# optional local overrides (gitignored)
copy config.example.toml config.toml

.\start-hub.ps1
```

Open the URL printed by the script (port **8787**), for example:

```text
http://100.x.y.z:8787          # Tailscale
http://127.0.0.1:8787          # this PC only
```

| Action | Command |
|---|---|
| Stop | `.\stop-hub.ps1` |
| Restart (safe from a hub session) | `.\restart-hub.ps1` |
| Start at Windows logon | `.\install-startup.ps1` |

> **Do not** run bare `stop-hub.ps1` from inside a live hub/agent turn. Use `restart-hub.ps1` so stop+start is scheduled and survives the hub process exiting.

---

## Use from your phone

1. Install **Tailscale** on the PC and phone (same account / tailnet).
2. Start the hub on the PC. Confirm it prints `Hub is up` and health checks show `OK`.
3. **Once, as Administrator**, open the firewall for the hub:

   ```powershell
   .\fix-firewall.ps1
   ```

   Without this, Safari often cannot connect even when the PC can open the same URL.

4. On the phone (Tailscale connected), open:

   ```text
   http://<tailscale-ip>:8787
   ```

5. Optional: **Add to Home Screen** for an app-like shell.
6. Pick a session under **Working**, wait for history + load, then chat.

### If Safari still will not load

| Check | What to do |
|---|---|
| Hub dead | On PC: `.\start-hub.ps1`, then try `http://127.0.0.1:8787` |
| Firewall | Run `.\fix-firewall.ps1` **as Administrator** |
| Tailscale | Phone Connected; same account as the PC |
| URL | Must include `:8787` and `http://` (not https) unless Serve is set up |
| Optional HTTPS | Tailscale Serve → `tailscale serve --bg http://127.0.0.1:8787` |

Without Tailscale the hub binds **localhost only** and the UI shows **Local only**.

---

## Security

**This hub auto-approves agent tools** (`grok agent --always-approve serve`). Treat network access like handing someone your keyboard.

**Mitigations:**

1. Prefer **Tailscale only** (default never binds `0.0.0.0`).
2. Set optional **`hub_token`** in `config.toml`:

   ```toml
   [hub]
   hub_token = "long-random-string"
   ```

   Then open `http://…:8787?token=long-random-string` (or send `Authorization: Bearer …`).

3. Agent listens on **`127.0.0.1` only**; secret file `data/agent.secret` is gitignored.

Full policy and reporting: **[SECURITY.md](SECURITY.md)**.

Copy `config.example.toml` → `config.toml` for local overrides (`config.toml` is never committed).

---

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `hub.bind_port` | `8787` | UI HTTP/WS port |
| `hub.bind_host` | auto | Localhost + Tailscale IPv4 when available |
| `hub.hub_token` | empty | Optional shared secret for UI access |
| `hub.projects_root` | `~/Projects` | Project folders for New Session |
| `hub.sessions_root` | `~/.grok/sessions` | On-disk session index |
| `agent.bind` / `port` | `127.0.0.1:2419` | Local `grok agent serve` |

After a **CLI upgrade**, check the **Hub · CLI** badge in the rail footer, or:

- `GET /api/compat`
- `POST /api/compat/refresh`

Smoke checks are structural (versions, agent/ACP, sessions root, static UI). They do **not** spend a paid model turn.

---

## Architecture (short)

```text
  Phone / desktop browsers
           │  HTTP + WebSocket
           ▼
   ┌───────────────────┐         ACP WebSocket          ┌────────────────────┐
   │  Grok Remote Hub  │ ─────────────────────────────► │  grok agent serve  │
   │  SPA · REST · WS  │   sole client (permissions,    │  127.0.0.1:2419    │
   └───────────────────┘   fs, terminal, prompts)       └────────────────────┘
           │
           ├── ~/.grok/sessions/**/summary.json   (list)
           └── updates.jsonl                      (history hydrate + follow)
```

- **Sole ACP client** — browsers never talk to the agent directly.
- **Sole-writer prompts** — live turns use hub-owned `session/new`; the hub does not `session/load` foreign/CLI ids for prompting.
- **Dual browser, not dual TUI** — phone + desktop browsers share this process; stock Grok TUI is separate.

Design decisions: **[docs/adr/](docs/adr/)** (ADR 001–008).

### What is live together?

| Path | Live together? | Notes |
|---|---|---|
| Safari hub ↔ desktop **browser** hub | Yes | Same process; WS fan-out |
| Safari hub → **stock Grok CLI TUI** | No | Separate process |
| Safari hub → **desktop terminal follower** | Yes | Read-only tail of `updates.jsonl` |

### Desktop terminal follower

```powershell
.\follow.ps1
.\follow.ps1 --session <session-uuid>
.\follow.ps1 --cwd "C:\path\to\your\project"
```

Ctrl+C exits cleanly (read-only on disk; does not stop the hub).

---

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m hub
```

Live UI smoke (hub already on `:8787`):

```powershell
python -m playwright install chromium
python -m pytest tests/test_e2e_smoke.py -v
```

Prefer **Python Playwright**. Optional Node Playwright (`package.json`) can hang on some Windows/Node builds; do not block on it.

Contributing guide: **[CONTRIBUTING.md](CONTRIBUTING.md)**.  
Internal release checklist: **[docs/RELEASE_READINESS.md](docs/RELEASE_READINESS.md)**.

---

## Known limits (v0.2)

- **One live turn at a time** (sole ACP connection); multi-session UI continuity is not multi-agent concurrency
- Cancel is best-effort (depends on agent support)
- History capped (~200 normalized messages in the UI path; config can raise index caps)
- Session list capped at recent useful sessions (default 80)
- **Windows-first** ops scripts

---

## Privacy note (plan usage)

The hub may read `~/.grok/auth.json` on this machine (same login as the Grok CLI) to show the weekly plan usage bar. Access and refresh tokens are **not** returned in API responses or browser payloads.

---

## License

[MIT](LICENSE) © ChrisP-Builds

Issues and PRs welcome. Please keep secrets and personal machine paths out of the tree.
