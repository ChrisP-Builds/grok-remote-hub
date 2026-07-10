from __future__ import annotations

import json
from pathlib import Path

from hub.session_index import list_projects, scan_sessions


def _write_summary(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_scan_sessions_filters_and_sorts(tmp_path: Path) -> None:
    root = tmp_path / "sessions"

    # Valid project session with title
    s1 = root / "D%3A%5CProjects%5CAlpha" / "019f493c-af12-7652-a6d8-bf645c10921c"
    _write_summary(
        s1 / "summary.json",
        {
            "info": {"id": "019f493c-af12-7652-a6d8-bf645c10921c", "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Alpha work",
            "session_summary": "Alpha work",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 3,
            "current_model_id": "grok-4.5",
        },
    )
    (s1 / "updates.jsonl").write_text("{}\n", encoding="utf-8")

    # Older valid session
    s2 = root / "D%3A%5CProjects%5CBeta" / "019f06ee-305f-7903-b295-d706c08f0b4f"
    _write_summary(
        s2 / "summary.json",
        {
            "info": {"id": "019f06ee-305f-7903-b295-d706c08f0b4f", "cwd": r"D:\Projects\Beta"},
            "session_summary": "Beta chat",
            "updated_at": "2026-07-08T12:00:00Z",
            "num_chat_messages": 1,
            "current_model_id": "grok-build",
        },
    )

    # oracle-grok junk (should be excluded)
    junk = (
        root
        / "%5C%5C%3F%5CC%3A%5CUsers%5Cme%5CAppData%5CLocal%5CTemp%5Coracle-grok-abc"
        / "019f0abc-1111-2222-3333-444444444444"
    )
    _write_summary(
        junk / "summary.json",
        {
            "info": {
                "id": "019f0abc-1111-2222-3333-444444444444",
                "cwd": r"C:\Users\me\AppData\Local\Temp\oracle-grok-abc",
            },
            "generated_title": "Should hide",
            "updated_at": "2026-07-10T12:00:00Z",
            "num_chat_messages": 5,
        },
    )

    # Non-UUID folder
    bad = root / "D%3A%5CProjects%5CAlpha" / "not-a-uuid"
    _write_summary(
        bad / "summary.json",
        {
            "info": {"id": "not-a-uuid", "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Bad id",
            "updated_at": "2026-07-11T12:00:00Z",
            "num_chat_messages": 2,
        },
    )

    # Empty / useless summary
    empty = root / "D%3A%5CProjects%5CGamma" / "019f1111-1111-1111-1111-111111111111"
    _write_summary(
        empty / "summary.json",
        {
            "info": {"id": "019f1111-1111-1111-1111-111111111111", "cwd": r"D:\Projects\Gamma"},
            "session_summary": "",
            "updated_at": "2026-07-07T12:00:00Z",
            "num_chat_messages": 0,
            "num_messages": 0,
        },
    )

    results = scan_sessions(root, limit=80)
    ids = [r.sessionId for r in results]
    assert "019f493c-af12-7652-a6d8-bf645c10921c" in ids
    assert "019f06ee-305f-7903-b295-d706c08f0b4f" in ids
    assert "019f0abc-1111-2222-3333-444444444444" not in ids
    assert "not-a-uuid" not in ids
    assert "019f1111-1111-1111-1111-111111111111" not in ids
    assert results[0].sessionId == "019f493c-af12-7652-a6d8-bf645c10921c"
    assert results[0].title == "Alpha work"
    assert results[0].cwd == r"D:\Projects\Alpha"
    assert results[0].modelId == "grok-4.5"


def test_scan_sessions_respects_limit(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    for i in range(5):
        sid = f"019f0000-0000-0000-0000-00000000000{i}"
        d = root / "proj" / sid
        _write_summary(
            d / "summary.json",
            {
                "info": {"id": sid, "cwd": r"D:\Projects\P"},
                "generated_title": f"S{i}",
                "updated_at": f"2026-07-0{i+1}T12:00:00Z",
                "num_chat_messages": 1,
            },
        )
    results = scan_sessions(root, limit=3)
    assert len(results) == 3


def test_list_projects_merges_dirs_and_cwds(tmp_path: Path) -> None:
    projects = tmp_path / "Projects"
    (projects / "AppOne").mkdir(parents=True)
    (projects / "AppTwo").mkdir(parents=True)

    from hub.session_index import SessionInfo

    sessions = [
        SessionInfo(
            sessionId="a",
            title="t",
            cwd=str(tmp_path / "Other" / "Work"),
            updatedAt="2026-07-01T00:00:00Z",
            modelId="",
            path="",
        )
    ]
    items = list_projects(projects, sessions)
    names = {i["name"] for i in items}
    assert "AppOne" in names
    assert "AppTwo" in names
    assert "Work" in names
