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


def list_project_browse(projects_root: Path, rel: str = "") -> dict[str, object]:
    """List directories under projects_root for the New Session folder browser.

    Sandboxed: ``rel`` must resolve under ``projects_root`` only.
    Returns directories only; skips names starting with ``.``.
    """
    root = Path(projects_root).expanduser().resolve()
    raw = (rel or "").strip().replace("\\", "/").strip("/")

    if not raw:
        target = root
        rel_posix = ""
        parent: str | None = None
    else:
        target = resolve_under_root(projects_root, path=raw)
        try:
            rel_posix = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ProjectError("path escapes projects root") from exc
        if rel_posix in (".", ""):
            rel_posix = ""
            parent = None
        else:
            parent_parts = Path(rel_posix).parts[:-1]
            parent = "/".join(parent_parts) if parent_parts else ""

    if not target.exists():
        # Root missing: create it so Browse can open; never opaque "not found".
        if not raw:
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ProjectError(
                    f"projects root not found and could not create: {target} ({exc})"
                ) from exc
        else:
            raise ProjectError(f"not found: {target}")
    if not target.is_dir():
        raise ProjectError(f"not a directory: {target}")

    entries: list[dict[str, str]] = []
    try:
        children = list(target.iterdir())
    except OSError as exc:
        raise ProjectError(f"cannot list directory: {exc}") from exc

    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
            child_abs = child.resolve(strict=False)
        except OSError:
            continue
        if not _is_under(child_abs, root):
            continue
        try:
            child_rel = child_abs.relative_to(root).as_posix()
        except ValueError:
            continue
        entries.append(
            {
                "name": child.name,
                "path": child_rel,
                "absolute": str(child_abs),
            }
        )

    entries.sort(key=lambda e: e["name"].casefold())

    return {
        "projectsRoot": str(root),
        "path": rel_posix,
        "absolute": str(target.resolve(strict=False)),
        "parent": parent,
        "entries": entries,
    }
