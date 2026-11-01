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


def test_load_config_allowed_hosts_and_origins(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[hub]\n"
        'allowed_hosts = ["hub.example", "desk"]\n'
        'allowed_origins = ["https://desk.tailnet.ts.net"]\n',
        encoding="utf-8",
    )
    loaded = load_config(cfg_path)
    assert loaded.allowed_hosts == ["hub.example", "desk"]
    assert loaded.allowed_origins == ["https://desk.tailnet.ts.net"]


def test_load_config_allowed_lists_default_empty(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[hub]\nbind_port = 8787\n", encoding="utf-8")
    loaded = load_config(cfg_path)
    assert loaded.allowed_hosts == []
    assert loaded.allowed_origins == []


def test_load_config_approval_mode(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[hub]\napproval_mode = "Strict"\n', encoding="utf-8")
    assert load_config(cfg_path).approval_mode == "strict"
    cfg_path.write_text('[hub]\napproval_mode = "yolo"\n', encoding="utf-8")
    assert load_config(cfg_path).approval_mode == "balanced"
    cfg_path.write_text("[hub]\n", encoding="utf-8")
    loaded = load_config(cfg_path)
    assert loaded.approval_mode == "balanced"
    assert loaded.gate_plugin_dir.name == "grok-approval-gate"

def test_load_config_allowed_tailscale_logins(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[hub]\nallowed_tailscale_logins = ["Partner@Example.com"]\n', encoding="utf-8")
    assert load_config(cfg_path).allowed_tailscale_logins == ["Partner@Example.com"]
    cfg_path.write_text("[hub]\n", encoding="utf-8")
    assert load_config(cfg_path).allowed_tailscale_logins == []