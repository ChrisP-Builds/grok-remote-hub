# Contributing

Thanks for interest in Grok Remote Hub. This project is **Windows-first** and tightly coupled to the local Grok Build CLI + ACP agent.

## Before you open a PR

1. Read [README.md](README.md) (product scope and security model).
2. Skim [docs/adr/](docs/adr/) if your change touches sessions, ACP, or dual-device streaming.
3. Keep secrets and personal machine paths out of commits (`config.toml`, `data/`, `logs/`, probe dumps).

## Dev setup

```powershell
git clone https://github.com/ChrisP-Builds/grok-remote-hub.git
cd grok-remote-hub
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

Live UI smoke (hub must already be running on `:8787`):

```powershell
python -m playwright install chromium
python -m pytest tests/test_e2e_smoke.py -v
```

Prefer the **Python** Playwright suite. Optional Node Playwright (`package.json`) can hang on some Windows/Node builds.

Optional **Preview Hub** (`tools/preview-hub/`) is a Node stdlib companion for static-site preview in browser chrome. Not required for Python tests or the agent hub.

## After hub code changes

Use detached restart (safe from a hub-owned agent session):

```powershell
.\restart-hub.ps1
```

Do not run bare `.\stop-hub.ps1` from inside a live hub/agent turn.

## PR guidelines

- Prefer small, focused commits and PRs.
- Match existing style; avoid drive-by refactors.
- Add or update tests when behavior changes.
- Document user-facing changes in the PR description (and README when install/security changes).

## Issues

Bug reports are welcome via GitHub Issues. Include: OS, Grok CLI version (`grok --version`), hub version from `/health`, steps, and whether Tailscale or localhost.
