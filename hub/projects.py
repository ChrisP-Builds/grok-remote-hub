"""Project folder creation under the configured projects root."""

from __future__ import annotations

import re
from pathlib import Path

# Windows-invalid filename characters: < > : " / \ | ? *
_INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*]')


class ProjectError(ValueError):
    """User-facing validation error for project create."""


def sanitize_project_name(name: str) -> str:
    """Strip, replace invalid Windows filename chars with '-', reject empty."""
    raw = (name or "").strip()
    cleaned = _INVALID_WIN_CHARS.sub("-", raw)
    # Collapse path separators / dots that could escape after sanitize
    cleaned = cleaned.strip(" .")
    if not cleaned:
        raise ProjectError("name required")
    if cleaned in {".", ".."} or ".." in cleaned.split("/"):
        raise ProjectError("invalid name")
    return cleaned


def resolve_under_root(projects_root: Path, *, name: str | None = None, path: str | None = None) -> Path:
    """Resolve a project folder path that must stay under projects_root.

    Accepts either ``name`` (folder under root) or ``path`` (absolute or relative).
    Rejects path escape, ``..``, and absolute paths outside root.
    """
    root = Path(projects_root).expanduser().resolve()

    if path is not None and str(path).strip():
        raw = str(path).strip()
        # Reject explicit parent traversal in the input string
        parts = Path(raw).parts
        if ".." in parts:
            raise ProjectError("path escapes projects root")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise ProjectError(f"invalid path: {exc}") from exc
        if not _is_under(resolved, root):
            raise ProjectError("path escapes projects root")
        return resolved

    if name is not None and str(name).strip():
        safe = sanitize_project_name(str(name))
        if Path(safe).is_absolute() or ".." in Path(safe).parts or "/" in safe or "\\" in safe:
            raise ProjectError("invalid name")
        resolved = (root / safe).resolve(strict=False)
        if not _is_under(resolved, root):
            raise ProjectError("path escapes projects root")
        return resolved

    raise ProjectError("name or path required")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def create_project(
    projects_root: Path,
    *,
    name: str | None = None,
    path: str | None = None,
) -> dict[str, object]:
    """Create a project folder under root. Returns path, name, created.

    - mkdir if missing
    - if exists as directory: return it with created=false
    - if exists as file: raise ProjectError (400)
    """
    target = resolve_under_root(projects_root, name=name, path=path)
    folder_name = target.name

    if target.exists():
        if target.is_file():
            raise ProjectError("path exists as a file")
        if target.is_dir():
            return {"path": str(target), "name": folder_name, "created": False}
        raise ProjectError("path exists and is not a directory")

    target.mkdir(parents=True, exist_ok=True)
    return {"path": str(target), "name": folder_name, "created": True}
