"""Default topology: private agent serve; stock CLI independent (ADR-022)."""

from __future__ import annotations

from pathlib import Path

from hub.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]
ADR_022 = ROOT / "docs" / "adr" / "022-serve-default-cli-independent-topology.md"


def test_adr_022_file_exists() -> None:
    assert ADR_022.is_file()
    text = ADR_022.read_text(encoding="utf-8")
    assert "Accepted" in text
    assert "serve" in text.lower()


def test_config_default_agent_backend_is_serve() -> None:
    cfg = Config()
    assert cfg.agent_backend == "serve"
    assert cfg.uses_leader_backend is False


def test_load_config_omitted_backend_is_serve(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[agent]\nport = 2419\n", encoding="utf-8")
    loaded = load_config(p)
    assert loaded.agent_backend == "serve"
    assert loaded.uses_leader_backend is False


def test_load_config_invalid_backend_falls_back_to_serve(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[agent]\nbackend = "weird"\n', encoding="utf-8")
    loaded = load_config(p)
    assert loaded.agent_backend == "serve"
    assert loaded.uses_leader_backend is False


def test_uses_leader_backend_false_by_default() -> None:
    assert Config().uses_leader_backend is False
    assert Config(agent_backend="serve").uses_leader_backend is False


def test_adr_shortlist_decisions_named_in_adr_index() -> None:
    """AC: topology, stop, plan/resume, permissions each have a named ADR."""
    adr = ROOT / "docs" / "adr"
    readme = (adr / "README.md").read_text(encoding="utf-8")
    for num, slug, needle in (
        ("022", "022-serve-default-cli-independent-topology.md", "dual-live"),
        ("023", "023-permissions-always-approve-and-cli-resume-handoff.md", "resume"),
        ("024", "024-stop-cancels-turn-and-clears-bg-activity.md", "end_turn"),
        ("025", "025-plan-copy-session-search-and-recap.md", "recap"),
    ):
        path = adr / slug
        assert path.is_file(), f"missing ADR {num}"
        text = path.read_text(encoding="utf-8")
        assert "Accepted" in text
        assert needle.lower() in text.lower()
        assert num in readme
