"""Unit tests for Hub session plan viewer (plan.md + plan_mode.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hub.plan_view import (
    PLAN_MD_MAX_BYTES,
    PLAN_MD_NAME,
    PLAN_MODE_NAME,
    PlanViewError,
    _safe_plan_file,
    apply_plan_action,
    merge_plan_mode_action,
    read_session_plan,
)


def _session_dir(tmp_path: Path, sid: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") -> Path:
    # Mimic sessions_root / <encoded-cwd> / sid layout; path is what find_session uses.
    d = tmp_path / "sessions" / "proj" / sid
    d.mkdir(parents=True)
    return d


def test_missing_plan_md(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = _session_dir(tmp_path, sid)
    out = read_session_plan(root, sid, session_path=session_dir)
    assert out["sessionId"] == sid
    assert out["exists"] is False
    assert out["markdown"] == ""
    assert out["planMode"] is None
    assert out["awaitingApproval"] is False
    assert out["state"] is None


def test_plan_md_present(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = _session_dir(tmp_path, sid)
    (session_dir / PLAN_MD_NAME).write_text(
        "# Plan\n\n1. Do thing\n", encoding="utf-8"
    )
    out = read_session_plan(root, sid, session_path=session_dir)
    assert out["exists"] is True
    assert "# Plan" in out["markdown"]
    assert "Do thing" in out["markdown"]
    assert out["awaitingApproval"] is False


def test_plan_mode_awaiting_approval(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = _session_dir(tmp_path, sid)
    (session_dir / PLAN_MD_NAME).write_text("steps\n", encoding="utf-8")
    (session_dir / PLAN_MODE_NAME).write_text(
        json.dumps(
            {
                "awaiting_plan_approval": True,
                "state": "Active",
            }
        ),
        encoding="utf-8",
    )
    out = read_session_plan(root, sid, session_path=session_dir)
    assert out["exists"] is True
    assert out["awaitingApproval"] is True
    assert out["state"] == "Active"
    assert isinstance(out["planMode"], dict)
    assert out["planMode"]["awaiting_plan_approval"] is True


def test_session_not_found(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    with pytest.raises(PlanViewError) as ei:
        read_session_plan(root, "does-not-exist-session-id")
    assert ei.value.status == 404
    assert "not found" in ei.value.message.lower()


def test_empty_session_id(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    with pytest.raises(PlanViewError) as ei:
        read_session_plan(root, "")
    assert ei.value.status == 404


def test_safe_plan_file_fixed_names_only(tmp_path: Path) -> None:
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    ok = _safe_plan_file(session_dir, PLAN_MD_NAME)
    assert ok.name == PLAN_MD_NAME
    assert ok.parent == session_dir.resolve()

    ok_mode = _safe_plan_file(session_dir, PLAN_MODE_NAME)
    assert ok_mode.name == PLAN_MODE_NAME

    for bad in (
        "../plan.md",
        "..\\plan.md",
        "other.md",
        "subdir/plan.md",
        "plan_mode.json.bak",
        "",
        ".",
        "..",
    ):
        with pytest.raises(PlanViewError) as ei:
            _safe_plan_file(session_dir, bad)
        assert ei.value.status == 400


def test_no_client_path_param_only_fixed_files(tmp_path: Path) -> None:
    """API surface is session id only; module never reads client-relative paths."""
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = _session_dir(tmp_path, sid)
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    # Traversal-like content next to session must not be readable via plan API
    (session_dir / "notes.md").write_text("not a plan", encoding="utf-8")
    (session_dir / PLAN_MD_NAME).write_text("real plan", encoding="utf-8")

    out = read_session_plan(root, sid, session_path=session_dir)
    assert out["markdown"] == "real plan"
    assert "SECRET" not in out["markdown"]
    assert "not a plan" not in out["markdown"]

    # Corrupt / missing plan_mode is optional soft-fail
    (session_dir / PLAN_MODE_NAME).write_text("{not json", encoding="utf-8")
    out2 = read_session_plan(root, sid, session_path=session_dir)
    assert out2["planMode"] is None
    assert out2["awaitingApproval"] is False


def test_plan_md_truncated_when_huge(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "eeeeeeee-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = _session_dir(tmp_path, sid)
    # Slightly over the cap
    body = "A" * (PLAN_MD_MAX_BYTES + 100)
    (session_dir / PLAN_MD_NAME).write_text(body, encoding="utf-8")
    out = read_session_plan(root, sid, session_path=session_dir)
    assert out["exists"] is True
    assert out.get("truncated") is True
    assert "truncated" in out["markdown"].lower()
    assert len(out["markdown"]) < len(body)


def test_plan_mode_false_awaiting(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = _session_dir(tmp_path, sid)
    (session_dir / PLAN_MD_NAME).write_text("x\n", encoding="utf-8")
    (session_dir / PLAN_MODE_NAME).write_text(
        json.dumps({"awaiting_plan_approval": False, "state": "Idle"}),
        encoding="utf-8",
    )
    out = read_session_plan(root, sid, session_path=session_dir)
    assert out["awaitingApproval"] is False
    assert out["state"] == "Idle"


def test_merge_approve_clears_awaiting_preserves_keys() -> None:
    existing = {
        "awaiting_plan_approval": True,
        "state": "Active",
        "extra_key": 42,
        "nested": {"a": 1},
    }
    out = merge_plan_mode_action(existing, "approve")
    assert out["awaiting_plan_approval"] is False
    assert out["state"] == "Inactive"
    assert out["extra_key"] == 42
    assert out["nested"] == {"a": 1}
    # Input not mutated
    assert existing["awaiting_plan_approval"] is True
    assert existing["state"] == "Active"


def test_merge_request_changes_stays_active() -> None:
    existing = {"awaiting_plan_approval": True, "state": "Active", "keep": "yes"}
    out = merge_plan_mode_action(existing, "request_changes")
    assert out["awaiting_plan_approval"] is False
    assert out["state"] == "Active"
    assert out["keep"] == "yes"


def test_merge_quit() -> None:
    existing = {"awaiting_plan_approval": True, "state": "Active", "x": 1}
    out = merge_plan_mode_action(existing, "quit")
    assert out["awaiting_plan_approval"] is False
    assert out["state"] == "Inactive"
    assert out["x"] == 1


def test_merge_invalid_action() -> None:
    with pytest.raises(PlanViewError) as ei:
        merge_plan_mode_action({"awaiting_plan_approval": True}, "nope")
    assert ei.value.status == 400
    with pytest.raises(PlanViewError) as ei2:
        merge_plan_mode_action(None, "")
    assert ei2.value.status == 400


def test_apply_plan_action_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "11111111-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_dir = _session_dir(tmp_path, sid)
    (session_dir / PLAN_MD_NAME).write_text("# Plan\n\ndo it\n", encoding="utf-8")
    (session_dir / PLAN_MODE_NAME).write_text(
        json.dumps(
            {
                "awaiting_plan_approval": True,
                "state": "Active",
                "custom": "kept",
            }
        ),
        encoding="utf-8",
    )
    before = read_session_plan(root, sid, session_path=session_dir)
    assert before["awaitingApproval"] is True
    assert before["state"] == "Active"

    out = apply_plan_action(root, sid, "approve", session_path=session_dir)
    assert out["action"] == "approve"
    assert out["awaitingApproval"] is False
    assert out["state"] == "Inactive"
    assert out["exists"] is True
    assert "do it" in out["markdown"]
    assert isinstance(out["planMode"], dict)
    assert out["planMode"]["awaiting_plan_approval"] is False
    assert out["planMode"]["state"] == "Inactive"
    assert out["planMode"]["custom"] == "kept"

    # Disk durable
    disk = json.loads((session_dir / PLAN_MODE_NAME).read_text(encoding="utf-8"))
    assert disk["awaiting_plan_approval"] is False
    assert disk["state"] == "Inactive"
    assert disk["custom"] == "kept"

    again = read_session_plan(root, sid, session_path=session_dir)
    assert again["awaitingApproval"] is False
    assert again["state"] == "Inactive"
