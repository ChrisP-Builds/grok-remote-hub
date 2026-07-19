"""ANSI SGR parse/strip (Python) + JS render contract tests."""

from __future__ import annotations

from pathlib import Path

from hub.ui_format import parse_ansi_segments, strip_ansi

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

SAMPLE = "\x1b[31mERROR:\x1b[0m \x1b[1;34musage:\x1b[0m"


def test_parse_ansi_segments_basic() -> None:
    segs = parse_ansi_segments(SAMPLE)
    texts = [s["text"] for s in segs]
    assert "ERROR:" in texts
    assert "usage:" in texts
    assert " " in texts or any(" " in t for t in texts)

    error = next(s for s in segs if s["text"] == "ERROR:")
    assert error["fg"] == "red"
    assert error["bold"] is False

    usage = next(s for s in segs if s["text"] == "usage:")
    assert usage["fg"] == "blue"
    assert usage["bold"] is True

    plain = next(s for s in segs if s["text"] == " ")
    assert plain["fg"] is None
    assert plain["bold"] is False


def test_strip_ansi_removes_codes() -> None:
    assert strip_ansi(SAMPLE) == "ERROR: usage:"
    assert strip_ansi("plain") == "plain"
    assert strip_ansi("") == ""
    assert strip_ansi(None) == ""


def test_incomplete_trailing_esc_omitted() -> None:
    s = "ok\x1b[3"
    assert strip_ansi(s) == "ok"
    segs = parse_ansi_segments(s)
    assert "".join(x["text"] for x in segs) == "ok"
    assert all("\x1b" not in x["text"] for x in segs)
    assert all("[" not in x["text"] or x["text"] != "[" for x in segs)


def test_other_csi_and_osc_stripped() -> None:
    # ESC[K erase-in-line + OSC title
    s = "a\x1b[Kb\x1b]0;title\x07c"
    assert strip_ansi(s) == "abc"
    segs = parse_ansi_segments(s)
    assert "".join(x["text"] for x in segs) == "abc"


def test_reset_and_default_colors() -> None:
    s = "\x1b[31;1mR\x1b[22mN\x1b[39mD\x1b[0mX"
    segs = parse_ansi_segments(s)
    # R bold-red; N red normal; D+X plain (same style merges after 39 and 0)
    assert segs[0]["text"] == "R" and segs[0]["fg"] == "red" and segs[0]["bold"] is True
    assert segs[1]["text"] == "N" and segs[1]["fg"] == "red" and segs[1]["bold"] is False
    assert segs[2]["text"] == "DX" and segs[2]["fg"] is None and segs[2]["bold"] is False


def test_bg_and_bright() -> None:
    s = "\x1b[42;97mhi\x1b[0m"
    segs = parse_ansi_segments(s)
    assert len(segs) == 1
    assert segs[0]["text"] == "hi"
    assert segs[0]["bg"] == "green"
    assert segs[0]["fg"] == "bright-white"


def test_js_ansi_contracts() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function parseAnsiSegments" in js
    assert "function stripAnsi" in js
    assert "function renderAnsiInto" in js
    assert "ansi-bold" in js
    assert "ansi-fg-" in js
    assert "ansi-bg-" in js

    # onTerminalOut must render ANSI, not only textContent assignment of raw
    idx = js.find("function onTerminalOut")
    assert idx >= 0
    chunk = js[idx : idx + 2500]
    assert "renderAnsiInto(detail, raw)" in chunk
    assert "detail._rawText = raw" in chunk
    # Must not solely set textContent = raw without render path
    assert "detail.textContent = raw" not in chunk
    assert "stripAnsi(raw)" in chunk

    # setToolDetailBody uses render path
    set_idx = js.find("function setToolDetailBody")
    assert set_idx >= 0
    set_fn = js[set_idx : set_idx + 600]
    assert "renderAnsiInto" in set_fn
    assert "detail._rawText" in set_fn


def test_css_ansi_palette() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".ansi-fg-red" in css
    assert ".ansi-fg-blue" in css
    assert ".ansi-bold" in css
    assert ".ansi-dim" in css
    assert ".ansi-bg-green" in css
    # Readable on dark bg (not pure #000 black for black fg)
    assert "#6b7280" in css or "ansi-fg-black" in css
