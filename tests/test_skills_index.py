"""Unit tests for skill discovery and frontmatter parsing."""

from __future__ import annotations

from pathlib import Path

from hub.skills_index import list_skills, parse_skill_frontmatter


def _write_skill(dir_path: Path, name: str, description: str, body: str = "# skill\n") -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )


def test_parse_single_line_frontmatter() -> None:
    text = "---\nname: handoff\ndescription: End-of-session wrap-up.\n---\n\n# handoff\n"
    name, desc = parse_skill_frontmatter(text)
    assert name == "handoff"
    assert desc == "End-of-session wrap-up."


def test_parse_folded_description() -> None:
    text = (
        "---\n"
        "name: check-work\n"
        "description: >\n"
        "  Check your work with a verifier.\n"
        "  Use when asked to verify.\n"
        "metadata:\n"
        "  short-description: verify\n"
        "---\n\n# body\n"
    )
    name, desc = parse_skill_frontmatter(text)
    assert name == "check-work"
    assert "Check your work" in desc
    assert "verify" in desc.lower()
    assert "short-description" not in desc


def test_parse_doc_sync_mentions_handoff() -> None:
    """Regression fixture: desc contains 'handoff skill' (client ranks name-first)."""
    text = (
        "---\n"
        "name: doc-sync\n"
        "description: Update living docs. Use as a step inside the handoff skill.\n"
        "---\n\n# doc-sync\n"
    )
    name, desc = parse_skill_frontmatter(text)
    assert name == "doc-sync"
    assert "handoff skill" in desc


def test_list_skills_finds_top_level_and_nested(tmp_path: Path) -> None:
    grok = tmp_path / ".grok" / "skills"
    claude = tmp_path / ".claude" / "skills"
    _write_skill(grok / "check-work", "check-work", "Verify changes")
    _write_skill(claude / "handoff", "handoff", "Session wrap-up")
    _write_skill(claude / "doc-sync", "doc-sync", "Living docs via handoff skill")
    # Nested hyperframes-style path (depth 4)
    nested = grok / "hyperframes" / "skills" / "gsap"
    _write_skill(nested, "gsap", "GSAP animations")
    # Too deep (5 parts under root) — skipped
    deep = grok / "a" / "b" / "c" / "d"
    _write_skill(deep, "too-deep", "Should be skipped")
    # node_modules skipped
    nm = grok / "pkg" / "node_modules" / "x"
    _write_skill(nm, "evil", "skip me")

    items = list_skills(home=tmp_path)
    names = {i["name"] for i in items}
    assert "check-work" in names
    assert "handoff" in names
    assert "doc-sync" in names
    assert "gsap" in names
    assert "too-deep" not in names
    assert "evil" not in names

    by_name = {i["name"]: i for i in items}
    assert by_name["handoff"]["source"] == "claude"
    assert by_name["check-work"]["source"] == "grok"
    assert "handoff skill" in by_name["doc-sync"]["description"]


def test_list_skills_dedupes_prefer_first_root(tmp_path: Path) -> None:
    grok = tmp_path / ".grok" / "skills"
    claude = tmp_path / ".claude" / "skills"
    _write_skill(grok / "help", "help", "Grok help")
    _write_skill(claude / "help", "help", "Claude help")
    items = list_skills(home=tmp_path)
    help_items = [i for i in items if i["name"] == "help"]
    assert len(help_items) == 1
    assert help_items[0]["source"] == "grok"
    assert help_items[0]["description"] == "Grok help"


def test_list_skills_explicit_roots(tmp_path: Path) -> None:
    only = tmp_path / "custom"
    _write_skill(only / "foo", "foo", "Custom skill")
    items = list_skills(roots=[("custom", only)])
    assert len(items) == 1
    assert items[0] == {
        "name": "foo",
        "description": "Custom skill",
        "source": "custom",
    }


def test_list_skills_fallback_folder_name(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    d = root / "my-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: No name key\n---\n\n# x\n", encoding="utf-8")
    items = list_skills(roots=[("t", root)])
    assert len(items) == 1
    assert items[0]["name"] == "my-skill"
