# First-release readiness (self-exam)

Date: 2026-07-11  
Product: Grok Remote Hub v0.2.x

## Automated results

| Check | Result |
|-------|--------|
| `python -m pytest -q` | **200 passed** (includes unit + structural + e2e smoke when hub up) |
| Hub `/health` | `ok`, agent up, ACP connected (live machine) |
| Playwright e2e (`tests/test_e2e_smoke.py`) | **3 passed**: health/API fields, empty state + filters, open Working session + meta bubble |
| npm `@playwright/test` CLI | **Unreliable** on this Windows/Node 24 setup (hangs on `--list` / `test` >90s). Do not block release on it. |

## Product once-over (manual + code)

### Working well
- Session filters: Working / Subagent / All + noise exclusion
- Session open keeps clicked history (no silent jump to empty live id)
- Topbar meta bubble (body-level popover; hover/tap)
- Session delete (DELETE API + confirm)
- Thinking open by default
- Prompt queue, usage bar, file tree, slash palette
- Unit coverage for session_index, permissions, ask_user helpers, UX helpers

### Known limitations (OK for v0.1/v0.2 if documented)
- **Windows-first** start/stop scripts (WMI, firewall, scheduled task)
- **Sole-writer ACP**: one live turn at a time; multi-session stream is UI continuity, not multi-agent
- **ask_user_question** may still need shape tuning against newer agent builds if agent rejects `outcome` (watch logs)
- **Weekly plan usage** needs local `~/.grok/auth.json`
- **Auto-approve tools** is intentional and powerful — must stay loud in README security

### Release blockers fixed in this pass
- Stale UI copy tests (`Remote agent stream over Tailscale`) updated for empty state
- Personal Tailscale IP / MagicDNS removed from README, start-hub, fix-firewall
- Default `projects_root` → `~/Projects` (not a machine-specific drive path)
- `.gitignore` expanded for probes, dumps, node_modules, playwright artifacts
- Reliable e2e path: Python Playwright smoke (not hanging npx CLI)

### Before you push to public GitHub
1. Confirm no secrets: `config.toml`, `data/`, `logs/` gitignored; never force-add `agent.secret` or `auth.json`
2. Run: `python -m pytest -q` and (with hub up) `python -m pytest tests/test_e2e_smoke.py -v`
3. Choose LICENSE (MIT recommended) if not already present
4. Optional: scrub `SESSION_LOG.md` personal machine notes if the repo is public
5. Do not commit local probe scripts / `out_*.txt` (ignored)

## How to re-run the release gate

```powershell
.\start-hub.ps1
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
python -m pytest tests/test_e2e_smoke.py -v
```
