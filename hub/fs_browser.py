"""Sandboxed filesystem browser: list, read, and write under a session root."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
# Raw media serve cap (images + video); keep high enough for phone-friendly clips.
RAW_MAX_BYTES = 150_000_000

# Binary upload caps (composer / Files panel).
UPLOAD_MAX_IMAGE_BYTES = 40_000_000
UPLOAD_MAX_VIDEO_BYTES = 150_000_000
# HEIC/HEIF for iPhone camera rolls; not required for in-browser preview.
UPLOAD_IMAGE_EXTS = IMAGE_EXTS | {".heic", ".heif"}
UPLOAD_VIDEO_EXTS = set(VIDEO_EXTS)
UPLOAD_ALLOWED_EXTS = UPLOAD_IMAGE_EXTS | UPLOAD_VIDEO_EXTS


class FsBrowserError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def is_image_path(rel_or_name: str) -> bool:
    return Path(str(rel_or_name)).suffix.lower() in IMAGE_EXTS


def is_video_path(rel_or_name: str) -> bool:
    return Path(str(rel_or_name)).suffix.lower() in VIDEO_EXTS


def is_upload_allowed_ext(rel_or_name: str) -> bool:
    return Path(str(rel_or_name)).suffix.lower() in UPLOAD_ALLOWED_EXTS


def resolve_file_for_read(
    projects_root: Path, root: str | Path, rel: str
) -> Path:
    """Resolve sandboxed path and ensure it is an existing file."""
    path = resolve_sandbox(projects_root, root, rel)
    if not path.exists():
        raise FsBrowserError("not found", 404)
    if path.is_dir():
        raise FsBrowserError("not a file", 400)
    return path


def content_type_for(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    if ctype:
        return ctype
    return "application/octet-stream"


def content_disposition_attachment(filename: str) -> str:
    """Build Content-Disposition attachment header; basename only, no path segments."""
    base = Path(str(filename or "")).name
    safe = (
        base.replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
        .replace("\\", "")
        .strip()
    )
    if not safe or safe in {".", ".."}:
        safe = "download"
    return f'attachment; filename="{safe}"'


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
    """Resolve a path under session root (cwd).

    Primary sandbox is ``root`` itself: all paths must stay under the resolved
    session root. ``projects_root`` is kept for call-site compatibility and is
    not used as a boundary check (session cwd may be outside projects_root).
    """
    del projects_root  # API compatibility only; sandbox is session root
    root_resolved = Path(root).expanduser().resolve()

    if not root_resolved.is_absolute():
        raise FsBrowserError("root must be absolute", 400)

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
    del projects_root  # API compatibility only
    root_resolved = Path(root).expanduser().resolve()
    if not root_resolved.is_absolute():
        raise FsBrowserError("root must be absolute", 400)
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


def sanitize_upload_filename(name: str) -> str:
    """Basename-only safe filename for uploads; keep alnum, dash, underscore, dot."""
    base = Path(str(name or "")).name
    if not base or base in {".", ".."}:
        return "upload.bin"

    # Drop path-ish separators if any remain after Path.name (e.g. mixed).
    base = base.replace("\\", "/").split("/")[-1]
    if not base or base in {".", ".."}:
        return "upload.bin"

    orig_ext = Path(base).suffix.lower()
    safe_chars: list[str] = []
    for ch in base:
        if ch.isalnum() or ch in "-_.":
            safe_chars.append(ch)
    safe = "".join(safe_chars).lstrip(".")
    # Collapse empty / dot-only results
    if not safe or safe in {".", ".."} or all(c == "." for c in safe):
        if orig_ext and orig_ext in UPLOAD_ALLOWED_EXTS:
            return f"upload{orig_ext}"
        return "upload.bin"

    # Preserve allowed extension; if sanitize ate it, re-attach when known.
    safe_ext = Path(safe).suffix.lower()
    if not safe_ext and orig_ext and orig_ext in UPLOAD_ALLOWED_EXTS:
        safe = f"{safe}{orig_ext}"
    return safe


def _unique_upload_path(dir_path: Path, filename: str) -> Path:
    """Pick dir_path/filename or filename-1.ext, filename-2.ext, … if taken."""
    candidate = dir_path / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 1
    while True:
        alt = dir_path / f"{stem}-{n}{suffix}"
        if not alt.exists():
            return alt
        n += 1
        if n > 10_000:
            raise FsBrowserError("cannot allocate unique filename", 400)


def write_upload_bytes(
    projects_root: Path,
    root: str | Path,
    rel_dir: str,
    filename: str,
    data: bytes,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Write binary media under session root (default dir: uploads/).

    Does not log or return the body. Prefer unique filenames over overwrite.
    """
    del content_type  # reserved for future MIME sniffing; extension is authoritative

    if data is None:
        data = b""
    if not isinstance(data, (bytes, bytearray)):
        raise FsBrowserError("invalid body", 400)
    raw = bytes(data)

    rel_dir_norm = _normalize_rel(rel_dir if rel_dir is not None else "uploads")
    if not rel_dir_norm:
        rel_dir_norm = "uploads"

    safe_name = sanitize_upload_filename(filename)
    ext = Path(safe_name).suffix.lower()
    if ext not in UPLOAD_ALLOWED_EXTS:
        raise FsBrowserError("unsupported media type", 415)

    if ext in UPLOAD_IMAGE_EXTS:
        max_bytes = UPLOAD_MAX_IMAGE_BYTES
    else:
        max_bytes = UPLOAD_MAX_VIDEO_BYTES
    if len(raw) > max_bytes:
        raise FsBrowserError("file too large", 413)

    # Resolve directory under sandbox; auto-create uploads/ (and nested) if missing.
    dir_path = resolve_sandbox(projects_root, root, rel_dir_norm)
    root_resolved = _resolved_root(projects_root, root)
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FsBrowserError(f"cannot create directory: {exc}", 400) from exc
    if not dir_path.is_dir():
        raise FsBrowserError("not a directory", 400)

    # Unique target; re-check sandbox for the final file path.
    target = _unique_upload_path(dir_path, safe_name)
    try:
        rel_out = target.relative_to(root_resolved).as_posix()
    except ValueError as exc:
        raise FsBrowserError("path escapes root", 400) from exc
    # Final resolve rejects escape / absolute
    path = resolve_sandbox(projects_root, root, rel_out)

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
        try:
            path.write_bytes(raw)
        except OSError as exc:
            raise FsBrowserError(f"cannot write file: {exc}", 400) from exc

    return {
        "root": str(root_resolved),
        "path": _normalize_rel(rel_out),
        "size": len(raw),
    }
