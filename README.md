# Grok Remote Hub

Always-on Tailscale web UI for **Grok Build**: resume any local project session from phone or desktop, stream live replies on every connected browser, hydrate transcripts from disk.

**License:** [MIT](LICENSE)

## Product scope

- **Hub = remote control of the agent stream** over Tailscale (or localhost). It is a thin client for prompts, live session updates, and saved history.
- **Not full TUI parity.** Desktop Grok CLI TUI is a separate process; the hub does not inject into that TUI. Hub-owned sessions stream independently.
- Create a project folder under the configured root from New session (`POST /api/projects`), then start a remote session in that cwd.
- After a CLI upgrade, check the **Hub · CLI** badge in the rail footer, or `GET /api/compat` / `POST /api/compat/refresh` for structural compatibility (versions, agent/ACP, sessions root, static UI). Smoke does not run a paid model prompt.

## Requirements

- Windows PC with Grok Build (`grok` on PATH or `%USERPROFILE%\.grok\bin\grok.exe`)
- Python 3.11+
- Tailscale (recommended) for phone access on your tailnet

## Development / release checks

```powershell
# Unit + structural tests (no hub required)
python -m pip install -r requirements-dev.txt
python -m pytest -q

# Live UI smoke (hub must be running on :8787)
python -m playwright install chromium
python -m pytest tests/test_e2e_smoke.py -v
```

Optional Node Playwright (`package.json`) is provided for JS tooling, but on some Windows/Node builds the CLI hangs. Prefer the Python Playwright suite above for CI and release gates.

## Quick start

```powershell
cd path\to\grok-remote-hub   # your clone of this repo
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
2. Start the hub on the PC (`start-hub.ps1` or the logon task). Confirm it prints `Hub is up` and both URLs show `OK`.
3. **Windows Firewall (required once):** open **elevated** PowerShell in the repo root and run:
   ```powershell
   .\fix-firewall.ps1
   ```
   Without this, Safari on the phone often cannot connect even though the PC can open the same URL.
4. On the phone (Tailscale connected), open the URL printed by `start-hub.ps1` (Tailscale IP and optional MagicDNS), for example:
   - `http://<tailscale-ip>:8787`
5. Optional: Add to Home Screen for an app-like shell.
6. Pick a session under **Working**, wait for history + load, then chat.

### If Safari still will not load

| Check | What to do |
|---|---|
| Hub dead | On PC: `.\start-hub.ps1` then open `http://127.0.0.1:8787` in desktop browser |
| Firewall | Run `.\fix-firewall.ps1` **as Administrator** |
| Tailscale on phone | Status should show Connected; both devices same account |
| Wrong URL | Must include `:8787` and `http://` (not https) unless Serve is enabled |
| Optional HTTPS | Enable Serve in the Tailscale admin console, then `tailscale serve --bg http://127.0.0.1:8787` and use your MagicDNS HTTPS URL |

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
- Sole ACP client to `grok agent serve` on `127.0.0.1:2419` (full client surface: permissions, fs, terminal)
- **Sole-writer sessions:** live prompts use hub-owned `session/new` only; never `session/load` of foreign/CLI ids for prompt
- **Dual-hub topology:** phone + desktop browsers share this process over Tailscale; stock Grok TUI is not multi-client
- Session list from `~/.grok/sessions/**/summary.json`
- Transcript hydrate from `updates.jsonl` (ACP load does not replay chat)
- Detached start via WMI (`start-hub.ps1`); detached restart via `restart-hub.ps1` (do not `stop-hub.ps1` from a hub/agent session); see [docs/adr/](docs/adr/) (ADR 001–008)
- Sandboxed REST file browser for the session cwd (`/api/fs/list|read|write|raw`); skills index (`/api/skills`)
- FIFO **prompt queue** while a turn runs (Stop clears the queue); see ADR-005
- Session list classifies **subagents** from `summary.json` `session_kind` (legacy path fallback); user renames via `PATCH /api/sessions/{id}` → `hub_title` (ADR-007, ADR-008)

## UI capabilities (current)

- **Sessions | Files** rail: list/search; **Working | Subagent | All** filters; pin; rename; delete; project/model/path info bubble; file tree (edit/save, markdown + Mermaid, images)
- **Composer:** multi-line grow, iOS ≥16px no-zoom, slash palette, prompt queue while a turn runs, OS spellcheck
- **Transcript:** tools collapsed by default; thinking open for live progress; plan auto-expands active items; mid-turn session switch keeps streaming
- **Chrome:** dual usage bar (session context + weekly plan), collapsible desktop rail

### Privacy note (plan usage)

The hub reads `~/.grok/auth.json` on this machine (same login the Grok CLI uses), refreshes an access token server-side, and calls xAI billing credits to show the **W** (weekly) plan segment. Access and refresh tokens never leave the hub process in API responses or browser payloads.

## Safari and desktop: what is live where

| Path | Live together? | Notes |
|---|---|---|
| Safari hub ↔ desktop **browser** hub | Yes | Same hub process; WebSocket fan-out to every open UI |
| Safari hub → **stock Grok CLI TUI** | No | Separate process; cannot inject into the TUI |
| Safari hub → **desktop terminal follower** | Yes | Read-only tail of the same `updates.jsonl` the hub/agent write |

### Desktop terminal follower

While the hub (or any Grok agent) is writing a session, open a terminal on the PC and run:

```powershell
cd path\to\grok-remote-hub
.\follow.ps1
# or pin a session / project:
.\follow.ps1 --session <session-uuid>
.\follow.ps1 --cwd "C:\path\to\your\project"
.\.venv\Scripts\python.exe -m hub.follow -v
```

Behavior:

1. Resolves session (`--session`, else hub's `logs/last-remote-session.txt` after a Safari remote prompt, else most recent for `--cwd`, else most recent overall)
2. Prints title / cwd / path and a compact recent transcript (`You:` / `Grok:`; `-v` adds tools/thoughts)
3. Tails `updates.jsonl` from EOF and prints new turns live
4. Ctrl+C exits cleanly (read-only on disk; does not touch the hub server)

**Safari remote streaming:** prompts never inject into the stock desktop TUI session. The hub creates a **hub-owned** agent session (`session/new`) and may send `session_switch` so the UI follows that id. After a remote prompt, follow the new id:

```powershell
.\follow.ps1                          # defaults to last-remote-session.txt when present
.\follow.ps1 --session <NEW_REMOTE_ID>
```

Use this when you chat from Safari on the hub UI and want the same stream in a desktop terminal without the stock Grok TUI.

## Dev

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m hub
```

After code changes that need a hub process restart, use the detached restart script from any shell (including a hub-owned agent session):

```powershell
.\restart-hub.ps1
```

Do **not** run bare `stop-hub.ps1` from a hub/agent session: that kills the hub mid-turn and leaves the browser stuck until someone runs `start-hub.ps1` again. `restart-hub.ps1` schedules stop+start via WMI so the restart chain survives the hub dying.

## Known limits (MVP)

- One ACP connection: one active loaded session; mid-turn session switch blocked
- Concurrent prompts rejected while a turn is running
- Cancel is best-effort (agent may not support cancel)
- History capped at ~200 normalized messages
- Session list capped at 80 recent useful sessions
