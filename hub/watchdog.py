"""Long-lived external hub health watchdog (stdlib only).

Polls http://127.0.0.1:<bind_port>/health. On N consecutive failures and after
cooldown since last restart, runs restart-hub.ps1 -KeepAgent with no window.

CLI: python -m hub.watchdog [--once] [--interval N] [--max-fails N]
     [--cooldown-minutes N] [--root PATH]

Does not import the hub server stack (aiohttp etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows CREATE_NO_WINDOW: no console flash when spawning PowerShell restart.
CREATE_NO_WINDOW = 0x08000000

DEFAULT_PORT = 8787
DEFAULT_INTERVAL = 60
DEFAULT_MAX_FAILS = 2
DEFAULT_COOLDOWN_MINUTES = 5
HEALTH_TIMEOUT_SEC = 3.0
BIND_PORT_RE = re.compile(r"^\s*bind_port\s*=\s*(\d+)", re.MULTILINE)

_STATE_KEYS = ("consecutiveFails", "lastRestartAt", "lastOkAt", "lastError")


def default_root() -> Path:
    """Repo root: parent of the hub package directory."""
    return Path(__file__).resolve().parent.parent


def parse_bind_port(config_text: str, default: int = DEFAULT_PORT) -> int:
    """Parse bind_port from config.toml text (same regex as watch-hub.ps1)."""
    m = BIND_PORT_RE.search(config_text or "")
    if not m:
        return default
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return default


def read_bind_port(root: Path, default: int = DEFAULT_PORT) -> int:
    cfg = root / "config.toml"
    if not cfg.is_file():
        return default
    try:
        return parse_bind_port(cfg.read_text(encoding="utf-8", errors="replace"), default)
    except OSError:
        return default


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {
        "consecutiveFails": 0,
        "lastRestartAt": None,
        "lastOkAt": None,
        "lastError": None,
    }


def read_state(path: Path) -> dict[str, Any]:
    """Load watch-hub-state.json; always returns at least the original keys."""
    base = default_state()
    if not path.is_file():
        return base
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return base
        out = dict(base)
        if data.get("consecutiveFails") is not None:
            try:
                out["consecutiveFails"] = int(data["consecutiveFails"])
            except (TypeError, ValueError):
                out["consecutiveFails"] = 0
        out["lastRestartAt"] = data.get("lastRestartAt")
        out["lastOkAt"] = data.get("lastOkAt")
        out["lastError"] = data.get("lastError")
        # Preserve optional richer keys from prior runs.
        for k, v in data.items():
            if k not in out:
                out[k] = v
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return base


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    obj: dict[str, Any] = {
        "consecutiveFails": int(state.get("consecutiveFails") or 0),
        "lastRestartAt": state.get("lastRestartAt"),
        "lastOkAt": state.get("lastOkAt"),
        "lastError": state.get("lastError"),
    }
    for k in (
        "pollIntervalSeconds",
        "pid",
        "mode",
        "lastCheckAt",
    ):
        if k in state:
            obj[k] = state[k]
    path.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")


def cooldown_elapsed(last_restart_at: Any, cooldown_minutes: int, now: datetime | None = None) -> bool:
    """True if no lastRestartAt or age >= cooldown_minutes (UTC-aware when possible)."""
    if not last_restart_at:
        return True
    try:
        s = str(last_restart_at).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        t = datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        return (now_dt - t).total_seconds() >= float(cooldown_minutes) * 60.0
    except (TypeError, ValueError, OSError):
        return True


def check_health(url: str, timeout: float = HEALTH_TIMEOUT_SEC) -> tuple[bool, str | None]:
    """GET health URL. Success when JSON has ok: true. Returns (ok, error_or_none)."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return False, "health response not json"
        if isinstance(data, dict) and data.get("ok") is True:
            return True, None
        return False, "health ok!=true"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None) or e
        return False, str(reason)
    except TimeoutError:
        return False, "timeout"
    except OSError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001 — surface any transport failure
        return False, str(e)


def log_line(log_path: Path, msg: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Local wall time with offset, matching PowerShell yyyy-MM-ddTHH:mm:ss.fffK style closely.
    ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
    line = f"{ts} {msg}\n"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def day_log_path(logs_dir: Path, when: datetime | None = None) -> Path:
    d = when or datetime.now()
    return logs_dir / f"watch-hub-{d.strftime('%Y%m%d')}.log"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                    return False
                # STILL_ACTIVE = 259
                return int(exit_code.value) == 259
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, AttributeError, ValueError):
        return False


def _cmdline_for_pid(pid: int) -> str | None:
    """Best-effort process command line (Windows WMI via PowerShell; None if unknown)."""
    if sys.platform != "win32":
        # Linux: /proc/<pid>/cmdline
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        except OSError:
            return None
    try:
        # Avoid console flash; short timeout.
        creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        ps = (
            f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\""
            f" -ErrorAction SilentlyContinue).CommandLine"
        )
        r = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creationflags,
        )
        out = (r.stdout or "").strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def is_watchdog_process(pid: int) -> bool:
    """True if PID is alive and looks like python running hub.watchdog."""
    if not _pid_alive(pid):
        return False
    cmd = _cmdline_for_pid(pid)
    if not cmd:
        # Alive but unknown cmdline: treat as possible peer only if pid file claimed it;
        # caller uses is_watchdog_process for single-instance — require hub.watchdog marker.
        return False
    low = cmd.lower()
    if "hub.watchdog" not in low and "hub\\watchdog" not in low and "hub/watchdog" not in low:
        return False
    # Prefer python* host
    if "python" not in low and "py.exe" not in low:
        return False
    return True


def read_pid_file(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if not raw:
            return None
        return int(raw[0].strip())
    except (OSError, ValueError):
        return None


def write_pid_file(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{int(pid)}\n", encoding="ascii")


def remove_pid_file_if_ours(path: Path, our_pid: int) -> None:
    existing = read_pid_file(path)
    if existing is None:
        return
    if existing != our_pid:
        return
    try:
        path.unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    except OSError:
        pass


def run_restart(root: Path, log_path: Path) -> int:
    """Invoke restart-hub.ps1 -KeepAgent with CREATE_NO_WINDOW; wait for completion."""
    restart_script = root / "restart-hub.ps1"
    if not restart_script.is_file():
        log_line(log_path, "ERROR missing restart-hub.ps1")
        return -1
    log_line(log_path, "RESTART invoking restart-hub.ps1 -KeepAgent")
    ps_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not ps_exe.is_file():
        ps_exe = Path("powershell.exe")
    creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        r = subprocess.run(
            [
                str(ps_exe),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(restart_script),
                "-KeepAgent",
            ],
            cwd=str(root),
            timeout=600,
            creationflags=creationflags,
        )
        code = int(r.returncode if r.returncode is not None else -1)
        log_line(log_path, f"RESTART done exit={code}")
        return code
    except subprocess.TimeoutExpired:
        log_line(log_path, "RESTART error: timeout")
        return -1
    except OSError as e:
        log_line(log_path, f"RESTART error: {e}")
        return -1


def one_cycle(
    root: Path,
    *,
    max_fails: int = DEFAULT_MAX_FAILS,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    poll_interval_seconds: int | None = None,
    mode: str = "daemon",
    pid: int | None = None,
) -> dict[str, Any]:
    """Run a single health check / possible restart. Returns updated state."""
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    state_path = logs / "watch-hub-state.json"
    log_path = day_log_path(logs)
    port = read_bind_port(root)
    url = f"http://127.0.0.1:{port}/health"
    state = read_state(state_path)
    state["lastCheckAt"] = utc_now_iso()
    if poll_interval_seconds is not None:
        state["pollIntervalSeconds"] = int(poll_interval_seconds)
    if pid is not None:
        state["pid"] = int(pid)
    state["mode"] = mode

    ok, err = check_health(url)
    if ok:
        state["consecutiveFails"] = 0
        state["lastOkAt"] = utc_now_iso()
        state["lastError"] = None
        write_state(state_path, state)
        log_line(log_path, f"OK port={port}")
        return state

    fails = int(state.get("consecutiveFails") or 0) + 1
    state["consecutiveFails"] = fails
    state["lastError"] = err
    write_state(state_path, state)
    log_line(log_path, f"FAIL port={port} fails={fails}/{max_fails} err={err}")

    if fails < max_fails:
        return state

    if not cooldown_elapsed(state.get("lastRestartAt"), cooldown_minutes):
        log_line(
            log_path,
            f"SKIP restart (cooldown {cooldown_minutes}m lastRestartAt={state.get('lastRestartAt')})",
        )
        return state

    run_restart(root, log_path)
    state["lastRestartAt"] = utc_now_iso()
    state["consecutiveFails"] = 0
    write_state(state_path, state)
    return state


def try_acquire_singleton(root: Path) -> bool:
    """
    Write logs/watchdog.pid. If an existing live hub.watchdog owns the file, return False.
    Otherwise take over (stale pid or dead process).
    """
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    pid_path = logs / "watchdog.pid"
    existing = read_pid_file(pid_path)
    our = os.getpid()
    if existing is not None and existing != our:
        if is_watchdog_process(existing):
            return False
        # Stale or non-watchdog: take over
    write_pid_file(pid_path, our)
    return True


def run_daemon(
    root: Path,
    *,
    interval: int = DEFAULT_INTERVAL,
    max_fails: int = DEFAULT_MAX_FAILS,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
) -> int:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    pid_path = logs / "watchdog.pid"
    our = os.getpid()

    if not try_acquire_singleton(root):
        # Another daemon already running — exit quietly.
        return 0

    stop = {"flag": False}

    def _handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop["flag"] = True

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    log_path = day_log_path(logs)
    log_line(
        log_path,
        f"START daemon pid={our} interval={interval}s max_fails={max_fails} cooldown={cooldown_minutes}m",
    )

    try:
        while not stop["flag"]:
            # Re-claim pid file if something wiped it.
            if read_pid_file(pid_path) != our:
                write_pid_file(pid_path, our)
            try:
                one_cycle(
                    root,
                    max_fails=max_fails,
                    cooldown_minutes=cooldown_minutes,
                    poll_interval_seconds=interval,
                    mode="daemon",
                    pid=our,
                )
            except Exception as e:  # noqa: BLE001 — never die the loop on one bad cycle
                log_line(log_path, f"ERROR cycle: {e}")
            # Interruptible sleep
            end = time.monotonic() + max(1, int(interval))
            while time.monotonic() < end and not stop["flag"]:
                time.sleep(min(0.5, end - time.monotonic()))
    finally:
        remove_pid_file_if_ours(pid_path, our)
        log_line(log_path, f"STOP daemon pid={our}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Grok Remote Hub external health watchdog")
    p.add_argument(
        "--once",
        action="store_true",
        help="One check cycle then exit 0 (compat with old one-shot task)",
    )
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Poll interval seconds (daemon)")
    p.add_argument("--max-fails", type=int, default=DEFAULT_MAX_FAILS, help="Consecutive fails before restart")
    p.add_argument(
        "--cooldown-minutes",
        type=int,
        default=DEFAULT_COOLDOWN_MINUTES,
        help="Minutes between automatic restarts",
    )
    p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Repo root (default: parent of hub package)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    max_fails = max(1, int(args.max_fails))
    cooldown = max(0, int(args.cooldown_minutes))
    interval = max(1, int(args.interval))

    if args.once:
        one_cycle(
            root,
            max_fails=max_fails,
            cooldown_minutes=cooldown,
            poll_interval_seconds=interval,
            mode="once",
            pid=os.getpid(),
        )
        return 0

    return run_daemon(
        root,
        interval=interval,
        max_fails=max_fails,
        cooldown_minutes=cooldown,
    )


if __name__ == "__main__":
    raise SystemExit(main())
