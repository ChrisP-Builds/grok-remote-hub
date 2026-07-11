"""ACP session/request_permission option selection (pure helpers)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _option_id(opt: Mapping[str, Any]) -> str:
    return str(opt.get("optionId") or opt.get("option_id") or "")


def _option_kind(opt: Mapping[str, Any]) -> str:
    return str(opt.get("kind") or "").lower()


def _is_reject_or_cancel(opt: Mapping[str, Any]) -> bool:
    kind = _option_kind(opt)
    oid = _option_id(opt).lower()
    return (
        kind.startswith("reject")
        or kind.startswith("cancel")
        or "reject" in oid
        or "cancel" in oid
    )


def pick_permission_option(options: Sequence[Mapping[str, Any]] | None) -> str:
    """Pick the best auto-approve optionId from ACP permission options.

    Preference order:
    1. kind == allow_always, or optionId with always / proceed_always
    2. kind == allow_once, or optionId with proceed_once / allow-once
    3. first non-cancel/reject option
    4. first option / fallback allow-always
    """
    opts = [o for o in (options or []) if isinstance(o, Mapping)]
    if not opts:
        return "allow-always"

    # 1) allow_always / always / proceed_always (not reject_always)
    for opt in opts:
        if _is_reject_or_cancel(opt):
            continue
        kind = _option_kind(opt)
        oid = _option_id(opt).lower()
        if kind == "allow_always":
            return _option_id(opt)
        if "proceed_always" in oid or ("always" in oid and ("allow" in oid or "proceed" in oid)):
            return _option_id(opt)
        if "always" in oid:
            return _option_id(opt)

    # 2) allow_once / proceed_once
    for opt in opts:
        if _is_reject_or_cancel(opt):
            continue
        kind = _option_kind(opt)
        oid = _option_id(opt).lower()
        if kind == "allow_once":
            return _option_id(opt)
        if "proceed_once" in oid or oid in ("allow-once", "allow_once"):
            return _option_id(opt)
        if "once" in oid and ("allow" in oid or "proceed" in oid):
            return _option_id(opt)

    # 3) first non-cancel/reject
    for opt in opts:
        if not _is_reject_or_cancel(opt) and _option_id(opt):
            return _option_id(opt)

    # 4) last resort
    return _option_id(opts[0]) or "allow-always"
