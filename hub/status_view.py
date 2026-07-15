"""Honest agent vs ACP status mapping for pill, /health, and WS status."""

from __future__ import annotations

from typing import Any

# ACP self-heal defaults (agent process up, WebSocket down).
ACP_HEAL_MAX_ATTEMPTS = 3
ACP_HEAL_MIN_DOWN_S = 10.0
ACP_HEAL_BACKOFF_BASE_S = 5.0

# ACP quality / zombie detection (half-open sockets, stalled pending RPCs).
ACP_STALE_SECONDS = 45.0
ACP_ZOMBIE_SEND_FAILURES = 2


def map_acp_quality(
    *,
    agent_process_up: bool,
    acp_connected: bool,
    consecutive_send_failures: int = 0,
    seconds_since_recv: float | None = None,
    has_pending: bool = False,
    zombie_failures: int = ACP_ZOMBIE_SEND_FAILURES,
    stale_seconds: float = ACP_STALE_SECONDS,
) -> dict[str, Any]:
    """Return acpQuality, acpConnected (chat-usable), agentDetail.

    - process down -> quality down, connected false, process-down
    - not acp_connected -> down, false, acp-disconnected
    - consecutive_send_failures >= zombie_failures -> zombie, false, acp-zombie
    - has_pending and seconds_since_recv >= stale_seconds -> stale, false, acp-stale
    - else if connected -> ok, true, ok

    Chat-usable acpConnected is false for zombie/stale so heal gates that key
    off acp_connected=false still fire without a separate quality branch.
    """
    if not agent_process_up:
        return {
            "acpQuality": "down",
            "acpConnected": False,
            "agentDetail": "process-down",
        }
    if not acp_connected:
        return {
            "acpQuality": "down",
            "acpConnected": False,
            "agentDetail": "acp-disconnected",
        }
    if int(consecutive_send_failures) >= int(zombie_failures):
        return {
            "acpQuality": "zombie",
            "acpConnected": False,
            "agentDetail": "acp-zombie",
        }
    if (
        has_pending
        and seconds_since_recv is not None
        and float(seconds_since_recv) >= float(stale_seconds)
    ):
        return {
            "acpQuality": "stale",
            "acpConnected": False,
            "agentDetail": "acp-stale",
        }
    return {
        "acpQuality": "ok",
        "acpConnected": True,
        "agentDetail": "ok",
    }


def map_agent_status(
    agent_process_up: bool,
    acp_connected: bool,
    *,
    consecutive_send_failures: int = 0,
    seconds_since_recv: float | None = None,
    has_pending: bool = False,
    zombie_failures: int = ACP_ZOMBIE_SEND_FAILURES,
    stale_seconds: float = ACP_STALE_SECONDS,
) -> dict[str, str | bool]:
    """Return agent (chat-ready), agentProcess, acpConnected, agentDetail, acpQuality.

    - agent: "up" only when process is up AND quality is chat-usable (ok).
    - agentProcess: process/port only.
    - acpConnected: quality-adjusted chat-usable flag (false for zombie/stale).
    - agentDetail: "ok" | "acp-disconnected" | "process-down" | "acp-zombie" | "acp-stale".
    - acpQuality: "ok" | "down" | "zombie" | "stale".

    Optional liveness kwargs default so existing call sites stay compatible.
    """
    process = "up" if agent_process_up else "down"
    quality = map_acp_quality(
        agent_process_up=agent_process_up,
        acp_connected=acp_connected,
        consecutive_send_failures=consecutive_send_failures,
        seconds_since_recv=seconds_since_recv,
        has_pending=has_pending,
        zombie_failures=zombie_failures,
        stale_seconds=stale_seconds,
    )
    chat_ok = bool(quality["acpConnected"])
    return {
        "agent": "up" if (agent_process_up and chat_ok) else "down",
        "agentProcess": process,
        "acpConnected": chat_ok,
        "agentDetail": str(quality["agentDetail"]),
        "acpQuality": str(quality["acpQuality"]),
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

    When status uses quality-adjusted acpConnected (false for zombie/stale),
    existing callers heal those half-dead cases without extra branches here.
    """
    if not agent_process_up or acp_connected or heal_in_progress:
        return False
    if attempts >= max_attempts:
        return False
    if disconnected_for_s is None:
        return False
    need = min_down_s + backoff_base_s * max(0, attempts)
    return float(disconnected_for_s) >= need
