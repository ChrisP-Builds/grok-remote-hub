"""Terminal-print formatting helpers shared by UI tests (and mirrored in static/app.js)."""

from __future__ import annotations

import re
from typing import Any


def format_term_prefix(role: str) -> str:
    """Role prefix for a terminal transcript line."""
    r = (role or "").strip().lower()
    if r == "user":
        return "You:"
    if r == "assistant":
        return "Grok:"
    # thought / tool / plan / system / activity — thought status is in the summary label
    return "·"


def format_tool_line(
    title: str | None,
    status: str | None = None,
    summary: str | None = None,
) -> str:
    """Compact one-line tool status, CLI style: `Label [status] summary`."""
    label = (title or "tool").strip() or "tool"
    parts: list[str] = [label]
    st = (status or "").strip()
    if st:
        parts.append(f"[{st}]")
    snip = (summary or "").strip()
    if snip and snip not in label:
        parts.append(snip)
    return " ".join(parts)


def should_show_tool_line() -> bool:
    """Tools are always visible in the terminal stream (not activity-only)."""
    return True


# More permissive than strict GFM: 1+ dashes per cell (agent tables often use 1–2).
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")
_TABLE_SEP_LOOSE_RE = re.compile(r"^\|?:?-{1,}:?(\|:?-{1,}:?)+\|?$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")


def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_separator(line: str) -> bool:
    """True if line is a GFM-style table separator (1+ dashes per cell)."""
    if _TABLE_SEP_RE.match(line):
        return True
    loose = line.strip().replace(" ", "")
    return bool(_TABLE_SEP_LOOSE_RE.match(loose))


def parse_simple_markdown_table(text: str) -> list[list[str]] | None:
    """
    Parse a simple GitHub-style markdown table block from text.

    Separator cells accept 1+ dashes (more permissive than strict GFM, which
    wants 3+) so agent-generated tables with short seps still render.

    Returns rows as list[list[str]] (header + body, separator omitted),
    or None if no table is found.
    """
    if not text or "|" not in text:
        return None

    lines = text.splitlines()
    # Find a header row followed by a separator
    for i in range(len(lines) - 1):
        header = lines[i]
        sep = lines[i + 1]
        if not _TABLE_ROW_RE.match(header) and "|" not in header:
            continue
        # Accept rows that look like pipe tables even without leading |
        if header.count("|") < 1:
            continue
        if not _is_table_separator(sep):
            continue

        cells_header = _split_table_row(header)
        if len(cells_header) < 2:
            continue

        rows: list[list[str]] = [cells_header]
        j = i + 2
        while j < len(lines):
            row = lines[j]
            if not row.strip():
                break
            if row.count("|") < 1:
                break
            # Stop if next block is not table-like
            if not (_TABLE_ROW_RE.match(row) or "|" in row):
                break
            cells = _split_table_row(row)
            # Pad/truncate to header width
            if len(cells) < len(cells_header):
                cells = cells + [""] * (len(cells_header) - len(cells))
            elif len(cells) > len(cells_header):
                cells = cells[: len(cells_header)]
            rows.append(cells)
            j += 1

        if len(rows) >= 1:
            return rows

    return None


def format_plan_summary(entries: list[dict[str, Any]] | None) -> str:
    """CLI-style plan line body without the leading · : `plan {done}/{n}`."""
    list_ = list(entries or [])
    n = len(list_)
    if n == 0:
        return "plan (empty)"
    done = 0
    for e in list_:
        st = str((e or {}).get("status") or "").strip().lower()
        if st in ("completed", "complete", "ok", "success", "succeeded", "done"):
            done += 1
    return f"plan {done}/{n}"
