"""Honest agent vs ACP status mapping for pill, /health, and WS status."""

from __future__ import annotations

# ACP self-heal defaults (agent process up, WebSocket down).
ACP_HEAL_MAX_ATTEMPTS = 3
ACP_HEAL_MIN_DOWN_S = 10.0
ACP_HEAL_BACKOFF_BASE_S = 5.0


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


def should_attempt_acp_heal(
    *,
    agent_process_up: bool,
    acp_connected: bool,
    heal_in_progress: bool,
    attempts: int,
    disconnected_for_s: float | None,
    max_attempts: int = ACP_HEAL_MAX_ATTEMPTS,
    min_down_s: float = ACP_HEAL_MIN_DOWN_S,
    backoff_base_s: float = ACP_HEAL_BACKOFF_BASE_S,
) -> bool:
    """True when hub should call AcpClient.reconnect for process-up / ACP-down.

    Wait min_down_s after disconnect, then backoff_base_s * attempts between tries.
    Cap at max_attempts. Never heal when process is down or already connected.
    """
    if not agent_process_up or acp_connected or heal_in_progress:
        return False
    if attempts >= max_attempts:
        return False
    if disconnected_for_s is None:
        return False
    need = min_down_s + backoff_base_s * max(0, attempts)
    return float(disconnected_for_s) >= need
