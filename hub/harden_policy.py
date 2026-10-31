"""Pure policy helpers for hub hardening (token gate, public health, heal exhaust).

Stdlib only. No I/O. Unit-testable without aiohttp or server state.
"""

from __future__ import annotations

from typing import Any

# Minimal /health body for unauthenticated clients when hub_token is set.
# Watchdog only needs ok; bootId/uptime/pid/port help ops without leaking traces.
PUBLIC_LIVENESS_KEYS = frozenset({
    "ok",
    "bootId",
    "uptimeSeconds",
    "pid",
    "port",
    "hubVersion",
})

# Rich keys that must never appear on unauthenticated public liveness.
# Tests assert these are stripped; not an exhaustive inventory of /health.
RICH_HEALTH_KEYS = frozenset({
    "opsCounters",
    "opsRecent",
    "acpTraceRecent",
    "liveTurns",
    "pendingQuestionSessions",
    "capacity",
    "hosts",
    "tailscaleIp",
    "acpHealAttempts",
    "acpHealError",
    "acpHealExhausted",
    "acpConnected",
    "acpQuality",
    "agent",
    "agentProcess",
    "agentDetail",
    "turnRunning",
    "turnSessionId",
    "loadedSessionId",
    "bind",
    "host",
    "cliVersion",
    "compatOk",
    "compatIssues",
    "wsClients",
    "startedAt",
})


def is_loopback_host(host: str) -> bool:
    """True for loopback bind targets only.

    0.0.0.0 / :: wildcards are not loopback (rejected elsewhere for binds).
    """
    h = str(host or "").strip().lower()
    if not h:
        return False
    # Strip IPv6 brackets if present.
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in ("127.0.0.1", "localhost", "::1"):
        return True
    # IPv4 loopback range 127.0.0.0/8
    if h.startswith("127."):
        parts = h.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return True
    return False


def empty_hub_token_allowed(hosts: list[str]) -> bool:
    """Empty hub_token is allowed only when every bind host is loopback."""
    if not hosts:
        return False
    return all(is_loopback_host(h) for h in hosts)


# Exact SPA shell paths (aiohttp path has no query string).
_PUBLIC_STATIC_EXACT = frozenset({
    "/",
    "/index.html",
    "/app.js",
    "/app.css",
    "/favicon.svg",
    "/favicon.ico",
})

_PUBLIC_ASSET_EXTENSIONS = frozenset({
    ".js",
    ".css",
    ".svg",
    ".map",
    ".woff",
    ".woff2",
    ".png",
    ".ico",
    ".html",
})


def is_public_unauthenticated_path(method: str, path: str) -> bool:
    """
    Paths that may be served without hub_token so SPA shell can load after
    tokenized document navigation. API, WebSocket, and preview remain protected.
    """
    p = str(path or "")
    m = str(method or "").upper()

    if p == "/health":
        return True

    if m not in ("GET", "HEAD"):
        return False

    if p == "/api" or p.startswith("/api/"):
        return False
    if p == "/ws" or p.startswith("/ws/"):
        return False
    if p == "/preview-site" or p.startswith("/preview-site/"):
        return False

    if p in _PUBLIC_STATIC_EXACT:
        return True

    if p.startswith("/vendor/"):
        return True

    lower = p.lower()
    for ext in _PUBLIC_ASSET_EXTENSIONS:
        if lower.endswith(ext):
            return True

    return False


def require_hub_token(hosts: list[str], hub_token: str) -> tuple[bool, str | None]:
    """Validate hub_token against bind hosts.

    Returns (ok, error_message). error_message is None when ok.
    """
    token = str(hub_token or "").strip()
    if token:
        return True, None
    if empty_hub_token_allowed(list(hosts or [])):
        return True, None
    return (
        False,
        "hub_token is required when binding non-loopback hosts (e.g. Tailscale). "
        "Set hub.hub_token in config.toml to a long random secret, then open the UI "
        "with ?token=… or Authorization: Bearer …. "
        "Empty hub_token is only allowed for loopback-only binds (127.0.0.1 / ::1 / localhost).",
    )


def public_liveness_body(full: dict) -> dict[str, Any]:
    """Project full health dict to PUBLIC_LIVENESS_KEYS only."""
    src = full if isinstance(full, dict) else {}
    out: dict[str, Any] = {}
    for key in PUBLIC_LIVENESS_KEYS:
        if key in src:
            out[key] = src[key]
    # Always include ok (watchdog needs it). Prefer source; default True if missing.
    if "ok" in src:
        out["ok"] = bool(src["ok"])
    else:
        out["ok"] = True
    return out


def health_response_body(
    full: dict,
    *,
    authenticated: bool,
    hub_token: str,
) -> dict[str, Any]:
    """Select full vs public liveness health body.

    - hub_token empty (local loopback dev): always full (tokenless local).
    - hub_token set and authenticated: full.
    - hub_token set and not authenticated: public liveness only.
    """
    token = str(hub_token or "").strip()
    if not token:
        return dict(full) if isinstance(full, dict) else {}
    if authenticated:
        return dict(full) if isinstance(full, dict) else {}
    return public_liveness_body(full if isinstance(full, dict) else {})


def should_auto_restart_agent_on_heal_exhaust(
    *,
    enabled: bool,
    agent_process_up: bool,
    acp_connected: bool,
    heal_attempts: int,
    max_attempts: int,
    turn_active: bool,
    auto_restarts_used: int,
    max_auto_restarts: int = 1,
) -> bool:
    """True when config-gated auto restart-agent should run after heal exhaust.

    Mid-turn (turn_active) MUST return False. Process must be up, ACP not
    chat-usable, heal exhausted, and restart budget remaining.
    """
    if not enabled:
        return False
    if turn_active:
        return False
    if not agent_process_up:
        return False
    if acp_connected:
        return False
    try:
        attempts = int(heal_attempts)
        max_a = int(max_attempts)
        used = int(auto_restarts_used)
        max_r = int(max_auto_restarts)
    except (TypeError, ValueError):
        return False
    if attempts < max_a:
        return False
    if used >= max_r:
        return False
    return True


def heal_exhaust_operator_fields(
    *,
    heal_attempts: int,
    max_attempts: int,
    agent_process_up: bool,
    acp_connected: bool,
    last_error: str | None,
) -> dict[str, Any]:
    """Durable operator fields for heal-exhausted ACP recovery UX."""
    try:
        attempts = int(heal_attempts)
    except (TypeError, ValueError):
        attempts = 0
    try:
        max_a = int(max_attempts)
    except (TypeError, ValueError):
        max_a = 0
    exhausted = (
        attempts >= max_a
        and bool(agent_process_up)
        and not bool(acp_connected)
    )
    return {
        "acpHealExhausted": exhausted,
        "acpHealAttempts": attempts,
        "acpHealError": last_error if exhausted else None,
    }


def agent_recycle_complete(*, process_up: bool, acp_connected: bool) -> bool:
    """True only when process is up AND ACP is chat-connected. Port-only is not enough."""
    return bool(process_up) and bool(acp_connected)


def agent_recycle_reconnect_plan(
    *,
    reconnect_attempts: int = 3,
    reconnect_timeout_s: float = 15.0,
    settle_after_port_s: float = 0.75,
) -> dict[str, Any]:
    """Bounded plan for post-kill ACP reconnect (for tests/docs)."""
    return {
        "reconnect_attempts": max(1, int(reconnect_attempts)),
        "reconnect_timeout_s": max(1.0, float(reconnect_timeout_s)),
        "settle_after_port_s": max(0.0, float(settle_after_port_s)),
        "reset_maintain_loop": True,
    }

_SECRET_KEY_PARTS = (
    "secret", "password", "passwd", "authorization", "credential",
    "apikey", "api_key", "api-key", "private_key",
)
_SECRET_KEY_SUFFIXES = ("_token", "-token", "accesstoken", "refreshtoken")
_NAME_VALUE_LIST_KEYS = frozenset({"env", "headers"})
REDACTED = "[redacted]"


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(p in k for p in _SECRET_KEY_PARTS) or k == "token" or k.endswith(_SECRET_KEY_SUFFIXES)


def redact_for_browser(value: Any, _parent_key: str = "") -> Any:
    """Copy of an agent message safe to send to browsers (ADR-026 amendment).

    Blanks env/header values and secret-named string fields; numbers (token counts) pass.
    """
    if isinstance(value, dict):
        if _parent_key in _NAME_VALUE_LIST_KEYS and "value" in value:
            return {**value, "value": REDACTED}
        out = {}
        for k, v in value.items():
            if isinstance(v, str) and _is_secret_key(str(k)):
                out[k] = REDACTED
            else:
                out[k] = redact_for_browser(v, str(k).lower())
        return out
    if isinstance(value, list):
        return [redact_for_browser(v, _parent_key) for v in value]
    return value
