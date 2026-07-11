"""Weekly Grok plan usage via cli-chat-proxy billing credits.

Reads local ~/.grok/auth.json, refreshes an OIDC access token, and GETs
/v1/billing?format=credits. Tokens are never logged or returned to clients.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("hub.billing_usage")

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
CLIENT_VERSION = "0.2.93"
BILLING_TTL_S = 90.0
TOKEN_SKEW_S = 60.0
HTTP_TIMEOUT_S = 20.0

_lock = threading.Lock()
_token_cache: dict[str, Any] = {
    "access_token": None,
    "expires_at": 0.0,
    "user_id": None,
}
_billing_cache: dict[str, Any] = {
    "payload": None,
    "fetched_at": 0.0,
}


def _unavailable(error: str) -> dict[str, Any]:
    return {
        "weeklyPercent": None,
        "periodType": None,
        "periodStart": None,
        "periodEnd": None,
        "product": None,
        "available": False,
        "error": error,
    }


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strip_period_type(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    prefix = "USAGE_PERIOD_TYPE_"
    upper = s.upper()
    if upper.startswith(prefix):
        upper = upper[len(prefix) :]
    return upper or None


def normalize_credits_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Pure normalize of billing `config` object into the plan usage shape."""
    if not isinstance(config, dict):
        return _unavailable("invalid config")

    weekly: float | None = None
    product: str | None = None
    product_usage = config.get("productUsage")
    if isinstance(product_usage, list):
        for item in product_usage:
            if not isinstance(item, dict):
                continue
            name = item.get("product")
            if name == "GrokBuild":
                product = "GrokBuild"
                weekly = _as_float(item.get("usagePercent"))
                break
    if weekly is None:
        weekly = _as_float(config.get("creditUsagePercent"))

    period_type: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    current = config.get("currentPeriod")
    if isinstance(current, dict):
        period_type = _strip_period_type(current.get("type"))
        start = current.get("start")
        end = current.get("end")
        if isinstance(start, str) and start.strip():
            period_start = start.strip()
        if isinstance(end, str) and end.strip():
            period_end = end.strip()

    if period_start is None:
        bp = config.get("billingPeriodStart")
        if isinstance(bp, str) and bp.strip():
            period_start = bp.strip()
    if period_end is None:
        bp = config.get("billingPeriodEnd")
        if isinstance(bp, str) and bp.strip():
            period_end = bp.strip()

    if weekly is None:
        out = _unavailable("no usage percent")
        out["periodType"] = period_type
        out["periodStart"] = period_start
        out["periodEnd"] = period_end
        out["product"] = product
        return out

    if weekly < 0:
        weekly = 0.0
    elif weekly > 100:
        weekly = 100.0

    return {
        "weeklyPercent": weekly,
        "periodType": period_type,
        "periodStart": period_start,
        "periodEnd": period_end,
        "product": product or "GrokBuild",
        "available": True,
        "error": None,
    }


def _auth_path() -> Path:
    return Path.home() / ".grok" / "auth.json"


def _load_auth_entry() -> dict[str, Any] | None:
    path = _auth_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    for _key, value in raw.items():
        if isinstance(value, dict) and value.get("refresh_token"):
            return value
    return None


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _discover_token_endpoint(issuer: str) -> str:
    base = issuer.rstrip("/")
    fallback = base + "/oauth2/token"
    url = base + "/.well-known/openid-configuration"
    try:
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=HTTP_TIMEOUT_S) as resp:
            conf = json.loads(resp.read().decode("utf-8"))
        if isinstance(conf, dict):
            endpoint = conf.get("token_endpoint")
            if isinstance(endpoint, str) and endpoint.strip():
                return endpoint.strip()
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
        log.debug("oidc discovery failed: %s", type(exc).__name__)
    return fallback


def _refresh_access_token(entry: dict[str, Any]) -> tuple[str, float, str]:
    """Return (access_token, expires_at_epoch, user_id). Never logs secrets."""
    issuer = str(entry.get("oidc_issuer") or "https://auth.x.ai").rstrip("/")
    client_id = str(entry.get("oidc_client_id") or "")
    refresh = str(entry.get("refresh_token") or "")
    user_id = str(entry.get("user_id") or entry.get("principal_id") or "")
    if not refresh or not client_id:
        raise RuntimeError("auth incomplete")

    token_url = _discover_token_endpoint(issuer)
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=HTTP_TIMEOUT_S) as resp:
            tok = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("token refresh HTTP %s", exc.code)
        raise RuntimeError(f"token refresh HTTP {exc.code}") from exc
    except Exception as exc:
        log.warning("token refresh failed: %s", type(exc).__name__)
        raise RuntimeError("token refresh failed") from exc

    if not isinstance(tok, dict):
        raise RuntimeError("token response invalid")
    access = tok.get("access_token")
    if not isinstance(access, str) or not access:
        raise RuntimeError("no access token")
    expires_in = _as_float(tok.get("expires_in")) or 3600.0
    expires_at = time.time() + max(30.0, expires_in - TOKEN_SKEW_S)
    return access, expires_at, user_id


def _get_access_token() -> tuple[str, str]:
    """Cached access token + user_id. Thread-safe."""
    now = time.time()
    with _lock:
        cached = _token_cache.get("access_token")
        exp = float(_token_cache.get("expires_at") or 0.0)
        uid = str(_token_cache.get("user_id") or "")
        if isinstance(cached, str) and cached and now < exp:
            return cached, uid

    entry = _load_auth_entry()
    if not entry:
        raise RuntimeError("no local grok auth")

    access, expires_at, user_id = _refresh_access_token(entry)
    with _lock:
        _token_cache["access_token"] = access
        _token_cache["expires_at"] = expires_at
        _token_cache["user_id"] = user_id
    return access, user_id


def _http_get_billing(access: str, user_id: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "Accept": "application/json",
        "x-grok-client-version": CLIENT_VERSION,
        "User-Agent": "Grok-Remote-Hub/0.1",
    }
    if user_id:
        headers["x-userid"] = user_id
    req = urllib.request.Request(BILLING_URL, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        log.warning("billing HTTP %s", exc.code)
        raise RuntimeError(f"billing HTTP {exc.code}") from exc
    except Exception as exc:
        log.warning("billing fetch failed: %s", type(exc).__name__)
        raise RuntimeError("billing fetch failed") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("billing parse failed") from exc
    if not isinstance(data, dict):
        raise RuntimeError("billing response invalid")
    return data


def fetch_credits_usage(*, force: bool = False) -> dict[str, Any]:
    """Return normalized weekly plan usage. Safe for API responses (no tokens)."""
    now = time.time()
    with _lock:
        cached = _billing_cache.get("payload")
        fetched_at = float(_billing_cache.get("fetched_at") or 0.0)
        if (
            not force
            and isinstance(cached, dict)
            and cached.get("available")
            and (now - fetched_at) < BILLING_TTL_S
        ):
            return dict(cached)
        # Also serve recent negative cache briefly to avoid hammering on auth miss
        if (
            not force
            and isinstance(cached, dict)
            and not cached.get("available")
            and (now - fetched_at) < min(30.0, BILLING_TTL_S)
        ):
            return dict(cached)

    try:
        access, user_id = _get_access_token()
        data = _http_get_billing(access, user_id)
        config = data.get("config") if isinstance(data, dict) else None
        result = normalize_credits_config(config if isinstance(config, dict) else None)
    except RuntimeError as exc:
        result = _unavailable(str(exc) or "unavailable")
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_credits_usage failed: %s", type(exc).__name__)
        result = _unavailable("unavailable")

    with _lock:
        _billing_cache["payload"] = dict(result)
        _billing_cache["fetched_at"] = time.time()
    return result


def clear_caches() -> None:
    """Test helper: drop token and billing caches."""
    with _lock:
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0.0
        _token_cache["user_id"] = None
        _billing_cache["payload"] = None
        _billing_cache["fetched_at"] = 0.0
