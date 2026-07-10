# Grok Remote Hub Implementation Plan

> **For agentic workers:** Implement task-by-task. Checkboxes track progress.

**Goal:** Always-on Tailscale web hub that remotes Grok Build with Claude-like session rail, live dual-device stream, disk history, and slash palette.

**Architecture:** Python asyncio hub owns one `grok agent serve` ACP WebSocket, serves static SPA, fans out events to browser clients, hydrates history from `updates.jsonl`.

**Tech Stack:** Python 3.11+, aiohttp, websockets, tomllib, static HTML/CSS/JS.

## Global Constraints

- Install root: `D:\Projects\Grok Remote Hub`
- Bind port `8787`; prefer Tailscale IPv4; fallback `127.0.0.1`
- Agent on `127.0.0.1:2419` with local secret
- No public `0.0.0.0` by default
- UI tokens per design spec (ops control room / amber signal)
- Code via implementer; tests for session index + history parser

### Task 1: Core backend + SPA + scripts

Implement the full MVP in one coherent tree:

```
hub/config.py
hub/session_index.py
hub/history.py
hub/agent_supervisor.py
hub/acp_client.py
hub/server.py
hub/__main__.py
static/index.html
static/app.css
static/app.js
tests/test_session_index.py
tests/test_history.py
requirements.txt
config.example.toml
start-hub.ps1
stop-hub.ps1
install-startup.ps1
README.md
.gitignore
```

Acceptance:

- `python -m hub` starts agent + server
- `/api/sessions` lists real sessions (filtered)
- `/api/sessions/{id}/history` returns messages
- WS prompt streams ACP to all subscribers
- UI: rail/sheet, chat, slash, reconnect, status pill
- `pytest` passes for index/history
- `start-hub.ps1` works on Windows

See design: `docs/superpowers/specs/2026-07-09-grok-remote-hub-design.md`
