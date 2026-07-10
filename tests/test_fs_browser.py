"""Unit tests for sandboxed fs_browser list/read/write."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub.fs_browser import (
    FsBrowserError,
    list_dir,
    read_text,
    resolve_sandbox,
    write_text,
)


def _projects_and_root(tmp_path: Path) -> tuple[Path, Path]:
    projects = tmp_path / "Projects"
    root = projects / "App"
    root.mkdir(parents=True)
    return projects, root


def test_list_root_file_and_dir_dirs_first(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    (root / "zebra.txt").write_text("z", encoding="utf-8")
    (root / "Alpha").mkdir()
    (root / "beta").mkdir()
    (root / "Apple.txt").write_text("a", encoding="utf-8")

    result = list_dir(projects, root)
    names = [e["name"] for e in result["entries"]]
    types = [e["type"] for e in result["entries"]]

    assert types[:2] == ["dir", "dir"]
    assert names[:2] == ["Alpha", "beta"]
    assert names[2:] == ["Apple.txt", "zebra.txt"]
    assert all(e["type"] == "file" for e in result["entries"][2:])

    for e in result["entries"]:
        if e["type"] == "dir":
            assert e["size"] is None
        else:
            assert isinstance(e["size"], int)

    assert Path(result["root"]) == root.resolve()
    assert result["path"] == ""


def test_list_subdir_path_normalized(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    sub = root / "src"
    sub.mkdir()
    (sub / "main.py").write_text("print(1)\n", encoding="utf-8")

    result = list_dir(projects, root, "src")
    assert result["path"] == "src"
    assert len(result["entries"]) == 1
    assert result["entries"][0]["name"] == "main.py"
    assert result["entries"][0]["type"] == "file"


def test_read_text(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    (root / "notes.txt").write_text("hello world", encoding="utf-8")

    result = read_text(projects, root, "notes.txt")
    assert result["content"] == "hello world"
    assert result["size"] == len(b"hello world")
    assert result["truncated"] is False
    assert result["path"] == "notes.txt"
    assert Path(result["root"]) == root.resolve()


def test_write_updates_content(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    path = root / "out.txt"
    path.write_text("old", encoding="utf-8")

    result = write_text(projects, root, "out.txt", "new content")
    assert path.read_text(encoding="utf-8") == "new content"
    assert result["size"] == len("new content".encode("utf-8"))
    assert result["path"] == "out.txt"
    assert Path(result["root"]) == root.resolve()


def test_write_new_file_when_parent_exists(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    result = write_text(projects, root, "fresh.txt", "created")
    assert (root / "fresh.txt").read_text(encoding="utf-8") == "created"
    assert result["size"] == len(b"created")


def test_rel_dotdot_escape_400(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    with pytest.raises(FsBrowserError) as exc:
        resolve_sandbox(projects, root, "..")
    assert exc.value.status == 400
    assert "escape" in exc.value.message.lower() or ".." in exc.value.message

    with pytest.raises(FsBrowserError) as exc2:
        list_dir(projects, root, "foo/../../etc")
    assert exc2.value.status == 400


def test_absolute_rel_400(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    abs_rel = str(tmp_path / "Elsewhere")
    with pytest.raises(FsBrowserError) as exc:
        resolve_sandbox(projects, root, abs_rel)
    assert exc.value.status == 400


def test_root_outside_projects_root_400(tmp_path: Path) -> None:
    projects = tmp_path / "Projects"
    projects.mkdir()
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    with pytest.raises(FsBrowserError) as exc:
        resolve_sandbox(projects, outside, "")
    assert exc.value.status == 400
    assert "projects" in exc.value.message.lower() or "escape" in exc.value.message.lower()


def test_missing_404(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    with pytest.raises(FsBrowserError) as exc:
        list_dir(projects, root, "no-such-dir")
    assert exc.value.status == 404
    assert "not found" in exc.value.message.lower()

    with pytest.raises(FsBrowserError) as exc2:
        read_text(projects, root, "missing.txt")
    assert exc2.value.status == 404


def test_list_on_file_400(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    (root / "only.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FsBrowserError) as exc:
        list_dir(projects, root, "only.txt")
    assert exc.value.status == 400
    assert "directory" in exc.value.message.lower()


def test_read_on_dir_400(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    (root / "subdir").mkdir()
    with pytest.raises(FsBrowserError) as exc:
        read_text(projects, root, "subdir")
    assert exc.value.status == 400
    assert "file" in exc.value.message.lower()


def test_binary_nul_415(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    (root / "blob.bin").write_bytes(b"abc\x00def")
    with pytest.raises(FsBrowserError) as exc:
        read_text(projects, root, "blob.bin")
    assert exc.value.status == 415
    assert "binary" in exc.value.message.lower()


def test_oversize_read_413(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    (root / "big.txt").write_bytes(b"x" * 100)
    with pytest.raises(FsBrowserError) as exc:
        read_text(projects, root, "big.txt", max_bytes=50)
    assert exc.value.status == 413
    assert "large" in exc.value.message.lower()


def test_oversize_write_413(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    content = "y" * 100
    with pytest.raises(FsBrowserError) as exc:
        write_text(projects, root, "big.txt", content, max_bytes=50)
    assert exc.value.status == 413
    assert "large" in exc.value.message.lower()
    assert not (root / "big.txt").exists()


def test_write_parent_missing_404(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    with pytest.raises(FsBrowserError) as exc:
        write_text(projects, root, "nope/child.txt", "x")
    assert exc.value.status == 404
    assert "parent" in exc.value.message.lower()


def test_write_none_content_as_empty(tmp_path: Path) -> None:
    projects, root = _projects_and_root(tmp_path)
    result = write_text(projects, root, "empty.txt", None)  # type: ignore[arg-type]
    assert (root / "empty.txt").read_text(encoding="utf-8") == ""
    assert result["size"] == 0


def test_resolve_sandbox_root_equal_projects(tmp_path: Path) -> None:
    projects = tmp_path / "Projects"
    projects.mkdir()
    got = resolve_sandbox(projects, projects, "")
    assert got == projects.resolve()
