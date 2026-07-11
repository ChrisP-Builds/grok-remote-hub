"""ACP client fs/read_text_file and fs/write_text_file helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _require_absolute_path(path: Any) -> Path:
    if path is None or (isinstance(path, str) and not path.strip()):
        raise ValueError("path is required")
    p = Path(str(path))
    if not p.is_absolute():
        raise ValueError(f"path must be absolute: {path!r}")
    return p


def read_text_file(params: dict[str, Any]) -> dict[str, str]:
    """Read a text file; optional 1-based line + limit (line count)."""
    path = _require_absolute_path(params.get("path"))
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")

    # Cap very large reads at ~5MB for safety
    max_bytes = 5_000_000
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")

    line = params.get("line")
    limit = params.get("limit")
    if line is not None or limit is not None:
        lines = text.splitlines(keepends=True)
        start = 0
        if line is not None:
            try:
                start_line = int(line)
            except (TypeError, ValueError) as exc:
                raise ValueError("line must be an integer") from exc
            # 1-based; treat 0 as start of file
            if start_line > 0:
                start = start_line - 1
        end = len(lines)
        if limit is not None:
            try:
                lim = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError("limit must be an integer") from exc
            if lim < 0:
                raise ValueError("limit must be >= 0")
            end = min(len(lines), start + lim)
        text = "".join(lines[start:end])

    return {"content": text}


def write_text_file(params: dict[str, Any]) -> dict[str, Any]:
    path = _require_absolute_path(params.get("path"))
    if "content" not in params:
        raise ValueError("content is required")
    content = params.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write
    tmp = path.with_suffix(path.suffix + ".hubtmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        # Fallback direct write
        path.write_text(content, encoding="utf-8", newline="\n")
    return {}
