"""Pure session list search helpers (title / id / cwd basename)."""

from __future__ import annotations

from pathlib import PurePath, PurePosixPath, PureWindowsPath


def _cwd_basename(cwd: str) -> str:
    raw = str(cwd or "").strip().rstrip("/\\")
    if not raw:
        return ""
    # Prefer OS-native path semantics; fall back to posix/win for cross-platform tests.
    name = PurePath(raw).name
    if name:
        return name
    name = PureWindowsPath(raw).name
    if name:
        return name
    return PurePosixPath(raw).name


def session_matches_query(
    title: str,
    session_id: str,
    cwd: str,
    query: str,
) -> bool:
    """True when query is empty or matches title, session id, or cwd basename.

    Match is case-insensitive substring for title and basename; session id
    matches as substring (full or partial uuid) case-insensitive.
    """
    q = str(query or "").strip().lower()
    if not q:
        return True
    if q in str(title or "").lower():
        return True
    if q in str(session_id or "").lower():
        return True
    base = _cwd_basename(cwd).lower()
    if base and q in base:
        return True
    # Also allow full cwd path match (parity with SPA filter).
    if q in str(cwd or "").lower():
        return True
    return False
