"""Terminal-print formatting helpers shared by UI tests (and mirrored in static/app.js)."""

from __future__ import annotations

import re
from typing import Any

# CSI SGR color names (mirrored in static/app.js)
_ANSI_FG: dict[int, str] = {
    30: "black",
    31: "red",
    32: "green",
    33: "yellow",
    34: "blue",
    35: "magenta",
    36: "cyan",
    37: "white",
    90: "bright-black",
    91: "bright-red",
    92: "bright-green",
    93: "bright-yellow",
    94: "bright-blue",
    95: "bright-magenta",
    96: "bright-cyan",
    97: "bright-white",
}
_ANSI_BG: dict[int, str] = {
    40: "black",
    41: "red",
    42: "green",
    43: "yellow",
    44: "blue",
    45: "magenta",
    46: "cyan",
    47: "white",
    100: "bright-black",
    101: "bright-red",
    102: "bright-green",
    103: "bright-yellow",
    104: "bright-blue",
    105: "bright-magenta",
    106: "bright-cyan",
    107: "bright-white",
}

def _is_esc_at(s: str, i: int) -> tuple[bool, int]:
    """Return (matched, consume_len) if an ESC introducer starts at i."""
    if i >= len(s):
        return False, 0
    c = s[i]
    if c == "\x1b":
        return True, 1
    if c == "\x9b":  # single-byte CSI
        return True, 1
    # Rare literal forms: \x1b, \033, \e (backslash + digits/letter)
    if c == "\\" and i + 1 < len(s):
        rest = s[i + 1 :]
        if rest.startswith("x1b") or rest.startswith("x1B"):
            return True, 4
        if rest.startswith("033"):
            return True, 4
        if rest.startswith("e") or rest.startswith("E"):
            return True, 2
    return False, 0


def _apply_sgr(params: list[int], style: dict[str, Any]) -> None:
    """Mutate style dict from SGR parameter list."""
    if not params:
        params = [0]
    i = 0
    while i < len(params):
        p = params[i]
        if p == 0:
            style["fg"] = None
            style["bg"] = None
            style["bold"] = False
            style["dim"] = False
        elif p == 1:
            style["bold"] = True
            style["dim"] = False
        elif p == 2:
            style["dim"] = True
            style["bold"] = False
        elif p == 22:
            style["bold"] = False
            style["dim"] = False
        elif p == 39:
            style["fg"] = None
        elif p == 49:
            style["bg"] = None
        elif p in _ANSI_FG:
            style["fg"] = _ANSI_FG[p]
        elif p in _ANSI_BG:
            style["bg"] = _ANSI_BG[p]
        # 38/48 extended colors: skip sequence (ignore, do not show garbage)
        elif p in (38, 48):
            # 38;5;n or 38;2;r;g;b
            if i + 1 < len(params):
                mode = params[i + 1]
                if mode == 5 and i + 2 < len(params):
                    i += 2
                elif mode == 2 and i + 4 < len(params):
                    i += 4
                else:
                    i += 1
            # else incomplete extended — ignore remainder
        i += 1


def _flush_segment(
    buf: list[str],
    style: dict[str, Any],
    out: list[dict[str, Any]],
) -> None:
    if not buf:
        return
    text = "".join(buf)
    buf.clear()
    if not text:
        return
    seg = {
        "text": text,
        "fg": style["fg"],
        "bg": style["bg"],
        "bold": bool(style["bold"]),
        "dim": bool(style["dim"]),
    }
    if out and (
        out[-1]["fg"] == seg["fg"]
        and out[-1]["bg"] == seg["bg"]
        and out[-1]["bold"] == seg["bold"]
        and out[-1]["dim"] == seg["dim"]
    ):
        out[-1]["text"] += text
    else:
        out.append(seg)


def parse_ansi_segments(text: str | None) -> list[dict[str, Any]]:
    """
    Parse text with ANSI/SGR into styled segments.

    Each segment: {"text": str, "fg": str|None, "bg": str|None, "bold": bool, "dim": bool}

    Handles CSI SGR ``ESC[...m`` (0 reset, 1 bold, 2 dim, 22 normal intensity,
    39/49 default fg/bg, 30–37/90–97 fg, 40–47/100–107 bg). Other CSI and OSC
    sequences are stripped. Incomplete trailing escape at end of string is omitted.
    """
    s = "" if text is None else str(text)
    if not s:
        return []

    style: dict[str, Any] = {"fg": None, "bg": None, "bold": False, "dim": False}
    out: list[dict[str, Any]] = []
    buf: list[str] = []
    i = 0
    n = len(s)

    while i < n:
        is_esc, esc_len = _is_esc_at(s, i)
        # Single-byte CSI (\x9b) is already a CSI introducer (no '[')
        if is_esc and esc_len == 1 and s[i] == "\x9b":
            _flush_segment(buf, style, out)
            j = i + 1
            while j < n and not ("\x40" <= s[j] <= "\x7e"):
                j += 1
            if j >= n:
                break
            final = s[j]
            body = s[i + 1 : j]
            if final == "m":
                _apply_sgr(_parse_sgr_params(body), style)
            i = j + 1
            continue

        if is_esc:
            _flush_segment(buf, style, out)
            after = i + esc_len
            if after >= n:
                break
            kind = s[after]

            # OSC: ESC ] ... BEL or ESC ] ... ST (ESC \)
            if kind == "]":
                j = after + 1
                while j < n:
                    if s[j] == "\x07":
                        j += 1
                        break
                    if s[j] == "\x1b" and j + 1 < n and s[j + 1] == "\\":
                        j += 2
                        break
                    j += 1
                else:
                    break
                i = j
                continue

            # CSI: ESC [
            if kind == "[":
                j = after + 1
                while j < n and not ("\x40" <= s[j] <= "\x7e"):
                    j += 1
                if j >= n:
                    break
                final = s[j]
                body = s[after + 1 : j]
                if final == "m":
                    _apply_sgr(_parse_sgr_params(body), style)
                # else: other CSI (e.g. ESC[K) — strip
                i = j + 1
                continue

            # Other 2-byte ESC sequences: skip introducer + next byte
            i = after + 1 if after < n else after
            continue

        buf.append(s[i])
        i += 1

    _flush_segment(buf, style, out)
    return out


def _parse_sgr_params(body: str) -> list[int]:
    """Parse CSI parameter string for SGR (digits and semicolons)."""
    if not body or not body.strip():
        return [0]
    params: list[int] = []
    for part in body.split(";"):
        part = part.strip()
        if part == "":
            params.append(0)
            continue
        # Ignore private-mode / intermediate chars; take leading digits only
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            try:
                params.append(int(digits))
            except ValueError:
                params.append(0)
        else:
            params.append(0)
    return params if params else [0]


def strip_ansi(text: str | None) -> str:
    """Remove ANSI/CSI/OSC sequences; return plain text (for one-liners / strip)."""
    segs = parse_ansi_segments(text)
    return "".join(seg["text"] for seg in segs)


def format_term_prefix(role: str) -> str:
    """Role prefix for a terminal transcript line."""
    r = (role or "").strip().lower()
    # Trailing space so "You: /compact" stays readable next to body text.
    if r == "user":
        return "You: "
    if r == "assistant":
        return "Grok: "
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


def find_simple_markdown_tables(text: str) -> list[dict]:
    """
    Return all GFM-ish tables in order:
      {"start": line_idx, "end": line_idx_exclusive, "rows": list[list[str]]}

    Separator cells accept 1+ dashes (more permissive than strict GFM, which
    wants 3+) so agent-generated tables with short seps still render.
    """
    if not text or "|" not in text:
        return []

    lines = text.splitlines()
    tables: list[dict] = []
    i = 0
    while i < len(lines) - 1:
        header = lines[i]
        sep = lines[i + 1]
        if not _TABLE_ROW_RE.match(header) and "|" not in header:
            i += 1
            continue
        # Accept rows that look like pipe tables even without leading |
        if header.count("|") < 1:
            i += 1
            continue
        if not _is_table_separator(sep):
            i += 1
            continue

        cells_header = _split_table_row(header)
        if len(cells_header) < 2:
            i += 1
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
            tables.append({"start": i, "end": j, "rows": rows})
            i = j  # continue after this table (do not only return first)
        else:
            i += 1

    return tables


def split_text_with_markdown_tables(text: str) -> list[tuple[str, object]]:
    """
    Segment full text into ordered parts:
      ("text", str) | ("table", list[list[str]])

    Plain text preserves intervening newlines between tables.
    """
    s = "" if text is None else str(text)
    tables = find_simple_markdown_tables(s)
    if not tables:
        return [("text", s)]

    lines = s.splitlines()
    parts: list[tuple[str, object]] = []
    cursor = 0
    for t in tables:
        start = int(t["start"])
        end = int(t["end"])
        rows = t["rows"]
        if start > cursor:
            parts.append(("text", "\n".join(lines[cursor:start])))
        parts.append(("table", rows))
        cursor = end
    if cursor < len(lines):
        parts.append(("text", "\n".join(lines[cursor:])))
    return parts if parts else [("text", s)]


def parse_simple_markdown_table(text: str) -> list[list[str]] | None:
    """
    Parse the first simple GitHub-style markdown table block from text.

    Back-compat wrapper over find_simple_markdown_tables (first match only).

    Returns rows as list[list[str]] (header + body, separator omitted),
    or None if no table is found.
    """
    found = find_simple_markdown_tables(text)
    if not found:
        return None
    return found[0]["rows"]


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


def merge_stream_text(prev: str | None, chunk: str | None) -> str:
    """Merge live stream chunks (mirror of static/app.js mergeStreamText).

    Cumulative snapshots replace; pure deltas append; redundant suffix/overlap
    is absorbed so mixed deliveries do not double-print words.
    """
    p = "" if prev is None else str(prev)
    c = "" if chunk is None else str(chunk)
    if not c:
        return p
    if not p:
        return c
    if p == c or p.startswith(c):
        return p
    if c.startswith(p):
        return c
    if p.endswith(c):
        return p
    c_trim = c.lstrip()
    if c_trim and p.endswith(c_trim):
        return p
    if c_trim and p.rstrip() == c_trim:
        return p
    max_o = min(256, len(p), len(c))
    for o in range(max_o, 0, -1):
        if p[-o:] == c[:o]:
            return p + c[o:]
    return p + c
