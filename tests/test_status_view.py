"""Pure tests for honest agent vs ACP status mapping."""

from __future__ import annotations

from hub.status_view import map_agent_status


def test_map_agent_status_acp_disconnected() -> None:
    m = map_agent_status(True, False)
    assert m["agentProcess"] == "up"
    assert m["acpConnected"] is False
    assert m["agent"] == "down"  # not chat-ready
    assert m["agentDetail"] == "acp-disconnected"


def test_map_agent_status_ok() -> None:
    m = map_agent_status(True, True)
    assert m["agent"] == "up"
    assert m["agentProcess"] == "up"
    assert m["acpConnected"] is True
    assert m["agentDetail"] == "ok"


def test_map_agent_status_process_down() -> None:
    m = map_agent_status(False, False)
    assert m["agent"] == "down"
    assert m["agentProcess"] == "down"
    assert m["agentDetail"] == "process-down"
