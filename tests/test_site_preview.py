"""Unit tests for in-hub static HTML site preview."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hub.site_preview import (
    SitePreviewError,
    SitePreviewManager,
    build_preview_plan,
    serve_preview,
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
    """start, GET page + subresource via capability URL, stop (real handler)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    projects, root = _site(tmp_path)
    mgr = SitePreviewManager()

    async def serve(request: web.Request) -> web.StreamResponse:
        return await serve_preview(mgr, request)

    async def run() -> None:
        app = web.Application()
        app.router.add_get("/preview-site/{path:.*}", serve)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.start(build_preview_plan(projects, root, "index.html"))
            url = mgr.preview_url()
            assert url is not None
            key = url.split("/")[2]
            assert url == f"/preview-site/{key}/index.html"
            assert len(key) >= 20

            page = await client.get(url)
            assert page.status == 200
            assert "css/a.css" in await page.text()
            csp = page.headers.get("Content-Security-Policy", "")
            assert csp.startswith("sandbox ")
            assert "allow-scripts" in csp
            assert "allow-same-origin" not in csp
            assert page.headers.get("Access-Control-Allow-Origin") == "*"

            css = await client.get(f"/preview-site/{key}/css/a.css")
            assert css.status == 200
            assert "color" in await css.text()

            assert (await client.get("/preview-site/css/a.css")).status == 404
            assert (await client.get("/preview-site/wrongkey/index.html")).status == 404

            mgr.start(build_preview_plan(projects, root, "index.html"))
            assert (await client.get(url)).status == 404, "old key must die on restart"

            mgr.stop()
            assert (await client.get(mgr.preview_url() or url)).status == 404
        finally:
            await client.close()

    asyncio.run(run())


def test_preview_iframe_sandbox_has_no_same_origin_escape() -> None:
    """allow-scripts + allow-same-origin lets the framed page drop its sandbox."""
    import re

    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'id="site-preview-frame"[^>]*sandbox="([^"]*)"', html, re.S)
    assert m, "preview iframe must declare sandbox"
    assert "allow-scripts" in m.group(1)
    assert "allow-same-origin" not in m.group(1)


def test_preview_iframe_sends_no_referrer() -> None:
    """The SPA URL carries ?token=, so a framed page must not see it as document.referrer."""
    import re

    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<iframe[^>]*id="site-preview-frame"[^>]*>', html, re.S)
    assert m, "preview iframe missing"
    assert 'referrerpolicy="no-referrer"' in m.group(0)
