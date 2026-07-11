"""Unit tests for ACP permission option picking and fs helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub.acp_fs import read_text_file, write_text_file
from hub.acp_permissions import pick_permission_option


def test_prefer_allow_always_kind() -> None:
    opts = [
        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
    ]
    assert pick_permission_option(opts) == "allow-always"


def test_jetbrains_proceed_always_tool() -> None:
    opts = [
        {"optionId": "proceed_once", "name": "Proceed once", "kind": "allow_once"},
        {
            "optionId": "proceed_always_tool",
            "name": "Always allow this tool",
            "kind": "allow_always",
        },
        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
    ]
    assert pick_permission_option(opts) == "proceed_always_tool"


def test_proceed_always_by_option_id_without_kind() -> None:
    opts = [
        {"optionId": "proceed_once", "name": "Once"},
        {"optionId": "proceed_always", "name": "Always"},
    ]
    assert pick_permission_option(opts) == "proceed_always"


def test_prefer_allow_once_when_no_always() -> None:
    opts = [
        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    ]
    assert pick_permission_option(opts) == "allow-once"


def test_proceed_once_when_only_once() -> None:
    opts = [
        {"optionId": "proceed_once", "name": "Proceed", "kind": "allow_once"},
        {"optionId": "reject_once", "name": "No", "kind": "reject_once"},
    ]
    assert pick_permission_option(opts) == "proceed_once"


def test_skip_reject_always() -> None:
    opts = [
        {"optionId": "reject_always", "name": "Never", "kind": "reject_always"},
        {"optionId": "allow-once", "name": "Once", "kind": "allow_once"},
    ]
    assert pick_permission_option(opts) == "allow-once"


def test_empty_options_fallback() -> None:
    assert pick_permission_option([]) == "allow-always"
    assert pick_permission_option(None) == "allow-always"


def test_first_non_cancel() -> None:
    opts = [
        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
        {"optionId": "custom_ok", "name": "OK", "kind": "other"},
    ]
    assert pick_permission_option(opts) == "custom_ok"


def test_docs_shape_allow_once_only() -> None:
    """Official ACP docs example shape."""
    opts = [
        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
    ]
    assert pick_permission_option(opts) == "allow-once"


def test_read_write_text_file(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "hello.txt"
    write_text_file({"path": str(p), "content": "line1\nline2\nline3\n"})
    assert p.read_text(encoding="utf-8") == "line1\nline2\nline3\n"
    full = read_text_file({"path": str(p)})
    assert "line1" in full["content"]
    partial = read_text_file({"path": str(p), "line": 2, "limit": 1})
    assert partial["content"].startswith("line2")


def test_read_rejects_relative() -> None:
    with pytest.raises(ValueError, match="absolute"):
        read_text_file({"path": "relative/path.txt"})


def test_read_rejects_empty() -> None:
    with pytest.raises(ValueError, match="required"):
        read_text_file({"path": ""})
