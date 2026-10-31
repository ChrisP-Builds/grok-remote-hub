"""In-hub static HTML site preview: plan, one active site root, path resolve."""

from __future__ import annotations

import mimetypes
import os
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from hub.fs_browser import FsBrowserError, resolve_file_for_read

HTML_EXTS = {".html", ".htm"}
PREVIEW_URL_PREFIX = "/preview-site/"
# Opaque origin: previewed pages cannot read hub storage or call the hub API (ADR-026).
PREVIEW_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"


class SitePreviewError(Exception):
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
    try:
        Path(os.path.normcase(str(path))).relative_to(
            Path(os.path.normcase(str(root)))
        )
        return True
    except ValueError:
        return False


def build_preview_plan(
    projects_root: Path,
    session_root: str | Path,
    rel_path: str,
) -> dict[str, Any]:
    """Resolve an HTML file and return the site root (parent dir) + entry.

    Relative CSS/JS resolve against the HTML file's parent directory.
    """
    try:
        file_path = resolve_file_for_read(projects_root, session_root, rel_path)
    except FsBrowserError as exc:
        raise SitePreviewError(exc.message, exc.status) from exc

    if file_path.suffix.lower() not in HTML_EXTS:
        raise SitePreviewError("not an html file", 400)

    site_root = file_path.parent.resolve()
    entry_rel = file_path.name
    return {
        "site_root": site_root,
        "entry_rel": entry_rel,
        "entry_url_path": entry_rel,
        "session_root": str(Path(session_root).expanduser().resolve()),
    }


class SitePreviewManager:
    """At most one active preview site root for the hub process."""

    def __init__(self) -> None:
        self.active: dict[str, Any] | None = None

    def start(self, plan: dict[str, Any]) -> dict[str, Any]:
        site_root = Path(plan["site_root"]).resolve()
        entry_rel = str(plan.get("entry_rel") or plan.get("entry_url_path") or "")
        if not entry_rel or ".." in Path(entry_rel).parts:
            raise SitePreviewError("invalid entry", 400)
        self.active = {
            "key": secrets.token_urlsafe(18),
            "site_root": site_root,
            "entry_rel": entry_rel,
            "session_root": str(plan.get("session_root") or ""),
            "started_at": time.time(),
        }
        return dict(self.active)

    def stop(self) -> None:
        self.active = None

    def preview_url(self) -> str | None:
        if not self.active:
            return None
        return f"{PREVIEW_URL_PREFIX}{self.active['key']}/{self.active['entry_rel']}"

    def resolve_keyed(self, url_path: str) -> Path | None:
        """Resolve ``<key>/<path>``; None unless the key matches the active preview."""
        if not self.active:
            return None
        key, _, rest = str(url_path or "").lstrip("/").partition("/")
        if not secrets.compare_digest(key.encode(), str(self.active["key"]).encode()):
            return None
        return self.resolve_file(rest)

    def status(self) -> dict[str, Any]:
        if not self.active:
            return {"active": False}
        a = self.active
        return {
            "active": True,
            "siteRoot": str(a["site_root"]),
            "entryRel": a["entry_rel"],
            "sessionRoot": a.get("session_root") or "",
            "startedAt": a.get("started_at"),
        }

    def resolve_file(self, url_path: str) -> Path | None:
        """Map a URL path under /preview-site/ to a file under active site_root.

        Returns None if no active preview or path invalid/missing.
        Directories resolve to index.html when present.
        """
        if not self.active:
            return None
        site_root: Path = self.active["site_root"]
        if not site_root.is_dir():
            return None

        raw = (url_path or "").strip().lstrip("/")
        # Empty path → site root (directory index)
        if not raw:
            candidate = site_root
        else:
            rel = Path(raw)
            if rel.is_absolute() or ".." in rel.parts:
                return None
            try:
                candidate = (site_root / rel).resolve()
            except (OSError, RuntimeError):
                return None

        if not _under(candidate, site_root):
            return None

        if candidate.is_dir():
            index = candidate / "index.html"
            if index.is_file() and _under(index.resolve(), site_root):
                return index
            return None

        if candidate.is_file():
            return candidate
        return None


async def serve_preview(manager: SitePreviewManager, request: web.Request) -> web.StreamResponse:
    file_path = manager.resolve_keyed(request.match_info.get("path") or "")
    if file_path is None:
        return web.Response(
            text="not found",
            status=404,
            content_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )
    ctype, _ = mimetypes.guess_type(str(file_path))
    if not ctype:
        ctype = "application/octet-stream"
    if ctype == "text/html":
        ctype = "text/html; charset=utf-8"
    return web.FileResponse(
        path=file_path,
        headers={
            "Content-Type": ctype,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": PREVIEW_CSP,
            # Sandboxed documents send Origin: null; module scripts, fonts and
            # fetch() of sibling files need CORS. Safe: content is key-gated.
            "Access-Control-Allow-Origin": "*",
        },
    )
