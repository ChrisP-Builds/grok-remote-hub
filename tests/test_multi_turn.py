"""Unit tests for multi-project concurrent turns and session status flags."""

from __future__ import annotations

from hub.acp_client import AcpClient
from hub.config import Config
from hub.multi_turn import (
    STATUS_IDLE,
    STATUS_QUESTION,
    STATUS_WORKING,
    can_start_concurrent_turn,
    merge_session_flags,
    session_status_flag,
)


def test_session_status_flag_priority():
    assert (
        session_status_flag(turn_running=True, has_pending_question=True)
        == STATUS_QUESTION
    )
    assert (
        session_status_flag(turn_running=True, has_pending_question=False)
        == STATUS_WORKING
    )
    assert (
        session_status_flag(turn_running=False, has_pending_question=True)
        == STATUS_QUESTION
    )
    assert (
        session_status_flag(turn_running=False, has_pending_question=False)
        == STATUS_IDLE
    )


def test_can_start_two_different_cwd_keys():
    active = {"sess-a": "cwd-a"}
    ok, reason = can_start_concurrent_turn(
        "sess-b",
        "cwd-b",
        active_by_session=active,
        max_concurrent=3,
    )
    assert ok is True
    assert reason == "ok"


def test_can_start_same_cwd_blocked():
    active = {"sess-a": "cwd-a"}
    ok, reason = can_start_concurrent_turn(
        "sess-b",
        "cwd-a",
        active_by_session=active,
        max_concurrent=3,
    )
    assert ok is False
    assert reason == "same_cwd_busy"


def test_can_start_max_cap():
    active = {"s1": "c1", "s2": "c2", "s3": "c3"}
    ok, reason = can_start_concurrent_turn(
        "s4",
        "c4",
        active_by_session=active,
        max_concurrent=3,
    )
    assert ok is False
    assert reason == "max_concurrent"


def test_can_start_already_active():
    active = {"sess-a": "cwd-a"}
    ok, reason = can_start_concurrent_turn(
        "sess-a",
        "cwd-a",
        active_by_session=active,
        max_concurrent=1,
    )
    assert ok is True
    assert reason == "already_active"


def test_can_start_empty_active():
    ok, reason = can_start_concurrent_turn(
        "sess-a",
        "cwd-a",
        active_by_session={},
        max_concurrent=3,
    )
    assert ok is True
    assert reason == "ok"


def test_merge_session_flags():
    flags = merge_session_flags(
        ["a", "b", "c"],
        active_sessions={"a", "b"},
        pending_question_sessions={"b"},
    )
    assert flags["a"] == STATUS_WORKING
    assert flags["b"] == STATUS_QUESTION
    assert flags["c"] == STATUS_IDLE


def test_acp_client_turn_running_any_of_two_active():
    """turn_running is true when any of two active_turns is set (no network)."""
    client = AcpClient(Config(), secret="test-secret")
    assert client.turn_running is False
    assert client.turn_session_id is None
    assert client.turn_session_ids == []

    client.active_turns["sess-a"] = {
        "started_at": 10.0,
        "last_activity": 10.0,
        "saw_update": False,
        "cwd_key": "cwd-a",
    }
    client.active_turns["sess-b"] = {
        "started_at": 20.0,
        "last_activity": 20.0,
        "saw_update": True,
        "cwd_key": "cwd-b",
    }
    assert client.turn_running is True
    assert set(client.turn_session_ids) == {"sess-a", "sess-b"}
    # Primary is most recently started
    assert client.turn_session_id == "sess-b"
    assert client.is_session_active("sess-a")
    assert client.active_by_session_cwd() == {
        "sess-a": "cwd-a",
        "sess-b": "cwd-b",
    }

    # Per-session clear leaves the other running
    cleared = client.force_clear_turn("test", session_id="sess-a")
    assert cleared is True
    assert client.is_session_active("sess-a") is False
    assert client.is_session_active("sess-b") is True
    assert client.turn_running is True

    client.force_clear_turn("test all")
    assert client.turn_running is False
    assert client.active_turns == {}


def test_config_max_concurrent_turns_default():
    cfg = Config()
    assert cfg.max_concurrent_turns == 3
