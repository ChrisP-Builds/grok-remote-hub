"""Unit tests for in-hub static HTML site preview."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hub.site_preview import (
    SitePreviewError,
    SitePreviewManager,
    build_preview_plan,
)


def _site(tmp_path: Path) -> tuple[Path, Path]:
    projects = tmp_path / "Projects"
    root = projects / "App"
    root.mkdir(parents=True)
    (root / "css").mkdir()
    (root / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="css/a.css"><h1>hi</h1>\n',
        encoding="utf-8",
    )
    (root / "css" / "a.css").write_text("h1{color:red}\n", encoding="utf-8")
    (root / "notes.txt").write_text("not html\n", encoding="utf-8")
    return projects, root


def test_build_preview_plan_index(tmp_path: Path) -> None:
    projects, root = _site(tmp_path)
    plan = build_preview_plan(projects, root, "index.html")
    assert plan["site_root"] == root.resolve()
    assert plan["entry_rel"] == "index.html"
    assert plan["entry_url_path"] == "index.html"
    assert Path(plan["session_root"]) == root.resolve()


def test_build_preview_plan_nested_html(tmp_path: Path) -> None:
    projects, root = _site(tmp_path)
    sub = root / "pages"
    sub.mkdir()
    (sub / "about.html").write_text(
        "<html><body>about</body></html>\n", encoding="utf-8"
    )
    plan = build_preview_plan(projects, root, "pages/about.html")
    assert plan["site_root"] == sub.resolve()
    assert plan["entry_rel"] == "about.html"


def test_reject_non_html(tmp_path: Path) -> None:
    projects, root = _site(tmp_path)
    with pytest.raises(SitePreviewError) as ei:
        build_preview_plan(projects, root, "notes.txt")
    assert ei.value.status == 400
    assert "html" in ei.value.message.lower()


def test_reject_missing(tmp_path: Path) -> None:
    projects, root = _site(tmp_path)
    with pytest.raises(SitePreviewError) as ei:
        build_preview_plan(projects, root, "nope.html")
    assert ei.value.status == 404


def test_manager_resolve_and_traversal(tmp_path: Path) -> None:
    projects, root = _site(tmp_path)
    plan = build_preview_plan(projects, root, "index.html")
    mgr = SitePreviewManager()
    assert mgr.resolve_file("index.html") is None  # not started

    mgr.start(plan)
    assert mgr.status()["active"] is True

    idx = mgr.resolve_file("index.html")
    assert idx is not None
    assert idx.name == "index.html"
    assert idx.read_text(encoding="utf-8").startswith("<!doctype")

    css = mgr.resolve_file("css/a.css")
    assert css is not None
    assert css.name == "a.css"

    # Directory → index.html
    at_root = mgr.resolve_file("")
    assert at_root is not None
    assert at_root.name == "index.html"
    at_slash = mgr.resolve_file("/")
    assert at_slash is not None
    assert at_slash.name == "index.html"

    # Path traversal blocked
    assert mgr.resolve_file("../notes.txt") is None
    assert mgr.resolve_file("css/../../notes.txt") is None
    assert mgr.resolve_file("..\\..\\Windows\\win.ini") is None

    mgr.stop()
    assert mgr.status()["active"] is False
    assert mgr.resolve_file("index.html") is None


def test_start_replaces_previous(tmp_path: Path) -> None:
    projects, root = _site(tmp_path)
    other = root / "other"
    other.mkdir()
    (other / "page.html").write_text("<html>x</html>\n", encoding="utf-8")

    mgr = SitePreviewManager()
    mgr.start(build_preview_plan(projects, root, "index.html"))
    assert mgr.resolve_file("css/a.css") is not None

    mgr.start(build_preview_plan(projects, root, "other/page.html"))
    assert mgr.active is not None
    assert mgr.active["entry_rel"] == "page.html"
    # Site root is other/; css from parent site is not served
    assert mgr.resolve_file("css/a.css") is None
    assert mgr.resolve_file("page.html") is not None


def test_preview_http_roundtrip(tmp_path: Path) -> None:
    """start → GET preview assets → stop (aiohttp TestClient, no pytest plugin)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    projects, root = _site(tmp_path)
    mgr = SitePreviewManager()

    async def start(request: web.Request) -> web.Response:
        body = await request.json()
        plan = build_preview_plan(projects, body["root"], body["path"])
        mgr.start(plan)
        entry = plan["entry_url_path"]
        return web.json_response(
            {
                "ok": True,
                "previewUrl": f"/preview-site/{entry}",
                "hubUrl": "/preview-site/",
            }
        )

    async def stop(request: web.Request) -> web.Response:
        mgr.stop()
        return web.json_response({"ok": True})

    async def serve(request: web.Request) -> web.Response:
        if not mgr.active:
            return web.Response(text="no active preview", status=404)
        path = request.match_info.get("path") or ""
        fp = mgr.resolve_file(path)
        if fp is None:
            return web.Response(text="not found", status=404)
        return web.FileResponse(path=fp, headers={"Cache-Control": "no-store"})

    async def run() -> None:
        app = web.Application()
        app.router.add_post("/api/preview/start", start)
        app.router.add_post("/api/preview/stop", stop)
        app.router.add_get("/preview-site", serve)
        app.router.add_get("/preview-site/", serve)
        app.router.add_get("/preview-site/{path:.*}", serve)

        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            r = await client.post(
                "/api/preview/start",
                json={"root": str(root), "path": "index.html"},
            )
            assert r.status == 200
            data = await r.json()
            assert data["ok"] is True
            preview = data["previewUrl"]

            page = await client.get(preview)
            assert page.status == 200
            body = await page.text()
            assert "css/a.css" in body

            css = await client.get("/preview-site/css/a.css")
            assert css.status == 200
            assert "color" in await css.text()

            stop_r = await client.post("/api/preview/stop")
            assert stop_r.status == 200
            gone = await client.get(preview)
            assert gone.status == 404
        finally:
            await client.close()

    asyncio.run(run())
