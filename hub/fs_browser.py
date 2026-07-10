"""Sandboxed filesystem browser: list, read, and write under a project root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class FsBrowserError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _under(path: Path, root: Path) -> bool:
    """Return True if path is root or a descendant (Windows case-insensitive)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass
    # Case-insensitive fallback (Windows drive/path casing)
    try:
        Path(os.path.normcase(str(path))).relative_to(Path(os.path.normcase(str(root))))
        return True
    except ValueError:
        return False


def _normalize_rel(rel: str) -> str:
    """Normalize relative path string to forward-slash form, empty for root."""
    raw = (rel or "").strip()
    if not raw:
        return ""
    return Path(raw).as_posix().strip("/")


def resolve_sandbox(projects_root: Path, root: str | Path, rel: str = "") -> Path:
    """Resolve a path under root, ensuring root stays under projects_root."""
    projects_resolved = Path(projects_root).expanduser().resolve()
    root_resolved = Path(root).expanduser().resolve()

    if not _under(root_resolved, projects_resolved):
        raise FsBrowserError("root escapes projects root", 400)

    rel_str = "" if rel is None else str(rel)
    if rel_str:
        rel_path = Path(rel_str)
        if rel_path.is_absolute():
            raise FsBrowserError("path must be relative", 400)
        if ".." in rel_path.parts:
            raise FsBrowserError("path escapes root", 400)
        target = (root_resolved / rel_path).resolve()
    else:
        target = root_resolved

    if not _under(target, root_resolved):
        raise FsBrowserError("path escapes root", 400)

    return target


def _resolved_root(projects_root: Path, root: str | Path) -> Path:
    projects_resolved = Path(projects_root).expanduser().resolve()
    root_resolved = Path(root).expanduser().resolve()
    if not _under(root_resolved, projects_resolved):
        raise FsBrowserError("root escapes projects root", 400)
    return root_resolved


def list_dir(projects_root: Path, root: str | Path, rel: str = "") -> dict[str, Any]:
    path = resolve_sandbox(projects_root, root, rel)
    root_resolved = _resolved_root(projects_root, root)

    if not path.exists():
        raise FsBrowserError("not found", 404)
    if not path.is_dir():
        raise FsBrowserError("not a directory", 400)

    entries: list[dict[str, Any]] = []
    try:
        children = list(path.iterdir())
    except OSError as exc:
        raise FsBrowserError(f"cannot list directory: {exc}", 400) from exc

    for child in children:
        try:
            is_dir = child.is_dir()
            if is_dir:
                entries.append({"name": child.name, "type": "dir", "size": None})
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
                entries.append({"name": child.name, "type": "file", "size": size})
        except OSError:
            continue

    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].casefold()))

    return {
        "root": str(root_resolved),
        "path": _normalize_rel(rel),
        "entries": entries,
    }


def read_text(
    projects_root: Path,
    root: str | Path,
    rel: str,
    *,
    max_bytes: int = 1_500_000,
) -> dict[str, Any]:
    path = resolve_sandbox(projects_root, root, rel)
    root_resolved = _resolved_root(projects_root, root)

    if not path.exists():
        raise FsBrowserError("not found", 404)
    if path.is_dir():
        raise FsBrowserError("not a file", 400)

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FsBrowserError(f"cannot read file: {exc}", 400) from exc

    if len(raw) > max_bytes:
        raise FsBrowserError("file too large", 413)
    if b"\x00" in raw[:8192]:
        raise FsBrowserError("binary file", 415)

    content = raw.decode("utf-8", errors="replace")
    return {
        "root": str(root_resolved),
        "path": _normalize_rel(rel),
        "content": content,
        "size": len(raw),
        "truncated": False,
    }


def write_text(
    projects_root: Path,
    root: str | Path,
    rel: str,
    content: str,
    *,
    max_bytes: int = 1_500_000,
) -> dict[str, Any]:
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)

    raw = content.encode("utf-8")
    if len(raw) > max_bytes:
        raise FsBrowserError("file too large", 413)

    path = resolve_sandbox(projects_root, root, rel)
    root_resolved = _resolved_root(projects_root, root)

    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise FsBrowserError("parent not found", 404)

    tmp = path.with_suffix(path.suffix + ".hubtmp")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        path.write_bytes(raw)

    return {
        "root": str(root_resolved),
        "path": _normalize_rel(rel),
        "size": len(raw),
    }
