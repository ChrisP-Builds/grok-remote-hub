"""Web push: VAPID keys, subscription store, notifier, triggers (ADR-028)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from py_vapid import Vapid
from pywebpush import WebPushException

import hub.push as push
from hub.push import (
    PresenceTracker,
    PushNotifier,
    PushTriggers,
    SubscriptionStore,
    VapidKeyError,
    build_notification,
    load_or_create_vapid,
    qr_svg_data_uri,
    serve_active_from_status,
    subscription_origin,
    token_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
CWD = r"D:\Projects\Secret Client\alpha-app"
COMMAND = "git push --force origin main"
FCM = "https://fcm.googleapis.com/fcm/send/"


def _sub(endpoint: str) -> dict:
    return {"endpoint": endpoint, "keys": {"p256dh": "pk-" + endpoint[-1], "auth": "au"}}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _browser_subscription(endpoint: str = "https://web.push.apple.com/QAbc") -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {"endpoint": endpoint, "keys": {"p256dh": _b64url(point), "auth": _b64url(b"0" * 16)}}


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = ""
        self.headers: dict = {}


# VAPID keys


def test_vapid_keys_are_created_once_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "data" / "vapid.json"
    first = load_or_create_vapid(path)
    second = load_or_create_vapid(path)
    assert first == second
    point = _b64url_decode(first["publicKey"])
    assert len(point) == 65 and point[0] == 4
    assert Vapid.from_string(first["privateKey"]).private_key is not None


@pytest.mark.parametrize("content", ["{not json", '{"publicKey": "only"}', "[]"])
def test_unreadable_vapid_file_is_never_overwritten(tmp_path: Path, content: str) -> None:
    path = tmp_path / "vapid.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(VapidKeyError):
        load_or_create_vapid(path)
    notifier = PushNotifier(SubscriptionStore(tmp_path / "s.json"), path, sender=lambda **kw: _Resp(201))
    config = notifier.config()
    assert config["enabled"] is False and config["publicKey"] is None and "vapid.json" in config["reason"]
    assert path.read_text(encoding="utf-8") == content


def test_invalid_private_key_disables_push(tmp_path: Path) -> None:
    path = tmp_path / "vapid.json"
    path.write_text(json.dumps({"privateKey": "AAAA", "publicKey": "BBBB"}), encoding="utf-8")
    notifier = PushNotifier(SubscriptionStore(tmp_path / "s.json"), path, sender=lambda **kw: _Resp(201))
    assert notifier.config()["enabled"] is False
    assert asyncio.run(notifier.send({"title": "t"})) == 0


# Subscription store


def test_store_dedupes_by_endpoint_and_keeps_latest(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "subs.json")
    store.add(_sub(FCM + "a"), "iPhone", {"via": "open"})
    store.add(_sub(FCM + "a"), "iPhone 2", {"via": "tailscale", "login": "me@x"})
    store.add(_sub(FCM + "b"), "Pixel", {"via": "open"})
    items = SubscriptionStore(tmp_path / "subs.json").all()
    assert [s["endpoint"] for s in items] == [FCM + "a", FCM + "b"]
    assert items[0]["device"] == "iPhone 2" and items[0]["via"] == "tailscale" and items[0]["login"] == "me@x"
    assert {"endpoint", "keys", "device", "addedAt", "via"} <= set(items[0])
    assert store.remove(FCM + "a") is True
    assert store.remove(FCM + "a") is False


def test_store_rejects_malformed_subscription(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "subs.json")
    assert store.add({"endpoint": "http://fcm.googleapis.com/x", "keys": {"p256dh": "a", "auth": "b"}}, "x") is False
    assert store.add({"endpoint": FCM + "a", "keys": {}}, "x") is False
    assert store.all() == []


@pytest.mark.parametrize("endpoint, ok", [
    ("https://fcm.googleapis.com/fcm/send/x", True),
    ("https://web.push.apple.com/QAbc", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
    ("https://wns2-par02p.notify.windows.com/w/?token=x", True),
    ("https://evil.example/fcm.googleapis.com", False),
    ("https://push.apple.com.evil.net/x", False),
    ("https://evilpush.apple.com/x", False),
    ("https://127.0.0.1/x", False),
])
def test_store_accepts_only_real_push_service_hosts(tmp_path: Path, endpoint: str, ok: bool) -> None:
    store = SubscriptionStore(tmp_path / "subs.json")
    assert store.add(_sub(endpoint), "d") is ok


def test_store_writes_through_temp_file_and_replace(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    real_replace = push.os.replace

    def spy(src, dst):
        calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(push.os, "replace", spy)
    path = tmp_path / "subs.json"
    SubscriptionStore(path).add(_sub(FCM + "a"), "d")
    assert calls and calls[0][1] == path and calls[0][0] != path
    assert json.loads(path.read_text(encoding="utf-8"))[0]["endpoint"] == FCM + "a"
    assert list(tmp_path.iterdir()) == [path]


def test_subscription_origin_records_how_it_was_created() -> None:
    assert subscription_origin("tailscale", {"login": "me@x", "name": "Me"}, "tok") == {"via": "tailscale", "login": "me@x"}
    assert subscription_origin("token", None, "tok") == {"via": "token", "tokenFp": token_fingerprint("tok")}
    assert subscription_origin("open", None, "") == {"via": "open"}
    assert subscription_origin(None, None, "") == {"via": "open"}
    assert token_fingerprint("tok") != token_fingerprint("other") and "tok" not in token_fingerprint("tok")
    assert len(token_fingerprint("tok")) == 16


def test_token_subscriptions_from_an_old_token_are_dropped(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "subs.json")
    store.add(_sub(FCM + "a"), "old", subscription_origin("token", None, "old-token"))
    store.add(_sub(FCM + "b"), "new", subscription_origin("token", None, "new-token"))
    store.add(_sub(FCM + "c"), "ts", subscription_origin("tailscale", {"login": "me@x"}, "old-token"))
    store.add(_sub(FCM + "d"), "open", subscription_origin("open", None, ""))
    assert store.drop_stale_token_subscriptions("new-token") == 1
    assert [s["endpoint"][-1] for s in store.all()] == ["b", "c", "d"]
    assert store.drop_stale_token_subscriptions("") == 1
    assert [s["endpoint"][-1] for s in store.all()] == ["c", "d"]


# Notifier


def _notifier(tmp_path: Path, sender, serve_url=None, ends="abcdef") -> PushNotifier:
    store = SubscriptionStore(tmp_path / "subs.json")
    for end in ends:
        store.add(_sub(FCM + end), end)
    return PushNotifier(store, tmp_path / "vapid.json", sender=sender, sub_claim=lambda: serve_url)


def test_send_prunes_dead_subscriptions_and_survives_other_failures(tmp_path: Path) -> None:
    outcomes = {"a": 410, "b": 404, "c": 500, "e": 401, "f": 403}

    def sender(**kw):
        status = outcomes.get(kw["subscription_info"]["endpoint"][-1])
        if status:
            raise WebPushException("Push failed", response=_Resp(status))
        return _Resp(201)

    notifier = _notifier(tmp_path, sender)
    assert asyncio.run(notifier.send({"title": "t"})) == 1
    assert [s["endpoint"][-1] for s in notifier.store.all()] == ["c", "d"]
    notifier.close()


def test_send_survives_transport_errors(tmp_path: Path) -> None:
    def sender(**kw):
        raise ConnectionError("offline")

    notifier = _notifier(tmp_path, sender, ends="abcd")
    assert asyncio.run(notifier.send({"title": "t"})) == 0
    assert len(notifier.store.all()) == 4
    notifier.close()


def test_send_passes_vapid_ttl_urgency_session_and_fresh_claims(tmp_path: Path) -> None:
    calls: list = []

    def sender(**kw):
        calls.append(kw)
        kw["vapid_claims"]["aud"] = "mutated by pywebpush"
        return _Resp(201)

    notifier = _notifier(tmp_path, sender, serve_url="https://desk.tailnet.ts.net/", ends="abcd")
    asyncio.run(notifier.send({"title": "t"}, ttl=600, urgency="high"))
    assert len(calls) == 4
    for kw in calls:
        assert kw["vapid_claims"]["sub"] == "https://desk.tailnet.ts.net"
        assert kw["ttl"] == 600 and kw["headers"]["Urgency"] == "high"
        assert kw["timeout"] and isinstance(kw["vapid_private_key"], Vapid)
        assert kw["requests_session"].max_redirects == 0
        assert json.loads(kw["data"]) == {"title": "t"}
    assert len({id(kw["vapid_claims"]) for kw in calls}) == 4
    notifier.close()


def test_send_runs_subscriptions_concurrently(tmp_path: Path) -> None:
    barrier = threading.Barrier(4, timeout=3)

    def sender(**kw):
        barrier.wait()
        return _Resp(201)

    notifier = _notifier(tmp_path, sender, ends="abcd")
    assert asyncio.run(notifier.send({"title": "t"})) == 4
    notifier.close()


def test_send_gives_up_on_a_send_past_the_deadline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(push, "SEND_DEADLINE_S", 0.2)

    def sender(**kw):
        if kw["subscription_info"]["endpoint"].endswith("a"):
            time.sleep(1.0)
        return _Resp(201)

    notifier = _notifier(tmp_path, sender, ends="ab")
    started = time.monotonic()
    assert asyncio.run(notifier.send({"title": "t"})) == 1
    assert time.monotonic() - started < 0.9
    assert len(notifier.store.all()) == 2
    notifier.close()


def test_real_send_does_not_follow_redirects(tmp_path: Path) -> None:
    hits: list = []

    async def redirect(request: web.Request) -> web.Response:
        hits.append("redirect")
        raise web.HTTPTemporaryRedirect("/landed")

    async def landed(request: web.Request) -> web.Response:
        hits.append("landed")
        return web.Response(status=201)

    from pywebpush import webpush

    app = web.Application()
    app.router.add_post("/push", redirect)
    app.router.add_post("/landed", landed)
    notifier = PushNotifier(SubscriptionStore(tmp_path / "s.json"), tmp_path / "vapid.json", sender=webpush)
    assert notifier.config()["enabled"]

    async def run() -> str:
        async with TestServer(app) as server:
            sub = _browser_subscription(str(server.make_url("/push")))
            return await asyncio.to_thread(notifier._send_one, sub, "{}", 60, "normal")

    assert asyncio.run(run()) == "failed"
    assert hits == ["redirect"]
    notifier.close()


def test_real_pywebpush_signs_with_serve_url_and_fallback_claims(tmp_path: Path, monkeypatch) -> None:
    from pywebpush import webpush

    monkeypatch.chdir(tmp_path)  # pywebpush curl mode writes encrypted.data to the cwd
    for serve_url in ("https://desk.tailnet.ts.net/", None):
        curls: list = []
        store = SubscriptionStore(tmp_path / f"subs-{bool(serve_url)}.json")
        store.add(_browser_subscription(), "phone")
        notifier = PushNotifier(
            store, tmp_path / "vapid.json",
            sender=lambda **kw: curls.append(webpush(**kw, curl=True)) or "curl",
            sub_claim=lambda: serve_url,
        )
        assert asyncio.run(notifier.send({"title": "t"}, ttl=300, urgency="high")) == 1
        assert "vapid t=" in curls[0] and "urgency: high" in curls[0].lower() and "ttl: 300" in curls[0].lower()
        notifier.close()


def test_claim_falls_back_to_mailto_without_https_serve_url(tmp_path: Path) -> None:
    claims: list = []
    notifier = _notifier(tmp_path, lambda **kw: claims.append(kw["vapid_claims"]) or _Resp(201), ends="a")
    asyncio.run(notifier.send({"title": "t"}))
    assert claims[0]["sub"] == "mailto:grok-remote-hub@localhost"
    notifier.close()


def test_notifier_reports_disabled_without_library(tmp_path: Path) -> None:
    notifier = PushNotifier(SubscriptionStore(tmp_path / "s.json"), tmp_path / "v.json", sender=None)
    config = notifier.config()
    assert config["enabled"] is False and config["reason"]
    assert asyncio.run(notifier.send({"title": "t"})) == 0
    assert not (tmp_path / "v.json").exists()


def test_notifier_config_exposes_public_key(tmp_path: Path) -> None:
    notifier = _notifier(tmp_path, lambda **kw: _Resp(201))
    config = notifier.config()
    assert config["enabled"] is True and config["reason"] is None
    assert config["publicKey"] == load_or_create_vapid(tmp_path / "vapid.json")["publicKey"]


# Notification text


def test_notification_text_names_project_and_category_only() -> None:
    note = build_notification("approval", "s1", CWD, category="Shell command", tag_id="ab12")
    assert note == {
        "title": "Approval needed: alpha-app",
        "body": "Shell command",
        "tag": "approval-ab12",
        "url": "/?session=s1",
        "sessionId": "s1",
        "kind": "approval",
    }
    assert build_notification("question", "s1", CWD)["title"] == "Grok has a question: alpha-app"
    assert build_notification("question", "s1", CWD)["tag"] == "question-s1"
    assert build_notification("turn_done", "s1", CWD)["title"] == "Finished: alpha-app"
    assert build_notification("turn_failed", "s1", CWD)["tag"] == "turn-s1"


def test_failed_turn_body_is_a_reason_class_not_the_error_text() -> None:
    err = f"agent crashed running {COMMAND} in {CWD}"
    note = build_notification("turn_failed", "s1", CWD, error=err)
    assert note["title"] == "Stopped: alpha-app"
    assert COMMAND not in note["body"] and CWD not in note["body"] and "crashed" not in note["body"]
    assert build_notification("turn_failed", "s1", CWD, error="ACP disconnected")["body"] == "Agent disconnected"


# Triggers


def _cwd(sid: str) -> str:
    return CWD


def _approval(approval_id: str = "ab12", sid: str = "s1", ttl: float = 300.0) -> dict:
    return {
        "type": "approval_request", "id": approval_id, "sessionId": sid, "tool": "run_terminal_command",
        "kind": "shell", "summary": COMMAND, "cwd": CWD, "category": "Shell command",
        "createdAt": 1000.0, "expiresAt": 1000.0 + ttl, "serverNow": 1000.0,
    }


def _question(request_id: str = "q1", sid: str = "s1") -> dict:
    return {"type": "user_question", "requestId": request_id, "sessionId": sid, "questions": [{"q": COMMAND}]}


def test_approval_push_never_leaks_command_or_path() -> None:
    pushes = PushTriggers(PresenceTracker()).observe(_approval(), _cwd, now=0.0)
    assert len(pushes) == 1
    text = json.dumps(pushes[0].note)
    assert COMMAND not in text and "Secret Client" not in text and "D:" not in text
    assert pushes[0].note["title"] == "Approval needed: alpha-app" and pushes[0].note["tag"] == "approval-ab12"
    assert pushes[0].urgency == "high" and pushes[0].ttl == 300


def test_question_push_has_no_question_text() -> None:
    pushes = PushTriggers(PresenceTracker()).observe(_question(), _cwd, now=0.0)
    assert len(pushes) == 1 and COMMAND not in json.dumps(pushes[0].note)
    assert pushes[0].note["title"] == "Grok has a question: alpha-app" and pushes[0].urgency == "high"


def test_visible_session_defers_until_the_sweep_sees_it_hidden() -> None:
    presence = PresenceTracker()
    triggers = PushTriggers(presence)
    presence.update("ws1", True, "s1", now=0.0)
    assert triggers.observe(_approval(), _cwd, now=1.0) == []
    assert triggers.observe(_question(), _cwd, now=1.0) == []
    assert triggers.sweep(now=6.0) == []
    presence.update("ws1", False, "s1", now=10.0)
    swept = triggers.sweep(now=11.0)
    assert sorted(p.note["kind"] for p in swept) == ["approval", "question"]
    assert triggers.sweep(now=16.0) == []
    presence.update("ws1", True, "s1", now=20.0)
    presence.drop("ws1")
    assert triggers.sweep(now=21.0) == []


def test_sweep_pushes_after_presence_goes_stale_or_socket_drops() -> None:
    presence = PresenceTracker()
    triggers = PushTriggers(presence)
    presence.update("ws1", True, "s1", now=100.0)
    triggers.observe(_approval("a1"), _cwd, now=100.0)
    assert triggers.sweep(now=219.0) == []
    assert [p.note["tag"] for p in triggers.sweep(now=221.0)] == ["approval-a1"]

    presence.update("ws2", True, "s1", now=300.0)
    triggers.observe(_question("q2"), _cwd, now=300.0)
    presence.drop("ws2")
    assert [p.note["tag"] for p in triggers.sweep(now=301.0)] == ["question-s1"]


def test_resolved_items_are_never_swept() -> None:
    presence = PresenceTracker()
    triggers = PushTriggers(presence)
    presence.update("ws1", True, "s1", now=0.0)
    triggers.observe(_approval("a1"), _cwd, now=1.0)
    triggers.observe(_question("q1"), _cwd, now=1.0)
    triggers.observe(_question("q2", sid="s2"), _cwd, now=1.0)
    presence.update("ws2", True, "s2", now=1.0)
    triggers.observe({"type": "approval_resolved", "id": "a1", "sessionId": "s1"}, _cwd, now=2.0)
    triggers.observe({"type": "user_question_resolved", "requestId": "q1"}, _cwd, now=2.0)
    triggers.observe({"type": "turn", "sessionId": "s2", "state": "idle", "error": None}, _cwd, now=2.0)
    presence.drop("ws1")
    presence.drop("ws2")
    assert triggers.sweep(now=10.0) == []


def test_swept_approval_ttl_is_the_time_left_and_expired_ones_are_dropped() -> None:
    presence = PresenceTracker()
    triggers = PushTriggers(presence)
    presence.update("ws1", True, "s1", now=0.0)
    triggers.observe(_approval("a1", ttl=300.0), _cwd, now=0.0)
    triggers.observe(_approval("a2", ttl=60.0), _cwd, now=0.0)
    presence.drop("ws1")
    swept = triggers.sweep(now=200.0)
    assert [(p.note["tag"], p.ttl) for p in swept] == [("approval-a1", 100)]


def test_approval_without_session_id_is_not_pushed() -> None:
    triggers = PushTriggers(PresenceTracker())
    assert triggers.observe({"type": "approval_request", "id": "x"}, _cwd, now=0.0) == []
    assert triggers.observe({"type": "approval_resolved", "sessionId": "s"}, _cwd, now=0.0) == []
    assert triggers.sweep(now=1.0) == []


def test_visible_other_session_does_not_suppress() -> None:
    presence = PresenceTracker()
    presence.update("ws1", True, "s2", now=0.0)
    assert len(PushTriggers(presence).observe(_question(), _cwd, now=1.0)) == 1


def _idle(sid: str, error: str | None = None) -> dict:
    return {"type": "turn", "sessionId": sid, "state": "idle", "error": error}


def test_turn_end_pushes_only_long_turns_once() -> None:
    triggers = PushTriggers(PresenceTracker())
    triggers.turn_started("agent-1", "view-1", now=0.0)
    assert triggers.observe(_idle("agent-1"), _cwd, now=30.0) == []

    triggers.turn_started("agent-1", "view-1", now=100.0)
    triggers.turn_started("agent-1", "view-1", now=130.0)
    [done] = triggers.observe(_idle("agent-1"), _cwd, now=161.0)
    assert done.note["kind"] == "turn_done" and done.note["sessionId"] == "view-1"
    assert done.note["title"] == "Finished: alpha-app" and done.note["url"] == "/?session=view-1"
    assert done.urgency == "normal"
    assert triggers.observe(_idle("view-1"), _cwd, now=162.0) == []

    triggers.turn_started("agent-1", "view-1", now=200.0)
    [failed] = triggers.observe(_idle("view-1", "Turn cleared (stuck)"), _cwd, now=300.0)
    assert failed.note["kind"] == "turn_failed" and failed.note["body"] == "Turn stalled"


def test_turn_end_suppressed_when_view_session_visible() -> None:
    presence = PresenceTracker()
    triggers = PushTriggers(presence)
    triggers.turn_started("agent-1", "view-1", now=0.0)
    presence.update("ws", True, "view-1", now=50.0)
    assert triggers.observe(_idle("agent-1"), _cwd, now=90.0) == []


# Phone setup and static


def test_qr_svg_data_uri_is_base64_svg() -> None:
    uri = qr_svg_data_uri("https://desk.tailnet.ts.net/")
    assert uri.startswith("data:image/svg+xml;base64,")
    assert b"<svg" in base64.b64decode(uri.split(",", 1)[1])
    assert qr_svg_data_uri(None) is None


def test_serve_active_requires_a_handler_proxying_to_the_hub_port() -> None:
    status = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {"desk.tailnet.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}},
    }
    assert serve_active_from_status(status, 8787) is True
    assert serve_active_from_status(status, 9000) is False
    assert serve_active_from_status({}, 8787) is False
    assert serve_active_from_status(None, 8787) is False


def test_static_route_serves_webmanifest_as_manifest_json(tmp_path: Path) -> None:
    (tmp_path / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    app = web.Application()
    app.router.add_static("/", tmp_path)

    async def run() -> str:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/manifest.webmanifest")
            return resp.headers["Content-Type"]

    assert asyncio.run(run()).startswith("application/manifest+json")


def test_server_wires_push_endpoints_presence_and_sweep() -> None:
    src = (ROOT / "hub" / "server.py").read_text(encoding="utf-8")
    for route in (
        'add_get("/api/whoami"', 'add_get("/api/push/config"', 'add_post("/api/push/subscribe"',
        'add_post("/api/push/unsubscribe"', 'add_post("/api/push/test"', 'add_get("/api/phone-setup"',
    ):
        assert route in src, route
    assert 'typ == "presence"' in src
    assert "self.presence.drop(ws)" in src
    assert "self.serve_url: str | None = None" in src
    assert "request.get(AUTH_VIA)" in src and "subscription_origin(" in src
    assert "drop_stale_token_subscriptions(" in src
    assert "_push_sweep_loop" in src and "self.notifier.close()" in src
    assert "mimetypes" not in src


def test_server_sweeps_immediately_when_a_page_goes_away() -> None:
    """Without this, a hidden or closed page waits up to SWEEP_INTERVAL_S before the phone buzzes."""
    src = (Path(__file__).resolve().parents[1] / "hub" / "server.py").read_text(encoding="utf-8")
    presence = src[src.index('if typ == "presence":'):src.index('if typ == "set_approval_mode":')]
    assert "self._sweep_pushes_now()" in presence
    assert "visible" in presence
    drop = src[src.index("async def _drop_client"):src.index("def check_auth")]
    assert "self._sweep_pushes_now()" in drop
    loop = src[src.index("async def _push_sweep_loop"):src.index("async def _push_sweep_loop") + 300]
    assert "self._sweep_pushes_now()" in loop