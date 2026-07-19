"""Honest agent vs ACP status mapping for pill, /health, and WS status."""

from __future__ import annotations

from typing import Any

# ACP self-heal defaults (agent process up, WebSocket down).
ACP_HEAL_MAX_ATTEMPTS = 3
ACP_HEAL_MIN_DOWN_S = 10.0
ACP_HEAL_BACKOFF_BASE_S = 5.0

# ACP quality / zombie detection (half-open sockets, stalled pending RPCs).
# Must be > session_policy.NO_OUTPUT_SECONDS so stall policy owns silent prompts
# before quality flips to stale and heal would reconnect mid-turn.
ACP_STALE_SECONDS = 90.0
ACP_ZOMBIE_SEND_FAILURES = 2

# Proactive WebSocket ping when idle silence looks half-open (false-green).
ACP_PROBE_SILENCE_S = 45.0  # no recv for this long → consider probe
ACP_PROBE_INTERVAL_S = 30.0  # min time between probes
ACP_PROBE_TIMEOUT_S = 5.0

# session/load replay suppress: hold until quiet period or max wall time.
LOAD_SUPPRESS_QUIET_S = 1.5
LOAD_SUPPRESS_MAX_S = 20.0


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
    acp_quality: str | None = None,
    turn_active: bool = False,
) -> bool:
    """True when hub should call AcpClient.reconnect for process-up / ACP-down.

    Wait min_down_s after disconnect, then backoff_base_s * attempts between tries.
    Cap at max_attempts. Never heal when process is down or already connected.

    When status uses quality-adjusted acpConnected (false for zombie/stale),
    existing callers heal those half-dead cases without extra branches here.

    Mid-turn stale is an exception: if turn_active and quality is ``stale``,
    return False. Pending session/prompt silence (TTFB, large context) is owned
    by the stall watchdog (no-output / mid-turn), not by reconnect. Still heal
    mid-turn when quality is ``zombie`` or ``down`` (wire truly dead).
    """
    if not agent_process_up or acp_connected or heal_in_progress:
        return False
    if attempts >= max_attempts:
        return False
    if turn_active and str(acp_quality or "").lower() == "stale":
        return False
    if disconnected_for_s is None:
        return False
    need = min_down_s + backoff_base_s * max(0, attempts)
    return float(disconnected_for_s) >= need


def should_probe_acp_liveness(
    *,
    connected: bool,
    has_pending: bool,
    seconds_since_recv: float | None,
    seconds_since_probe: float | None,
    silence_s: float = ACP_PROBE_SILENCE_S,
    interval_s: float = ACP_PROBE_INTERVAL_S,
) -> bool:
    """True when hub should send a WebSocket ping to detect half-open ACP.

    Skip when not connected, when a pending RPC exists (stale path covers that),
    when recv is recent, or when a probe ran recently. Does not KillAgent.
    """
    if not connected:
        return False
    if has_pending:
        return False
    if seconds_since_recv is None:
        return False
    if float(seconds_since_recv) < float(silence_s):
        return False
    if seconds_since_probe is not None and float(seconds_since_probe) < float(
        interval_s
    ):
        return False
    return True


# ACP notification methods that session/load may replay as historical flood.
_SESSION_LOAD_REPLAY_METHODS = frozenset(
    {
        "session/update",
        "_x.ai/session/update",
        "_x.ai/session_notification",
        "x.ai/session_notification",
    }
)


def should_suppress_session_load_fanout(
    *,
    loading_session_ids: set[str] | frozenset[str],
    session_id: str | None,
    method: str | None,
    update_kind: str | None = None,
    active_turn_session_ids: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """True when an ACP notification is historical replay during session/load.

    Drop session/update (+ x.ai session updates/notifications) for the loading
    session so the UI does not strobe. Allow available_commands_update through.

    While a session is in loading_session_ids, always suppress (even if that
    session is also in active_turn_session_ids). Residual history flush must not
    note_activity or fanout as live stream after heal re-registers a turn.
    Active-turn bypass applies only when the session is not loading.

    When session_id is missing, suppress only if no active turn exists (ambiguous
    frames during pure load); if any turn is live, do not suppress solely for load.
    """
    if not loading_session_ids:
        return False
    if method not in _SESSION_LOAD_REPLAY_METHODS:
        return False
    if update_kind == "available_commands_update":
        return False
    # Loading wins over active-turn: residual load-replay stays suppressed.
    if session_id and session_id in loading_session_ids:
        return True
    # Not loading this sid: live turns receive activity/fanout.
    if session_id and session_id in active_turn_session_ids:
        return False
    if not session_id:
        # Ambiguous frame during load: suppress only when no live turn is running.
        if active_turn_session_ids:
            return False
        return True
    return False


def load_suppress_should_release(
    *,
    quiet_elapsed_s: float,
    quiet_s: float,
    held_s: float,
    max_s: float,
) -> bool:
    """True when load-replay suppress may release (quiet period or max hold)."""
    return held_s >= max_s or quiet_elapsed_s >= quiet_s
