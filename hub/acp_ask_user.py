"""ACP _x.ai/ask_user_question normalize + result builders (pure helpers).

Agent ``AskUserQuestionExtResponse`` is an internally tagged enum (tag ``outcome``)::

    {"outcome": "accepted", "answers": {qid: ["opt0"]}, "partial_answers": {}}
    {"outcome": "cancelled"}

Each map value in ``answers`` is ``StringOrVec``: a string or a list of strings.
Prefer always a list of strings for multi-select consistency.

Do not nest like permission replies (``{"outcome": {"outcome": "selected", ...}}``).
Do not wrap values as ``{"values": [...]}``; that fails StringOrVec deserialization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _opt_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_questions(params: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize ask_user_question params into stable UI-ready questions.

    Accepts camelCase and snake_case. Generates missing ids (q0, opt0).
    Strips empty options and empty questions.
    """
    raw = (params or {}).get("questions")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []

    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        qid = _opt_str(item.get("id")) or f"q{i}"
        text = _opt_str(item.get("text") or item.get("question"))
        if not text:
            continue
        multi = _as_bool(item.get("multiSelect", item.get("multi_select", False)))
        options_raw = item.get("options")
        options: list[dict[str, str]] = []
        if isinstance(options_raw, Sequence) and not isinstance(options_raw, (str, bytes)):
            for j, opt in enumerate(options_raw):
                if not isinstance(opt, Mapping):
                    continue
                label = _opt_str(opt.get("label") or opt.get("name") or opt.get("text"))
                if not label:
                    continue
                oid = _opt_str(opt.get("id")) or f"opt{j}"
                options.append(
                    {
                        "id": oid,
                        "label": label,
                        "description": _opt_str(opt.get("description")),
                        "preview": _opt_str(opt.get("preview")),
                    }
                )
        out.append(
            {
                "id": qid,
                "text": text,
                "multiSelect": multi,
                "options": options,
            }
        )
    return out


def build_accepted_result(answers: dict[str, list[str]] | Mapping[str, Any] | None) -> dict[str, Any]:
    """Build ACP accepted outcome (internally tagged enum).

    Shape::

        {
          "outcome": "accepted",
          "answers": {qid: ["opt0", ...]},
          "partial_answers": {},
        }

    Each answer value is ``StringOrVec`` (prefer always ``list[str]``).
    Input may still use legacy ``{"values": [...]}``; that is unwrapped.
    """
    normalized: dict[str, list[str]] = {}
    if isinstance(answers, Mapping):
        for qid, val in answers.items():
            key = str(qid)
            if isinstance(val, Mapping) and "values" in val:
                raw_vals = val.get("values")
                if isinstance(raw_vals, Sequence) and not isinstance(raw_vals, (str, bytes)):
                    values = [str(v) for v in raw_vals if str(v).strip()]
                elif raw_vals is None:
                    values = []
                else:
                    s = str(raw_vals).strip()
                    values = [s] if s else []
            elif isinstance(val, Sequence) and not isinstance(val, (str, bytes)):
                values = [str(v) for v in val if str(v).strip()]
            elif val is None:
                values = []
            else:
                s = str(val).strip()
                values = [s] if s else []
            normalized[key] = values
    return {
        "outcome": "accepted",
        "answers": normalized,
        "partial_answers": {},
    }


def build_cancelled_result() -> dict[str, Any]:
    """Build ACP cancelled outcome (internally tagged enum)."""
    return {"outcome": "cancelled"}
