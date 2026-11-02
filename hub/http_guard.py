"""aiohttp trust boundary for the hub: Host allowlist, same-origin check, token gate.

The agent runs with always-approve, so any request that reaches /ws or a mutating
API can drive the host. Policy and threat model: ADR-026.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from hub.harden_policy import is_public_unauthenticated_path

log = logging.getLogger("hub.guard")

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
AUTH_VIA = web.RequestKey("auth_via", str)
TAILSCALE_IDENTITY = web.RequestKey("tailscale_identity", dict)
PREVIEW_PREFIX = "/preview-site/"
GROK_HOOK_PATH = "/internal/pre-tool-use"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORT_SUFFIXES = (":80", ":443")


@dataclass
class GuardPolicy:
    token: str = ""
    allowed_hosts: frozenset[str] = LOOPBACK_HOSTS
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    # Tailscale Serve name(s) and casefolded logins trusted without a token (ADR-028).
    serve_hosts: frozenset[str] = field(default_factory=frozenset)
    tailscale_logins: frozenset[str] = field(default_factory=frozenset)


def normalize_host(value: str) -> str:
    """Hostname only: no port, no IPv6 brackets, no trailing dot, casefolded."""
    h = str(value or "").strip().casefold()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def _normalize_netloc(value: str) -> str:
    n = str(value or "").strip().casefold().rstrip("/")
    for suffix in _DEFAULT_PORT_SUFFIXES:
        if n.endswith(suffix):
            return n[: -len(suffix)]
    return n


def _normalize_origin(value: str) -> str:
    return str(value or "").strip().casefold().rstrip("/")


def build_allowed_hosts(
    bind_hosts: list[str],
    tailscale_ip: str | None,
    tailscale_dns_names: list[str],
    extra: list[str],
) -> frozenset[str]:
    hosts = set(LOOPBACK_HOSTS)
    hosts.update(normalize_host(h) for h in bind_hosts)
    if tailscale_ip:
        hosts.add(normalize_host(tailscale_ip))
    for name in tailscale_dns_names:
        full = normalize_host(name)
        if full:
            hosts.add(full)
            hosts.add(full.split(".", 1)[0])
    hosts.update(normalize_host(h) for h in extra)
    hosts.discard("")
    return frozenset(hosts)


def host_allowed(host_header: str | None, allowed: frozenset[str]) -> bool:
    if not host_header:
        return True
    return normalize_host(host_header) in allowed


def origin_allowed(
    origin: str | None, host_header: str | None, extra_origins: frozenset[str]
) -> bool:
    """Absent Origin passes (non-browser); otherwise it must name this hub."""
    if origin is None:
        return True
    if _normalize_origin(origin) in extra_origins:
        return True
    netloc = urlsplit(origin.strip()).netloc
    if not netloc or not host_header:
        return False
    return _normalize_netloc(netloc) == _normalize_netloc(host_header)


def needs_origin_check(method: str, path: str) -> bool:
    return path == "/ws" or str(method or "").upper() not in _SAFE_METHODS


def token_matches(presented: str, token: str) -> bool:
    if not presented or not token:
        return False
    return secrets.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))


def request_has_token(request: web.Request, token: str) -> bool:
    if not token:
        return True
    auth = request.headers.get("Authorization") or ""
    if auth[:7].lower() == "bearer " and token_matches(auth[7:].strip(), token):
        return True
    return token_matches(request.rel_url.query.get("token") or "", token)


def _is_loopback(remote: str | None) -> bool:
    try:
        return ipaddress.ip_address(str(remote or "")).is_loopback
    except ValueError:
        return False


def trusted_login(
    remote: str | None, host_header: str | None, login_header: str | None, policy: GuardPolicy
) -> str | None:
    """Login vouched for by Tailscale Serve, which replaces any client-sent copy of the header."""
    login = str(login_header or "").strip()
    if not login or not _is_loopback(remote):
        return None
    if normalize_host(host_header or "") not in policy.serve_hosts:
        return None
    return login if login.casefold() in policy.tailscale_logins else None


def tailscale_owner_login(status: Any) -> str | None:
    if not isinstance(status, dict):
        return None
    uid = (status.get("Self") or {}).get("UserID")
    user = (status.get("User") or {}).get(str(uid)) if uid is not None else None
    return str(user.get("LoginName")) if isinstance(user, dict) and user.get("LoginName") else None


def _is_capability_path(method: str, path: str) -> bool:
    """Paths whose handler checks its own key instead of the browser token."""
    if method == "POST" and path == GROK_HOOK_PATH:
        return True
    return method in ("GET", "HEAD") and path.startswith(PREVIEW_PREFIX)


def _forbidden(reason: str, request: web.Request, detail: str) -> web.Response:
    log.warning("rejected %s %s: %s %r", request.method, request.path, reason, detail)
    return web.json_response({"error": reason}, status=403)


def install_guard(app: web.Application, policy: GuardPolicy) -> None:
    """Register guard middleware and security headers. Reads policy per request."""

    @web.middleware
    async def guard(request: web.Request, handler):
        host = request.headers.get("Host")
        if not host_allowed(host, policy.allowed_hosts):
            return _forbidden("forbidden host", request, host or "")
        if needs_origin_check(request.method, request.path):
            origin = request.headers.get("Origin")
            if not origin_allowed(origin, host, policy.allowed_origins):
                return _forbidden("forbidden origin", request, origin or "")
        login = trusted_login(request.remote, host, request.headers.get("Tailscale-User-Login"), policy)
        if login:
            request[AUTH_VIA] = "tailscale"
            request[TAILSCALE_IDENTITY] = {
                "login": login,
                "name": request.headers.get("Tailscale-User-Name") or "",
            }
        elif request_has_token(request, policy.token):
            request[AUTH_VIA] = "token" if policy.token else "open"
        elif not (
            is_public_unauthenticated_path(request.method, request.path)
            or _is_capability_path(request.method, request.path)
        ):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def security_headers(_request: web.Request, response: web.StreamResponse) -> None:
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")

    app.middlewares.append(guard)
    app.on_response_prepare.append(security_headers)
