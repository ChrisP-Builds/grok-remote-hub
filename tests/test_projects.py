"""Unit tests for project path sanitize, root validation, and create."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub.projects import (
    ProjectError,
    create_project,
    resolve_under_root,
    sanitize_project_name,
)


def test_sanitize_strips_and_replaces_invalid_chars() -> None:
    assert sanitize_project_name("  My Project  ") == "My Project"
    assert sanitize_project_name('a<b>c:d"e/f\\g|h?i*j') == "a-b-c-d-e-f-g-h-i-j"
    assert sanitize_project_name("ok-name_1") == "ok-name_1"


def test_sanitize_rejects_empty() -> None:
    with pytest.raises(ProjectError, match="name required"):
        sanitize_project_name("")
    with pytest.raises(ProjectError, match="name required"):
        sanitize_project_name("   ")
    with pytest.raises(ProjectError, match="name required"):
        sanitize_project_name(".")
    with pytest.raises(ProjectError, match="name required"):
        sanitize_project_name("..")


def test_sanitize_invalid_only_chars_become_dashes() -> None:
    # Each invalid char becomes '-'; not empty after sanitize
    assert sanitize_project_name('<>:"/\\|?*') == "---------"


def test_resolve_name_under_root(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    got = resolve_under_root(root, name="App One")
    assert got == (root / "App One").resolve()
    assert got.parent == root.resolve()


def test_resolve_rejects_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    with pytest.raises(ProjectError, match="escapes"):
        resolve_under_root(root, path=str(root / ".." / "Outside"))
    with pytest.raises(ProjectError, match="escapes|invalid"):
        resolve_under_root(root, path="..\\Outside")
    with pytest.raises(ProjectError, match="escapes"):
        resolve_under_root(root, path="foo/../../Outside")


def test_resolve_rejects_absolute_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    outside = tmp_path / "Elsewhere" / "Nope"
    with pytest.raises(ProjectError, match="escapes"):
        resolve_under_root(root, path=str(outside))


def test_resolve_accepts_absolute_under_root(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    target = root / "Nested" / "App"
    got = resolve_under_root(root, path=str(target))
    assert got == target.resolve()


def test_resolve_requires_name_or_path(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    with pytest.raises(ProjectError, match="name or path"):
        resolve_under_root(root)
    with pytest.raises(ProjectError, match="name or path"):
        resolve_under_root(root, name="", path="")


def test_create_project_mkdir(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    result = create_project(root, name="Fresh App")
    assert result["created"] is True
    assert result["name"] == "Fresh App"
    path = Path(str(result["path"]))
    assert path.is_dir()
    assert path.parent == root.resolve()


def test_create_project_existing_dir(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    existing = root / "Already"
    existing.mkdir(parents=True)
    result = create_project(root, name="Already")
    assert result["created"] is False
    assert Path(str(result["path"])) == existing.resolve()
    assert result["name"] == "Already"


def test_create_project_existing_file_errors(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    file_path = root / "NotADir"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ProjectError, match="file"):
        create_project(root, name="NotADir")


def test_create_project_path_body(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    root.mkdir()
    target = root / "Via Path"
    result = create_project(root, path=str(target))
    assert result["created"] is True
    assert Path(str(result["path"])).is_dir()
