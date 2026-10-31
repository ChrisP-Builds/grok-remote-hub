"""Tests for hub.config.load_config."""

from __future__ import annotations

from pathlib import Path

from hub.config import load_config


def test_load_config_tolerates_utf8_bom(tmp_path: Path) -> None:
    """PowerShell Set-Content -Encoding utf8 writes a BOM; tomllib must still parse."""
    body = (
        "[hub]\n"
        'hub_token = "bom-test-token"\n'
        "bind_port = 9999\n"
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    loaded = load_config(cfg_path)
    assert loaded.hub_token == "bom-test-token"
    assert loaded.bind_port == 9999


def test_load_config_without_bom(tmp_path: Path) -> None:
    body = (
        "[hub]\n"
        'hub_token = "plain-token"\n'
        "bind_port = 8788\n"
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(body, encoding="utf-8")

    loaded = load_config(cfg_path)
    assert loaded.hub_token == "plain-token"
    assert loaded.bind_port == 8788
