"""Honest agent vs ACP status mapping for pill, /health, and WS status."""

from __future__ import annotations


def map_agent_status(
    agent_process_up: bool, acp_connected: bool
) -> dict[str, str | bool]:
    """Return agent (chat-ready), agentProcess, acpConnected, agentDetail.

    - agent: "up" only when process is up AND ACP is connected (chat-ready).
    - agentProcess: process/port only.
    - acpConnected: raw ACP flag.
    - agentDetail: "ok" | "acp-disconnected" | "process-down".
    """
    process = "up" if agent_process_up else "down"
    if not agent_process_up:
        return {
            "agent": "down",
            "agentProcess": process,
            "acpConnected": bool(acp_connected),
            "agentDetail": "process-down",
        }
    if not acp_connected:
        return {
            "agent": "down",
            "agentProcess": process,
            "acpConnected": False,
            "agentDetail": "acp-disconnected",
        }
    return {
        "agent": "up",
        "agentProcess": process,
        "acpConnected": True,
        "agentDetail": "ok",
    }
