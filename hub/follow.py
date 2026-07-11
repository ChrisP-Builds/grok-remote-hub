"""Desktop terminal follower: tail a Grok session's updates.jsonl live.

Read-only on disk. Does not talk to the hub server or the agent ACP socket.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from hub.config import load_config
from hub.history import (
    _message_from_update,
    _update_from_line,
    load_session_history,
)
from hub.session_index import SessionInfo, find_session, scan_sessions
from hub.session_tailer import _FileWatch, parse_updates_line

HISTORY_LIMIT = 40
POLL_INTERVAL = 0.25


@dataclass
class Colors:
    enabled: bool = False

    @property
    def green(self) -> str:
        return "\033[32m" if self.enabled else ""

    @property
    def cyan(self) -> str:
        return "\033[36m" if self.enabled else ""

    @property
    def dim(self) -> str:
        return "\033[2m" if self.enabled else ""

    @property
    def reset(self) -> str:
        return "\033[0m" if self.enabled else ""


@dataclass
class StreamState:
    """Track open same-line stream (assistant/thought) for live chunks."""

    open_role: str | None = None
    tool_seen: dict[str, str] = field(default_factory=dict)


def _normalize_path(p: str | Path) -> str:
    try:
        return str(Path(p).expanduser().resolve()).casefold()
    except (OSError, RuntimeError, ValueError):
        return str(p).replace("/", "\\").rstrip("\\").casefold()


def read_last_remote_session_id(project_root: Path | None = None) -> str | None:
    """Read hub-written logs/last-remote-session.txt if present."""
    roots: list[Path] = []
    if project_root is not None:
        roots.append(Path(project_root))
    roots.append(Path(__file__).resolve().parent.parent)
    for root in roots:
        path = root / "logs" / "last-remote-session.txt"
        try:
            if path.is_file():
                sid = path.read_text(encoding="utf-8").strip()
                if sid:
                    return sid
        except OSError:
            continue
    return None


def resolve_session(
    sessions_root: Path,
    session_id: str | None = None,
    cwd: str | None = None,
    limit: int = 500,
    prefer_last_remote: bool = True,
    project_root: Path | None = None,
) -> SessionInfo | None:
    """Resolve which session to follow.

    Priority: --session id, else last hub remote id (Safari stream), else
    most recent for --cwd, else most recent overall.
    """
    root = Path(sessions_root)
    if session_id:
        info = find_session(root, session_id.strip())
        if info is not None:
            return info
        # find_session only returns "useful" sessions; still try direct scan match
        for s in scan_sessions(root, limit=10_000):
            if s.sessionId == session_id.strip():
                return s
        return None

    if prefer_last_remote and not cwd:
        last = read_last_remote_session_id(project_root)
        if last:
            info = find_session(root, last)
            if info is not None:
                return info
            for s in scan_sessions(root, limit=10_000):
                if s.sessionId == last:
                    return s

    sessions = scan_sessions(root, limit=limit)
    if not sessions:
        return None

    if cwd:
        want = _normalize_path(cwd)
        for s in sessions:
            if s.cwd and _normalize_path(s.cwd) == want:
                return s
        # Prefix / containment fallback (project subdirs)
        for s in sessions:
            if not s.cwd:
                continue
            got = _normalize_path(s.cwd)
            if got.startswith(want) or want.startswith(got):
                return s
        return None

    return sessions[0]


def format_message_line(msg: dict[str, Any], verbose: bool, colors: Colors) -> str | None:
    """Format a normalized history message for the compact transcript. None = skip."""
    role = msg.get("role") or ""
    text = (msg.get("text") or "").strip()
    meta = msg.get("meta") or {}
    c, r = colors, colors.reset

    if role == "user":
        if not text:
            return None
        return f"{c.cyan}You:{r} {text}"

    if role == "assistant":
        if not text:
            return None
        return f"{c.green}Grok:{r} {text}"

    if role == "thought":
        if not verbose or not text:
            return None
        return f"{c.dim}  · thought: {text}{r}"

    if role == "tool":
        if not verbose:
            return None
        label = meta.get("label") or text or "tool"
        status = meta.get("status") or ""
        summary = meta.get("summary") or ""
        parts = [str(label)]
        if status:
            parts.append(f"[{status}]")
        if summary and summary not in str(label):
            parts.append(summary)
        return f"{c.dim}  · {' '.join(parts)}{r}"

    if role == "plan":
        if not verbose:
            return None
        entries = meta.get("entries") or []
        if not entries:
            return f"{c.dim}  · plan (empty){r}"
        n = len(entries)
        done = sum(1 for e in entries if (e.get("status") or "") == "completed")
        first = ""
        if entries and isinstance(entries[0], dict):
            first = (entries[0].get("content") or "").strip()
            if len(first) > 60:
                first = first[:59] + "…"
        extra = f": {first}" if first else ""
        return f"{c.dim}  · plan {done}/{n}{extra}{r}"

    if role == "system":
        if not verbose:
            return None
        return f"{c.dim}  · {text or 'system'}{r}"

    return None


def print_history(
    messages: list[dict[str, Any]],
    verbose: bool,
    colors: Colors,
    out: TextIO = sys.stdout,
    max_messages: int = HISTORY_LIMIT,
) -> None:
    if max_messages > 0 and len(messages) > max_messages:
        messages = messages[-max_messages:]
        print(f"{colors.dim}… ({len(messages)} recent messages){colors.reset}", file=out)
    for msg in messages:
        line = format_message_line(msg, verbose=verbose, colors=colors)
        if line is not None:
            print(line, file=out)
    out.flush()


def _flush_open_stream(state: StreamState, out: TextIO) -> None:
    if state.open_role is not None:
        print(file=out)
        state.open_role = None
        out.flush()


def handle_live_update(
    update: dict[str, Any],
    state: StreamState,
    verbose: bool,
    colors: Colors,
    out: TextIO = sys.stdout,
) -> None:
    """Print one live sessionUpdate payload; stream assistant chunks on one line."""
    msg = _message_from_update(update)
    if msg is None:
        return

    role = msg.get("role") or ""
    text = msg.get("text") or ""
    meta = msg.get("meta") or {}
    c, r = colors, colors.reset

    if role in ("user", "assistant", "thought"):
        # Same-role chunks continue; role switch ends the previous line
        if state.open_role is not None and state.open_role != role:
            _flush_open_stream(state, out)

        if role == "thought" and not verbose:
            return
        if not text:
            return

        if state.open_role == role:
            out.write(text)
            out.flush()
            return

        # Start a new line for this role
        if role == "user":
            out.write(f"{c.cyan}You:{r} {text}")
        elif role == "assistant":
            out.write(f"{c.green}Grok:{r} {text}")
        else:
            out.write(f"{c.dim}  · thought: {text}")
        state.open_role = role
        out.flush()
        return

    # Non-stream roles: close any open stream first
    _flush_open_stream(state, out)

    if role == "tool":
        if not verbose:
            return
        tool_id = str(meta.get("toolCallId") or "")
        label = meta.get("label") or text or "tool"
        status = meta.get("status") or ""
        # Avoid spamming identical tool updates
        key = f"{tool_id}:{label}:{status}"
        if tool_id and state.tool_seen.get(tool_id) == key:
            return
        if tool_id:
            state.tool_seen[tool_id] = key
        status_s = f" [{status}]" if status else ""
        print(f"{c.dim}  · {label}{status_s}{r}", file=out)
        out.flush()
        return

    if role == "plan":
        if not verbose:
            return
        entries = meta.get("entries") or []
        n = len(entries)
        done = sum(1 for e in entries if (e.get("status") or "") == "completed")
        print(f"{c.dim}  · plan {done}/{n}{r}", file=out)
        out.flush()
        return

    if role == "system" and verbose:
        print(f"{c.dim}  · {text or 'system'}{r}", file=out)
        out.flush()


def handle_live_line(
    line: str,
    state: StreamState,
    verbose: bool,
    colors: Colors,
    out: TextIO = sys.stdout,
) -> bool:
    """Parse one updates.jsonl line and print. Returns True if a message was handled."""
    obj = parse_updates_line(line)
    if not obj:
        return False
    update = _update_from_line(obj)
    if not update:
        return False
    handle_live_update(update, state, verbose=verbose, colors=colors, out=out)
    return True


def print_header(info: SessionInfo, colors: Colors, out: TextIO = sys.stdout) -> None:
    c, r = colors, colors.reset
    print(f"{c.cyan}=== Grok session follower ==={r}", file=out)
    print(f"Title:   {info.title}", file=out)
    print(f"Session: {info.sessionId}", file=out)
    print(f"Cwd:     {info.cwd or '(unknown)'}", file=out)
    print(f"Path:    {info.path}", file=out)
    print(f"{c.dim}Tailing updates.jsonl (Ctrl+C to exit){r}", file=out)
    print(file=out)
    out.flush()


def follow_session(
    info: SessionInfo,
    *,
    verbose: bool = False,
    colors: Colors | None = None,
    max_seconds: float | None = None,
    poll_interval: float = POLL_INTERVAL,
    history_limit: int = HISTORY_LIMIT,
    out: TextIO = sys.stdout,
    sessions_root: Path | None = None,
) -> int:
    """Load history, then tail updates.jsonl from EOF until interrupt or max_seconds."""
    if colors is None:
        colors = Colors(enabled=getattr(out, "isatty", lambda: False)())

    print_header(info, colors, out=out)

    session_path = Path(info.path)
    messages = load_session_history(
        sessions_root or session_path.parent,
        info.sessionId,
        session_path=session_path,
        max_messages=max(history_limit * 4, 200),
    )
    # Prefer user/assistant for compact view; history already includes all roles
    print_history(messages, verbose=verbose, colors=colors, out=out, max_messages=history_limit)
    print(file=out)
    print(f"{colors.dim}--- live ---{colors.reset}", file=out)
    out.flush()

    updates = session_path / "updates.jsonl"
    watch = _FileWatch(updates)
    watch.open_at_end()
    state = StreamState()
    started = time.monotonic()

    try:
        while True:
            if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
                _flush_open_stream(state, out)
                print(f"\n{colors.dim}(max-seconds reached){colors.reset}", file=out)
                return 0
            try:
                lines = watch.read_new_lines()
            except OSError:
                lines = []
            for line in lines:
                handle_live_line(line, state, verbose=verbose, colors=colors, out=out)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        _flush_open_stream(state, out)
        print(f"\n{colors.dim}Stopped.{colors.reset}", file=out)
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m hub.follow",
        description="Follow a Grok session in the terminal (read-only tail of updates.jsonl).",
    )
    p.add_argument(
        "--session",
        metavar="ID",
        default=None,
        help="Session UUID to follow",
    )
    p.add_argument(
        "--cwd",
        metavar="PATH",
        default=None,
        help="Follow most recent session for this project cwd",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (for sessions_root)",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show thoughts, tools, and plan lines",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors",
    )
    p.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        metavar="N",
        help="Exit after N seconds (for smoke tests)",
    )
    p.add_argument(
        "--history",
        type=int,
        default=HISTORY_LIMIT,
        metavar="N",
        help=f"Recent history messages to print (default {HISTORY_LIMIT})",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = load_config(args.config)
    sessions_root = config.sessions_root

    info = resolve_session(
        sessions_root,
        session_id=args.session,
        cwd=args.cwd,
    )
    if info is None:
        if args.session:
            print(f"Session not found: {args.session}", file=sys.stderr)
        elif args.cwd:
            print(f"No session found for cwd: {args.cwd}", file=sys.stderr)
        else:
            print(f"No sessions under {sessions_root}", file=sys.stderr)
        return 1

    use_color = (not args.no_color) and sys.stdout.isatty()
    colors = Colors(enabled=use_color)

    return follow_session(
        info,
        verbose=args.verbose,
        colors=colors,
        max_seconds=args.max_seconds,
        history_limit=args.history,
        sessions_root=sessions_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
