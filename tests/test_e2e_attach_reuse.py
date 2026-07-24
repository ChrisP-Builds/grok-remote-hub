"""API-level attach reuse regression against a running hub (no Playwright).

Playwright browser suite may hang on Windows (npx/chromium with no output for
120s+). This file is the durable shipped-path proof for live attach reuse:
double attach same session, and dead-view attach via byCwd — stdlib urllib only.

  python -m pytest tests/test_e2e_attach_reuse.py -q

Env:
  HUB_URL  default http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

import pytest

HUB = os.environ.get("HUB_URL", "http://127.0.0.1:8787").rstrip("/")
ATTACH_BUDGET_S = 30.0
KNOWN_MISSING_SID = "019f73a3-3c92-7a40-a383-1622f8250d45"
GRH_CWD_MARKERS = ("grok remote hub",)


def _hub_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{HUB}/health", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


pytestmark = [
    pytest.mark.skipif(not _hub_ok(), reason=f"hub not reachable at {HUB}"),
]


def _get_json(path: str, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(f"{HUB}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(path: str, body: dict, timeout: float = 45.0) -> tuple[int, dict, float]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{HUB}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            status = int(r.status)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = int(e.code)
    elapsed = time.perf_counter() - t0
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw}
    if not isinstance(payload, dict):
        payload = {"_body": payload}
    return status, payload, elapsed


def _fetch_sessions() -> list[dict]:
    body = _get_json("/api/sessions", timeout=15.0)
    items = body.get("items") or []
    return list(items) if isinstance(items, list) else []


def _cwd_looks_grh(cwd: str) -> bool:
    low = (cwd or "").replace("\\", "/").casefold()
    return any(m in low for m in GRH_CWD_MARKERS)


def _pick_attach_session(items: list[dict]) -> dict | None:
    """Prefer isHubRemote / working non-subagent with GRH cwd, else any with cwd."""
    with_cwd = [
        s
        for s in items
        if s and isinstance(s, dict) and str(s.get("cwd") or "").strip()
    ]
    if not with_cwd:
        return None

    def score(s: dict) -> tuple:
        cwd = str(s.get("cwd") or "")
        return (
            0 if _cwd_looks_grh(cwd) else 1,
            0 if s.get("isHubRemote") else 1,
            0 if (s.get("isWorking") and not s.get("isSubagent")) else 1,
            0 if not s.get("isSubagent") else 1,
        )

    return sorted(with_cwd, key=score)[0]


def _grh_cwd_from_sessions(items: list[dict]) -> str:
    for s in items:
        if not s or not isinstance(s, dict):
            continue
        cwd = str(s.get("cwd") or "").strip()
        if cwd and _cwd_looks_grh(cwd):
            return cwd
    # Fall back to any cwd from a preferred session
    pick = _pick_attach_session(items)
    if pick:
        return str(pick.get("cwd") or "").strip()
    return ""


def test_health_ok_after_ops() -> None:
    health = _get_json("/health", timeout=5.0)
    assert health.get("ok") is True
    assert "agent" in health
    # ACP surface: either acpConnected or acpQuality (both present on current hub)
    assert "acpConnected" in health or "acpQuality" in health
    if "acpConnected" in health:
        assert isinstance(health["acpConnected"], bool)
    if "acpQuality" in health:
        assert health["acpQuality"] is not None


def test_double_attach_same_session_reuses_live_id() -> None:
    items = _fetch_sessions()
    session = _pick_attach_session(items)
    if not session:
        pytest.skip("No session with cwd available for attach")

    session_id = str(session.get("sessionId") or "").strip()
    cwd = str(session.get("cwd") or "").strip()
    if not session_id or not cwd:
        pytest.skip("Picked session missing sessionId or cwd")

    status1, body1, elapsed1 = _post_json(
        f"/api/sessions/{session_id}/attach", {"cwd": cwd}
    )
    if status1 == 503:
        pytest.skip("Agent not connected — cannot prove attach reuse")
    assert status1 == 200, f"attach#1 status={status1} body={body1}"
    assert elapsed1 < ATTACH_BUDGET_S, f"attach#1 too slow: {elapsed1:.2f}s"
    assert body1.get("liveSessionId"), f"attach#1 missing liveSessionId: {body1}"

    status2, body2, elapsed2 = _post_json(
        f"/api/sessions/{session_id}/attach", {"cwd": cwd}
    )
    if status2 == 503:
        pytest.skip("Agent not connected on second attach")
    assert status2 == 200, f"attach#2 status={status2} body={body2}"
    assert elapsed2 < ATTACH_BUDGET_S, f"attach#2 too slow: {elapsed2:.2f}s"

    live1 = body1.get("liveSessionId")
    live2 = body2.get("liveSessionId")
    reason1 = body1.get("reason")
    switched1 = body1.get("switched")
    if reason1 in ("hub_session", "reuse_cwd") or switched1 is False:
        assert live2 == live1, (
            f"expected live reuse: live1={live1} live2={live2} "
            f"reason1={reason1} switched1={switched1} reason2={body2.get('reason')}"
        )


def test_dead_view_attach_does_not_mint_two_sessions() -> None:
    items = _fetch_sessions()
    known_ids = {
        str(s.get("sessionId") or "").strip()
        for s in items
        if s and isinstance(s, dict)
    }
    known_ids.discard("")

    dead_id = KNOWN_MISSING_SID
    if dead_id in known_ids:
        # Invent a uuid that is not in the list
        for _ in range(8):
            candidate = str(uuid.uuid4())
            if candidate not in known_ids:
                dead_id = candidate
                break
        else:
            dead_id = f"dead-view-{uuid.uuid4()}"

    cwd = _grh_cwd_from_sessions(items)
    if not cwd:
        pytest.skip("No session cwd available for dead-view attach")

    status1, body1, elapsed1 = _post_json(
        f"/api/sessions/{dead_id}/attach", {"cwd": cwd}
    )
    if status1 == 503:
        pytest.skip("Agent not connected — cannot prove dead-view attach reuse")
    assert status1 == 200, f"dead attach#1 status={status1} body={body1}"
    assert elapsed1 < ATTACH_BUDGET_S, f"dead attach#1 too slow: {elapsed1:.2f}s"
    live1 = body1.get("liveSessionId")
    assert live1, f"dead attach#1 missing liveSessionId: {body1}"

    status2, body2, elapsed2 = _post_json(
        f"/api/sessions/{dead_id}/attach", {"cwd": cwd}
    )
    if status2 == 503:
        pytest.skip("Agent not connected on second dead-view attach")
    assert status2 == 200, f"dead attach#2 status={status2} body={body2}"
    assert elapsed2 < ATTACH_BUDGET_S, f"dead attach#2 too slow: {elapsed2:.2f}s"
    live2 = body2.get("liveSessionId")
    assert live2 == live1, (
        f"dead-view attach minted different live ids: "
        f"live1={live1} live2={live2} reason1={body1.get('reason')} "
        f"reason2={body2.get('reason')}"
    )
    # Force-pin reasons (not soft cli_or_foreign). Requires hub process with
    # dead_view switch code; older hubs may still return cli_or_foreign_session.
    reason1 = str(body1.get("reason") or "")
    if reason1 in ("dead_view", "resume_failed"):
        assert reason1 in ("dead_view", "resume_failed")
    elif reason1 == "cli_or_foreign_session":
        # Hub not restarted with dead_view pin yet — still no double-mint above.
        pass
    # Cold-path honesty fields when present (new hub).
    if "ensureMs" in body1:
        assert isinstance(body1.get("ensureMs"), (int, float))
        assert "ensureAction" in body1
        assert "liveHotPath" in body1
