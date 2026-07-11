"""Discover local agent skills (SKILL.md) for the slash command palette."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Max path parts under a skill root for SKILL.md (e.g. hyperframes/skills/gsap/SKILL.md = 4).
_MAX_SKILL_DEPTH = 4
_DESC_MAX = 160

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def default_skill_roots(home: Path | None = None) -> list[tuple[str, Path]]:
    """Return (source_label, path) for known skill roots that exist."""
    base = Path(home) if home is not None else Path.home()
    candidates = [
        ("grok", base / ".grok" / "skills"),
        ("claude", base / ".claude" / "skills"),
        ("bundled", base / ".grok" / "bundled" / "skills"),
    ]
    return [(src, p) for src, p in candidates if p.is_dir()]


def parse_skill_frontmatter(text: str) -> tuple[str | None, str]:
    """Parse name and description from YAML-lite SKILL.md frontmatter."""
    raw = text or ""
    # Strip BOM if present
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None, ""

    fm = m.group(1)
    name: str | None = None
    desc_parts: list[str] = []
    in_desc = False
    desc_block = False  # True when description uses > or |

    for line in fm.splitlines():
        if in_desc:
            if desc_block and (line.startswith((" ", "\t")) or line.strip() == ""):
                part = line.strip()
                if part:
                    desc_parts.append(part)
                continue
            in_desc = False
            desc_block = False
            # Fall through: this line may be the next key

        km = _KEY_RE.match(line)
        if not km:
            continue
        key, val = km.group(1), km.group(2)
        if key == "name":
            name = val.strip().strip("\"'") or None
            in_desc = False
            desc_block = False
        elif key == "description":
            val = val.strip()
            if val in (">", "|", ">-", "|-", ">+", "|+"):
                desc_parts = []
                in_desc = True
                desc_block = True
            elif val:
                desc_parts = [val.strip("\"'")]
                in_desc = False
                desc_block = False
            else:
                desc_parts = []
                in_desc = True
                desc_block = True
        else:
            in_desc = False
            desc_block = False

    description = " ".join(desc_parts).strip()
    return name, description


def _iter_skill_files(root: Path, max_depth: int = _MAX_SKILL_DEPTH):
    root = root.resolve()
    if not root.is_dir():
        return
    for path in root.rglob("SKILL.md"):
        if not path.is_file():
            continue
        parts_lower = {p.lower() for p in path.parts}
        if "node_modules" in parts_lower:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > max_depth:
            continue
        yield path


def _truncate_desc(desc: str, limit: int = _DESC_MAX) -> str:
    text = (desc or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def list_skills(
    *,
    home: Path | None = None,
    roots: list[tuple[str, Path]] | None = None,
) -> list[dict[str, Any]]:
    """Scan skill roots and return palette items.

    Each item: ``{"name": str, "description": str, "source": str}``.
    Duplicate names keep the first occurrence (scan order: grok, claude, bundled).
    """
    scan_roots = roots if roots is not None else default_skill_roots(home)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []

    for source, root in scan_roots:
        try:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            files = sorted(_iter_skill_files(root_path), key=lambda p: str(p).lower())
        except OSError:
            continue

        for skill_md in files:
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, description = parse_skill_frontmatter(text)
            if not name:
                name = skill_md.parent.name
            name = str(name).strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "name": name,
                    "description": _truncate_desc(description),
                    "source": source,
                }
            )

    items.sort(key=lambda x: (x["name"] or "").lower())
    return items
