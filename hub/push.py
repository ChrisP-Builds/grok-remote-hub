"""Web push for the phone: VAPID keys, subscriptions, notifier and triggers. ADR-028."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from hub.config import PROJECT_ROOT, TAILSCALE_EXE

try:
    import requests
    from py_vapid import Vapid
    from pywebpush import webpush
except ImportError:
    requests = None
    Vapid = None
    webpush = None

log = logging.getLogger("hub.push")

VAPID_FILE = PROJECT_ROOT / "data" / "vapid.json"
SUBSCRIPTIONS_FILE = PROJECT_ROOT / "data" / "push-subscriptions.json"
PRESENCE_WINDOW_S = 120.0
LONG_TURN_S = 60.0
PENDING_MAX_AGE_S = 3600.0
SWEEP_INTERVAL_S = 5.0
DEFAULT_TTL_S = 3600
SEND_TIMEOUT_S = (5.0, 10.0)
SEND_DEADLINE_S = 20.0
SEND_WORKERS = 4
MAX_SUBSCRIPTIONS = 50
GONE_STATUSES = frozenset({401, 403, 404, 410})
PUSH_SERVICE_HOSTS = ("fcm.googleapis.com", "push.apple.com", "push.services.mozilla.com", "notify.windows.com")
FALLBACK_SUB = "mailto:grok-remote-hub@localhost"
CREATE_NO_WINDOW = 0x08000000

PHONE_SETUP_STEPS = (
    "Install Tailscale on the phone and sign in with the same account.",
    "Scan the code or open the address in Safari (iPhone) or Chrome (Android).",
    "Add it to the Home Screen, open it from there and allow notifications.",
)

_TITLES = {
    "approval": "Approval needed: {project}",
    "question": "Grok has a question: {project}",
    "turn_done": "Finished: {project}",
    "turn_failed": "Stopped: {project}",
}
_BODIES = {"approval": "Action needs your approval", "question": "Tap to answer", "turn_done": "Tap to review"}
_FAILURE_CLASSES = (
    ("disconnect", "Agent disconnected"),
    ("max turn duration", "Time limit reached"),
    ("max duration", "Time limit reached"),
    ("stall", "Turn stalled"),
    ("stuck", "Turn stalled"),
    ("no output", "Turn stalled"),
    ("not responding", "Turn stalled"),
    ("reset", "Turn reset"),
    ("cancel", "Turn cancelled"),
)


class VapidKeyError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_or_create_vapid(path: Path = VAPID_FILE) -> dict[str, str]:
    """Keys are created only when the file is absent; anything else raises VapidKeyError."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _create_vapid(path)
    except OSError as exc:
        raise VapidKeyError(f"{path.name} is unreadable") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise VapidKeyError(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict) or not data.get("privateKey") or not data.get("publicKey"):
        raise VapidKeyError(f"{path.name} is missing keys")
    return {"privateKey": str(data["privateKey"]), "publicKey": str(data["publicKey"])}


def _create_vapid(path: Path) -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    keys = {
        "privateKey": _b64url(key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )),
        "publicKey": _public_key_b64(key),
    }
    _write_json_atomic(path, {**keys, "createdAt": _now_iso()})
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return keys


def _public_key_b64(private_key: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return _b64url(private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    ))


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def subscription_origin(via: str | None, identity: dict[str, Any] | None, token: str) -> dict[str, str]:
    if via == "tailscale":
        return {"via": "tailscale", "login": str((identity or {}).get("login") or "")}
    if via == "token" and token:
        return {"via": "token", "tokenFp": token_fingerprint(token)}
    return {"via": "open"}


def _is_push_service(endpoint: str) -> bool:
    try:
        host = (urlparse(endpoint).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in PUSH_SERVICE_HOSTS)


def _clean_subscription(subscription: Any) -> dict[str, Any] | None:
    if not isinstance(subscription, dict):
        return None
    endpoint, keys = subscription.get("endpoint"), subscription.get("keys")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 2048:
        return None
    if not _is_push_service(endpoint) or not isinstance(keys, dict):
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not all(isinstance(v, str) and 0 < len(v) <= 256 for v in (p256dh, auth)):
        return None
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


class SubscriptionStore:
    """Mutate only from the event loop thread; senders return dead endpoints instead."""

    def __init__(self, path: Path = SUBSCRIPTIONS_FILE) -> None:
        self.path = Path(path)

    def all(self) -> list[dict[str, Any]]:
        data = _read_json(self.path)
        if not isinstance(data, list):
            return []
        return [s for s in data if _clean_subscription(s)]

    def add(self, subscription: Any, device: str, origin: dict[str, str] | None = None) -> bool:
        clean = _clean_subscription(subscription)
        if clean is None:
            return False
        entry = {**clean, "device": str(device or "")[:80], "addedAt": _now_iso(), **(origin or {"via": "open"})}
        items = self.all()
        index = next((i for i, s in enumerate(items) if s["endpoint"] == clean["endpoint"]), None)
        if index is None:
            items.append(entry)
        else:
            items[index] = entry
        _write_json_atomic(self.path, items[-MAX_SUBSCRIPTIONS:])
        return True

    def remove(self, endpoint: str) -> bool:
        return self._remove_where(lambda s: s["endpoint"] == endpoint) > 0

    def remove_many(self, endpoints: Iterable[str]) -> int:
        gone = set(endpoints)
        return self._remove_where(lambda s: s["endpoint"] in gone)

    def drop_stale_token_subscriptions(self, token: str) -> int:
        current = token_fingerprint(token) if token else None
        return self._remove_where(lambda s: s.get("via") == "token" and s.get("tokenFp") != current)

    def _remove_where(self, doomed: Callable[[dict[str, Any]], bool]) -> int:
        items = self.all()
        kept = [s for s in items if not doomed(s)]
        if len(kept) != len(items):
            _write_json_atomic(self.path, kept)
        return len(items) - len(kept)


def _no_redirect_session() -> Any:
    session = requests.Session()
    session.max_redirects = 0
    return session


class PushNotifier:
    def __init__(
        self,
        store: SubscriptionStore,
        vapid_path: Path = VAPID_FILE,
        *,
        sender: Callable[..., Any] | None = webpush,
        sub_claim: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.store = store
        self._vapid_path = Path(vapid_path)
        self._sender = sender
        self._sub_claim = sub_claim
        self._keys: dict[str, str] | None = None
        self._vapid: Any = None
        self._reason: str | None = None
        self._executor: ThreadPoolExecutor | None = None

    def _ready(self) -> bool:
        if self._vapid is not None:
            return True
        if self._sender is None or Vapid is None or requests is None:
            self._reason = "pywebpush is not installed"
            return False
        try:
            keys = load_or_create_vapid(self._vapid_path)
            vapid = Vapid.from_string(keys["privateKey"])
            if _public_key_b64(vapid.private_key) != keys["publicKey"]:
                raise VapidKeyError(f"{self._vapid_path.name} public key does not match the private key")
        except VapidKeyError as exc:
            return self._disable(str(exc))
        except Exception:
            return self._disable(f"{self._vapid_path.name} private key is invalid")
        self._keys, self._vapid, self._reason = keys, vapid, None
        return True

    def _disable(self, reason: str) -> bool:
        log.warning("web push disabled: %s", reason)
        self._reason = reason
        return False

    def config(self) -> dict[str, Any]:
        ready = self._ready()
        return {
            "enabled": ready,
            "publicKey": self._keys["publicKey"] if ready and self._keys else None,
            "reason": self._reason,
        }

    def close(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _claims(self) -> dict[str, str]:
        # py_vapid rejects an https sub carrying a port, path or trailing slash.
        url = urlparse(str(self._sub_claim() or ""))
        return {"sub": f"https://{url.hostname}" if url.scheme == "https" and url.hostname else FALLBACK_SUB}

    async def send(self, payload: dict[str, Any], *, ttl: int = DEFAULT_TTL_S, urgency: str = "normal") -> int:
        if not self._ready():
            return 0
        subscriptions = self.store.all()
        if not subscriptions:
            return 0
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=SEND_WORKERS, thread_name_prefix="hub-push")
        pool, data = self._executor, json.dumps(payload)
        results = await asyncio.gather(
            *(self._send_with_deadline(pool, s, data, ttl, urgency) for s in subscriptions)
        )
        dead = [s["endpoint"] for s, r in zip(subscriptions, results) if r == "gone"]
        if dead:
            log.info("push: pruned %d dead subscription(s)", self.store.remove_many(dead))
        return results.count("ok")

    async def _send_with_deadline(
        self, pool: ThreadPoolExecutor, subscription: dict[str, Any], data: str, ttl: int, urgency: str,
    ) -> str:
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(pool, self._send_one, subscription, data, ttl, urgency)
            return await asyncio.wait_for(future, SEND_DEADLINE_S)
        except (asyncio.TimeoutError, RuntimeError):
            log.warning("push to %s gave up after %.0f s", urlparse(subscription["endpoint"]).netloc, SEND_DEADLINE_S)
            return "failed"

    def _send_one(self, subscription: dict[str, Any], data: str, ttl: int, urgency: str) -> str:
        try:
            with _no_redirect_session() as session:
                # webpush mutates vapid_claims (aud, exp), so each call needs its own dict.
                self._sender(
                    subscription_info={"endpoint": subscription["endpoint"], "keys": subscription["keys"]},
                    data=data,
                    vapid_private_key=self._vapid,
                    vapid_claims=self._claims(),
                    ttl=ttl,
                    timeout=SEND_TIMEOUT_S,
                    headers={"Urgency": urgency},
                    requests_session=session,
                )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in GONE_STATUSES:
                return "gone"
            host = urlparse(subscription["endpoint"]).netloc
            log.warning("push to %s failed: %s %s", host, type(exc).__name__, status or "")
            return "failed"
        return "ok"


def _project_name(cwd: str | None) -> str:
    parts = [p for p in re.split(r"[\\/]+", str(cwd or "")) if p and not p.endswith(":")]
    return parts[-1] if parts else "Grok"


def failure_class(error: str | None) -> str:
    text = str(error or "").lower()
    return next((label for needle, label in _FAILURE_CLASSES if needle in text), "Turn ended with an error")


def build_notification(
    kind: str,
    session_id: str,
    cwd: str | None,
    *,
    category: str | None = None,
    error: str | None = None,
    tag_id: str | None = None,
) -> dict[str, Any]:
    if kind == "approval":
        body, tag = str(category or _BODIES["approval"])[:60], f"approval-{tag_id or session_id}"
    elif kind == "turn_failed":
        body, tag = failure_class(error), f"turn-{session_id}"
    elif kind == "turn_done":
        body, tag = _BODIES["turn_done"], f"turn-{session_id}"
    else:
        body, tag = _BODIES["question"], f"question-{session_id}"
    return {
        "title": _TITLES[kind].format(project=_project_name(cwd)[:60]),
        "body": body,
        "tag": tag,
        "url": "/?session=" + quote(session_id, safe=""),
        "sessionId": session_id,
        "kind": kind,
    }


class PresenceTracker:
    def __init__(self, window_s: float = PRESENCE_WINDOW_S) -> None:
        self._window = window_s
        self._entries: dict[Any, tuple[bool, str | None, float]] = {}

    def update(self, key: Any, visible: bool, session_id: str | None, now: float) -> None:
        self._entries[key] = (bool(visible), session_id or None, now)

    def drop(self, key: Any) -> None:
        self._entries.pop(key, None)

    def is_visible(self, session_ids: Iterable[str], now: float) -> bool:
        wanted = set(session_ids)
        return any(
            visible and sid in wanted and now - seen <= self._window
            for visible, sid, seen in self._entries.values()
        )


@dataclass(frozen=True)
class Push:
    note: dict[str, Any]
    urgency: str
    ttl: int


@dataclass
class _Pending:
    session_id: str
    make: Callable[[], dict[str, Any]]
    urgency: str
    created: float
    deadline: float | None


@dataclass
class _Turn:
    started: float
    view_id: str
    ids: set[str] = field(default_factory=set)


def _question_note(session_id: str, cwd_for: Callable[[str], str | None]) -> dict[str, Any]:
    return build_notification("question", session_id, cwd_for(session_id))


def _seconds_left(payload: dict[str, Any]) -> float | None:
    try:
        return float(payload["expiresAt"]) - float(payload["serverNow"])
    except (KeyError, TypeError, ValueError):
        return None


class PushTriggers:
    """Turns hub broadcasts into pushes; items seen while visible wait for sweep()."""

    EVENT_TYPES = frozenset({"approval_request", "approval_resolved", "user_question", "user_question_resolved", "turn"})

    def __init__(self, presence: PresenceTracker) -> None:
        self.presence = presence
        self._turns: dict[str, _Turn] = {}
        self._pending: dict[str, _Pending] = {}

    def observe(self, payload: dict[str, Any], cwd_for: Callable[[str], str | None], now: float) -> list[Push]:
        typ, sid = payload.get("type"), str(payload.get("sessionId") or "")
        if typ == "approval_request" and sid:
            left = _seconds_left(payload)
            make = partial(
                build_notification, "approval", sid, payload.get("cwd"),
                category=payload.get("category"), tag_id=payload.get("id"),
            )
            deadline = now + left if left is not None else None
            return self._offer(f"approval:{payload.get('id')}", _Pending(sid, make, "high", now, deadline), now)
        if typ == "user_question" and sid:
            make = partial(_question_note, sid, cwd_for)
            key = f"question:{payload.get('requestId') or sid}"
            return self._offer(key, _Pending(sid, make, "high", now, None), now)
        if typ == "approval_resolved":
            self._pending.pop(f"approval:{payload.get('id')}", None)
        elif typ == "user_question_resolved":
            self._pending.pop(f"question:{payload.get('requestId')}", None)
        elif typ == "turn" and payload.get("state") == "idle" and sid:
            return self._turn_ended(sid, payload.get("error"), cwd_for, now)
        return []

    def sweep(self, now: float) -> list[Push]:
        pushes = []
        for key, item in list(self._pending.items()):
            expired = now - item.created > PENDING_MAX_AGE_S or (item.deadline is not None and item.deadline <= now)
            if not expired and self.presence.is_visible([item.session_id], now):
                continue
            del self._pending[key]
            push = None if expired else self._push(item, now)
            if push:
                pushes.append(push)
        return pushes

    def _offer(self, key: str, item: _Pending, now: float) -> list[Push]:
        if self.presence.is_visible([item.session_id], now):
            self._pending[key] = item
            return []
        push = self._push(item, now)
        return [push] if push else []

    @staticmethod
    def _push(item: _Pending, now: float) -> Push | None:
        ttl = DEFAULT_TTL_S if item.deadline is None else int(item.deadline - now)
        return Push(item.make(), item.urgency, ttl) if ttl > 0 else None

    def turn_started(self, session_id: str, view_id: str | None, now: float) -> None:
        ids = {i for i in (session_id, view_id) if i}
        turn = next((self._turns[i] for i in ids if i in self._turns), None)
        if turn is None:
            turn = _Turn(started=now, view_id=view_id or session_id)
        turn.ids |= ids
        for i in ids:
            self._turns[i] = turn

    def _turn_ended(
        self, session_id: str, error: str | None, cwd_for: Callable[[str], str | None], now: float,
    ) -> list[Push]:
        turn = self._turns.get(session_id)
        ids = (turn.ids if turn else set()) | {session_id}
        for key in [k for k, p in self._pending.items() if p.session_id in ids]:
            del self._pending[key]
        if turn is None:
            return []
        for i in turn.ids:
            self._turns.pop(i, None)
        if now - turn.started < LONG_TURN_S or self.presence.is_visible(turn.ids, now):
            return []
        cwd = next((c for c in map(cwd_for, [turn.view_id, *sorted(turn.ids)]) if c), None)
        note = build_notification("turn_failed" if error else "turn_done", turn.view_id, cwd, error=error)
        return [Push(note, "normal", DEFAULT_TTL_S)]


def qr_svg_data_uri(url: str | None) -> str | None:
    if not url:
        return None
    import io

    import segno

    buf = io.BytesIO()
    segno.make(url, micro=False, error="m").save(buf, kind="svg", scale=6, border=2)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def serve_active_from_status(status: Any, port: int) -> bool:
    web_cfg = status.get("Web") if isinstance(status, dict) else None
    if not isinstance(web_cfg, dict):
        return False
    proxies = [
        str(handler.get("Proxy") or "")
        for site in web_cfg.values() if isinstance(site, dict)
        for handler in (site.get("Handlers") or {}).values() if isinstance(handler, dict)
    ]
    return any(p == str(port) or p.rstrip("/").endswith(f":{port}") for p in proxies)


def read_serve_active(port: int) -> bool:
    try:
        proc = subprocess.run(
            [str(TAILSCALE_EXE), "serve", "status", "--json"],
            capture_output=True, text=True, timeout=5, check=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return proc.returncode == 0 and serve_active_from_status(json.loads(proc.stdout or "{}"), port)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
