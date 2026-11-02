"""Structural checks for the Home Screen app, web push UI, presence and phone setup (ADR-028)."""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
EM_DASH = "\u2014"
HTML_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML")


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


def _sw() -> str:
    return (STATIC / "sw.js").read_text(encoding="utf-8")


def _fn(src: str, name: str, span: int = 1600) -> str:
    match = re.search(rf"(async )?function {name}\(", src)
    assert match, f"missing function {name}"
    return src[match.start() : match.start() + span]


def _body(src: str, name: str) -> str:
    """Function text up to its closing brace at the enclosing indentation."""
    match = re.search(rf"^( *)(async )?function {name}\(", src, re.M)
    assert match, f"missing function {name}"
    close = src.find("\n" + match.group(1) + "}\n", match.start())
    assert close > 0
    return src[match.start() : close]


def _push_block(js: str) -> str:
    start = js.find("// Push and phone setup (ADR-028)")
    end = js.find("// End push")
    assert 0 <= start < end, "push section markers missing"
    return js[start:end]


def _png_info(path: Path) -> tuple[int, int, int, bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, color_type = struct.unpack(">IIBB", data[16:26])
    idat = b""
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    return width, height, color_type, raw[1:5]


def test_manifest_fields_and_icons() -> None:
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == "Grok Remote Hub"
    assert manifest["short_name"] == "Grok Hub"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert "--bg: " + manifest["background_color"] in _css()
    assert manifest["theme_color"] == manifest["background_color"]
    by_size: dict[str, set[str]] = {}
    for icon in manifest["icons"]:
        by_size.setdefault(icon["sizes"], set()).add(icon["purpose"])
        assert icon["type"] == "image/png"
        assert (STATIC / icon["src"].lstrip("/")).is_file()
    assert by_size["192x192"] == {"any", "maskable"}
    assert by_size["512x512"] == {"any", "maskable"}


def test_icon_pngs_have_right_size_and_opaque_corner() -> None:
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        width, height, color_type, first_pixel = _png_info(STATIC / "icons" / name)
        assert (width, height) == (size, size), name
        if color_type == 6:
            assert first_pixel[3] == 255, f"{name} corner is transparent"


def test_html_home_screen_tags_and_cache_bust() -> None:
    html = _html()
    assert '<link rel="manifest" href="/manifest.webmanifest"' in html
    assert 'href="/icons/apple-touch-icon.png"' in html
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html
    assert 'name="apple-mobile-web-app-title" content="Grok Hub"' in html
    assert 'name="theme-color" content="#0a0c10"' in html
    assert "/app.css?v=20261101d" in html
    assert "/app.js?v=20261101d" in html


def test_service_worker_push_only_no_caching() -> None:
    sw = _sw()
    assert 'addEventListener("push"' in sw
    assert 'addEventListener("notificationclick"' in sw
    assert "showNotification(" in sw
    assert "renotify" in sw
    assert "openWindow(" in sw
    assert ".navigate(" in sw
    assert ".focus()" in sw
    assert 'addEventListener("fetch"' not in sw
    assert "caches" not in sw
    assert "respondWith" not in sw
    for sink in HTML_SINKS:
        assert sink not in sw
    assert "url.origin === self.location.origin" in sw


def test_service_worker_registered_only_in_secure_context() -> None:
    reg = _fn(_push_block(_js()), "registerServiceWorker", 500)
    assert "window.isSecureContext" in reg
    assert '"serviceWorker" in navigator' in reg
    assert 'navigator.serviceWorker.register("/sw.js", { scope: "/" })' in reg


def test_push_control_markup() -> None:
    html = _html()
    for el_id in ("push-control", "push-label", "push-detail", "btn-push-on", "btn-push-test", "btn-push-off"):
        assert f'id="{el_id}"' in html
    assert "Turn on notifications" in html


def test_enable_push_requests_permission_first() -> None:
    enable = _fn(_push_block(_js()), "enablePush", 1600)
    first_await = enable.find("await ")
    assert enable.find("await Notification.requestPermission()") == first_await
    assert "userVisibleOnly: true" in enable
    assert "applicationServerKey" in enable
    assert '"/api/push/subscribe"' in enable
    assert "subscription: sub.toJSON()" in enable


def test_push_states_and_endpoints() -> None:
    block = _push_block(_js())
    state_fn = _fn(block, "computePushState", 1400)
    order = [
        state_fn.find("window.isSecureContext"),
        state_fn.find("isIosDevice()"),
        state_fn.find('"PushManager" in window'),
        state_fn.find("config.enabled"),
        state_fn.find('"denied"'),
        state_fn.find("subscribed"),
    ]
    assert all(i >= 0 for i in order) and order == sorted(order)
    assert "navigator.standalone" in block
    for text in (
        "Add to Home Screen first",
        "Notifications blocked",
        "Notifications on",
        "Notifications off",
        "https Tailscale address",
    ):
        assert text in block
    assert '"/api/push/config"' in block
    assert '"/api/push/test"' in block
    disable = _fn(block, "disablePush", 1200)
    assert ".unsubscribe()" in disable
    assert '"/api/push/unsubscribe"' in disable


def test_presence_sent_on_all_triggers() -> None:
    js = _js()
    block = _push_block(js)
    send = _fn(block, "sendPresence", 900)
    assert 'type: "presence"' in send
    assert "sessionId: approvalSessionId()" in send
    bind = _fn(block, "bindPresence", 900)
    for event in ('"focus"', '"blur"'):
        assert event in bind
    assert "PRESENCE_HEARTBEAT_MS" in bind
    vis = js.find('document.addEventListener("visibilitychange"')
    assert "onPresenceVisibilityChange()" in js[vis : vis + 200]
    assert "sendPresence()" in _fn(block, "onPresenceVisibilityChange", 300)
    assert "const PRESENCE_HEARTBEAT_MS = 60000" in block
    open_idx = js.find('ws.addEventListener("open"')
    assert "sendPresence(" in js[open_idx : open_idx + 500]
    assert "sendPresence(" in _fn(js, "setLivePromptSessionId", 300)
    assert "sendPresence(" in _fn(js, "setTopbarSessionMeta", 3000)
    assert "sendPresence(" in _fn(js, "clearTopbarSessionMeta", 400)


def test_idle_user_reports_not_visible() -> None:
    block = _push_block(_js())
    assert "const USER_IDLE_MS = 120000" in block
    events = re.search(r"const USER_INPUT_EVENTS = \[([^\]]*)\]", block)
    assert events
    for name in ("pointer", "keydown", "touchstart", "wheel", "scroll"):
        assert name in events.group(1)
    assert "!state.userIdle" in _fn(block, "pageVisible", 300)
    idle = _fn(block, "checkUserIdle", 300)
    assert idle.find("state.userIdle = true") < idle.find("sendPresence()")
    active = _fn(block, "noteUserInput", 300)
    assert active.find("state.userIdle = false") < active.find("sendPresence()")
    bind = _fn(block, "bindPresence", 1200)
    assert "USER_INPUT_EVENTS.forEach" in bind
    assert "setInterval(checkUserIdle" in bind


def test_existing_subscription_resynced_silently_on_load() -> None:
    block = _push_block(_js())
    load = _body(block, "loadPushState")
    assert "syncPushSubscription(reg, sub)" in load
    assert "requestPermission" not in load
    sync = _body(block, "syncPushSubscription")
    assert 'Notification.permission !== "granted"' in sync
    assert "subscriptionKey(sub) !== normalizeBase64Url(p.config.publicKey)" in sync
    assert sync.find(".unsubscribe()") < sync.find("pushManager.subscribe(")
    assert '"/api/push/subscribe"' in sync
    assert "requestPermission" not in sync
    assert "applicationServerKey" in _fn(block, "subscriptionKey", 400)


def test_service_worker_renews_changed_subscription() -> None:
    sw = _sw()
    assert 'addEventListener("pushsubscriptionchange"' in sw
    renew = _fn(sw, "renewSubscription", 800)
    assert "pushManager.subscribe(" in renew
    assert '"/api/push/subscribe"' in renew
    assert 'credentials: "same-origin"' in renew
    key = _fn(sw, "currentServerKey", 600)
    assert "oldSubscription" in key
    assert '"/api/push/config"' in key


def test_notification_click_messages_app_shell_first() -> None:
    sw = _sw()
    click = _fn(sw, "focusOrOpen", 900)
    assert '.pathname === "/"' in click
    post = click.find('postMessage({ type: "open-url", url })')
    nav = click.find(".navigate(")
    open_ = click.find("openWindow(")
    assert 0 <= post < nav < open_
    handler = _fn(_push_block(_js()), "onServiceWorkerMessage", 600)
    assert "openSessionById(sessionId)" in handler
    for reload in ("location.assign", "location.reload", "location.href ="):
        assert reload not in handler
    assert "navigator.serviceWorker.startMessages()" in _fn(_push_block(_js()), "registerServiceWorker", 400)


def test_session_deep_link_consumed_and_opened() -> None:
    js = _js()
    consume = _fn(_push_block(js), "consumeSessionDeepLink", 700)
    assert 'searchParams.get("session")' in consume
    assert 'searchParams.delete("session")' in consume
    assert "history.replaceState(" in consume
    boot = _fn(js, "bootstrap", 6000)
    assert "const deepLinkId = consumeSessionDeepLink()" in boot
    assert "deepLinkId ||" in boot


def test_phone_setup_modal() -> None:
    html = _html()
    for el_id in ("btn-phone-setup", "modal-phone-setup", "phone-setup-qr", "phone-setup-url", "btn-phone-setup-copy", "phone-setup-steps", "phone-setup-warning"):
        assert f'id="{el_id}"' in html
    assert "HTTPS address is not active yet" in html
    block = _push_block(_js())
    render = _fn(block, "renderPhoneSetup", 1500)
    assert "data.qrDataUri" in render
    assert ".src = " in render
    assert ".textContent = " in render
    assert "data.serveActive === false" in render
    assert '"/api/phone-setup"' in block
    css = _css()
    hide = css[css.find("@media (max-width: 899px) {\n  .phone-setup-btn") :]
    assert "display: none" in hide[: hide.find("}")]


def test_token_banner_respects_whoami() -> None:
    js = _js()
    banner = _fn(js, "updateAuthTokenBanner", 700)
    assert "state.authNeeded" in banner
    whoami = _fn(_push_block(js), "loadWhoami", 900)
    assert '"/api/whoami"' in whoami
    assert "state.authNeeded = false" in whoami
    assert "loadWhoami()" in _fn(js, "bootstrap", 6000)


def test_new_code_has_no_html_sinks_or_em_dashes() -> None:
    block = _push_block(_js())
    for sink in HTML_SINKS:
        assert sink not in block
    assert EM_DASH not in block
    assert EM_DASH not in _sw()
    css = _css()
    start = css.find("/* Push and phone setup")
    assert start >= 0
    assert EM_DASH not in css[start : css.find("/* End push", start)]
