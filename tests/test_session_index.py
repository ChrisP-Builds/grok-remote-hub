from __future__ import annotations

import json
from pathlib import Path

from hub.session_index import (
    SessionInfo,
    delete_session,
    is_noise_session,
    list_projects,
    rename_session,
    scan_sessions,
    stamp_hub_origin,
)


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


def test_scan_sessions_detects_subagents(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    parent_id = "019f493c-af12-7652-a6d8-bf645c10921c"
    child_id = "019f06ee-305f-7903-b295-d706c08f0b4f"
    cwd_key = "D%3A%5CProjects%5CAlpha"

    parent = root / cwd_key / parent_id
    _write_summary(
        parent / "summary.json",
        {
            "info": {"id": parent_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Parent session",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 2,
            "current_model_id": "grok-4.5",
        },
    )

    child = parent / "subagents" / child_id
    _write_summary(
        child / "summary.json",
        {
            "info": {"id": child_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Child agent work",
            "agent_name": "general-purpose",
            "updated_at": "2026-07-09T13:00:00Z",
            "num_chat_messages": 1,
            "current_model_id": "grok-build",
        },
    )

    results = scan_sessions(root, limit=80)
    by_id = {r.sessionId: r for r in results}
    assert parent_id in by_id
    assert child_id in by_id

    parent_info = by_id[parent_id]
    assert parent_info.isSubagent is False
    assert parent_info.isWorking is True
    assert parent_info.isNoise is False
    assert parent_info.parentSessionId == ""
    assert parent_info.agentName == ""

    child_info = by_id[child_id]
    assert child_info.isSubagent is True
    assert child_info.isWorking is False
    assert child_info.parentSessionId == parent_id
    assert child_info.agentName == "general-purpose"


def test_scan_sessions_detects_session_kind_subagent(tmp_path: Path) -> None:
    """Real Grok layout: subagents are sibling UUID folders with session_kind set."""
    root = tmp_path / "sessions"
    cwd_key = "D%3A%5CProjects%5CAlpha"
    parent_id = "019f493c-af12-7652-a6d8-bf645c10921c"
    sub_id = "019f06ee-305f-7903-b295-d706c08f0b4f"
    fork_id = "019f1111-2222-3333-4444-555555555555"
    main_agent_id = "019f2222-3333-4444-5555-666666666666"

    parent = root / cwd_key / parent_id
    _write_summary(
        parent / "summary.json",
        {
            "info": {"id": parent_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Main session",
            "agent_name": "grok-build-plan",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 2,
            "current_model_id": "grok-4.5",
        },
    )

    sub = root / cwd_key / sub_id
    _write_summary(
        sub / "summary.json",
        {
            "info": {"id": sub_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Sibling subagent",
            "session_kind": "subagent",
            "agent_name": "general-purpose",
            "parent_session_id": parent_id,
            "updated_at": "2026-07-09T13:00:00Z",
            "num_chat_messages": 1,
            "current_model_id": "grok-build",
        },
    )

    fork = root / cwd_key / fork_id
    _write_summary(
        fork / "summary.json",
        {
            "info": {"id": fork_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Forked subagent",
            "session_kind": "subagent_fork",
            "agent_name": "explore",
            "updated_at": "2026-07-09T14:00:00Z",
            "num_chat_messages": 1,
            "current_model_id": "grok-build",
        },
    )

    # agent_name alone must not mark as subagent
    mainish = root / cwd_key / main_agent_id
    _write_summary(
        mainish / "summary.json",
        {
            "info": {"id": main_agent_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Plan main",
            "agent_name": "grok-build-plan",
            "updated_at": "2026-07-09T11:00:00Z",
            "num_chat_messages": 1,
            "current_model_id": "grok-4.5",
        },
    )

    results = scan_sessions(root, limit=80)
    by_id = {r.sessionId: r for r in results}

    assert by_id[parent_id].isSubagent is False
    assert by_id[parent_id].isWorking is True
    assert by_id[parent_id].parentSessionId == ""

    assert by_id[sub_id].isSubagent is True
    assert by_id[sub_id].isWorking is False
    assert by_id[sub_id].parentSessionId == parent_id
    assert by_id[sub_id].agentName == "general-purpose"

    assert by_id[fork_id].isSubagent is True
    assert by_id[fork_id].isWorking is False
    assert by_id[fork_id].agentName == "explore"

    assert by_id[main_agent_id].isSubagent is False
    assert by_id[main_agent_id].isWorking is True
    assert by_id[main_agent_id].agentName == "grok-build-plan"


def test_hub_title_wins_for_display_title(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    d = root / "proj" / sid
    _write_summary(
        d / "summary.json",
        {
            "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
            "hub_title": "My rename",
            "generated_title": "Generated",
            "session_summary": "Summary",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 1,
        },
    )
    results = scan_sessions(root, limit=10)
    assert len(results) == 1
    assert results[0].title == "My rename"


def test_rename_session_writes_hub_title(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    d = root / "proj" / sid
    _write_summary(
        d / "summary.json",
        {
            "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Old title",
            "session_summary": "Old title",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 2,
            "current_model_id": "grok-4.5",
        },
    )

    updated = rename_session(root, sid, "  Fresh name  ")
    assert updated is not None
    assert updated.title == "Fresh name"
    assert updated.sessionId == sid

    data = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert data["hub_title"] == "Fresh name"
    assert data["generated_title"] == "Fresh name"

    rescanned = scan_sessions(root, limit=10)
    assert rescanned[0].title == "Fresh name"


def test_rename_session_rejects_empty_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    d = root / "proj" / sid
    _write_summary(
        d / "summary.json",
        {
            "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Keep me",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 1,
        },
    )

    assert rename_session(root, sid, "   ") is None
    assert rename_session(root, sid, "") is None
    assert rename_session(root, "00000000-0000-0000-0000-000000000000", "Nope") is None

    data = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert "hub_title" not in data
    assert data["generated_title"] == "Keep me"


def test_rename_session_truncates_long_title(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    d = root / "proj" / sid
    _write_summary(
        d / "summary.json",
        {
            "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Short",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 1,
        },
    )
    long_title = "x" * 250
    updated = rename_session(root, sid, long_title)
    assert updated is not None
    assert len(updated.title) == 200
    data = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert len(data["hub_title"]) == 200


def test_delete_session_removes_folder(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    d = root / "proj" / sid
    _write_summary(
        d / "summary.json",
        {
            "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Delete me",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 2,
        },
    )
    (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")

    assert d.is_dir()
    assert delete_session(root, sid) is True
    assert not d.exists()
    assert scan_sessions(root, limit=10) == []


def test_delete_session_missing_returns_false(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    assert delete_session(root, "00000000-0000-0000-0000-000000000000") is False
    assert delete_session(root, "not-a-uuid") is False
    assert delete_session(root, "") is False


def test_delete_session_refuses_path_outside_root(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside"
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    d = outside / sid
    _write_summary(
        d / "summary.json",
        {
            "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Outside",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 1,
        },
    )

    fake = SessionInfo(
        sessionId=sid,
        title="Outside",
        cwd=r"D:\Projects\Alpha",
        updatedAt="2026-07-09T12:00:00Z",
        modelId="",
        path=str(d),
    )
    monkeypatch.setattr(
        "hub.session_index.find_session",
        lambda *_args, **_kwargs: fake,
    )
    assert delete_session(root, sid) is False
    assert d.is_dir()
    assert (d / "summary.json").is_file()


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


def test_is_noise_session_temp_e2e() -> None:
    assert is_noise_session(r"C:\Users\me\AppData\Local\Temp\grok-hub-e2e\run1")
    assert is_noise_session(r"C:\Users\me\AppData\Local\Temp\oracle-grok-abc")
    assert is_noise_session(r"C:\Users\me\AppData\Local\Temp\pytest-of-me")
    assert is_noise_session(r"C:\Users\me\AppData\Local\Temp\some-scratch")
    assert is_noise_session(r"D:\Projects\Alpha", "e2e-turn-42")
    assert is_noise_session(r"D:\Projects\Alpha", "safari_e2e offline")
    assert is_noise_session(r"C:\tmp\grok-hub\work")
    assert not is_noise_session(r"D:\Projects\Grok Remote Hub", "Implement filters")
    assert not is_noise_session(r"D:\Projects\Alpha", "normal work")
    assert not is_noise_session(r"C:\Users\me\code\app", "hello")


def test_scan_sessions_is_working_vs_noise(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    main_id = "019f493c-af12-7652-a6d8-bf645c10921c"
    noise_id = "019f06ee-305f-7903-b295-d706c08f0b4f"
    sub_id = "019f1111-2222-3333-4444-555555555555"

    main = root / "proj" / main_id
    _write_summary(
        main / "summary.json",
        {
            "info": {"id": main_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Main project work",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 2,
        },
    )

    noise = root / "temp" / noise_id
    _write_summary(
        noise / "summary.json",
        {
            "info": {
                "id": noise_id,
                "cwd": r"C:\Users\me\AppData\Local\Temp\grok-hub-e2e\x",
            },
            "generated_title": "Temp e2e run",
            "updated_at": "2026-07-09T13:00:00Z",
            "num_chat_messages": 1,
        },
    )

    sub = root / "proj" / sub_id
    _write_summary(
        sub / "summary.json",
        {
            "info": {"id": sub_id, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Worker",
            "session_kind": "subagent",
            "agent_name": "general-purpose",
            "parent_session_id": main_id,
            "updated_at": "2026-07-09T14:00:00Z",
            "num_chat_messages": 1,
        },
    )

    results = scan_sessions(root, limit=80)
    by_id = {r.sessionId: r for r in results}

    assert by_id[main_id].isWorking is True
    assert by_id[main_id].isNoise is False
    assert by_id[main_id].isSubagent is False

    assert by_id[noise_id].isNoise is True
    assert by_id[noise_id].isWorking is False
    assert by_id[noise_id].isSubagent is False

    assert by_id[sub_id].isSubagent is True
    assert by_id[sub_id].isWorking is False
    assert by_id[sub_id].isNoise is False


def test_stamp_hub_origin(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    sid = "019f493c-af12-7652-a6d8-bf645c10921c"
    d = root / "proj" / sid
    _write_summary(
        d / "summary.json",
        {
            "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
            "generated_title": "Stamp me",
            "updated_at": "2026-07-09T12:00:00Z",
            "num_chat_messages": 1,
        },
    )

    assert stamp_hub_origin(root, sid, "user") is True
    data = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert data["hub_origin"] == "user"

    results = scan_sessions(root, limit=10)
    assert results[0].hubOrigin == "user"
    assert results[0].isWorking is True

    assert stamp_hub_origin(root, sid, "attach") is True
    data = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert data["hub_origin"] == "attach"
    assert scan_sessions(root, limit=10)[0].hubOrigin == "attach"

    assert stamp_hub_origin(root, sid, "nope") is False
    assert stamp_hub_origin(root, "00000000-0000-0000-0000-000000000000", "user") is False


def test_scan_hub_remote_flag(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    live_id = "019f493c-af12-7652-a6d8-bf645c10921c"
    other_id = "019f06ee-305f-7903-b295-d706c08f0b4f"

    for sid, title in ((live_id, "Live hub"), (other_id, "History only")):
        d = root / "proj" / sid
        _write_summary(
            d / "summary.json",
            {
                "info": {"id": sid, "cwd": r"D:\Projects\Alpha"},
                "generated_title": title,
                "updated_at": "2026-07-09T12:00:00Z",
                "num_chat_messages": 1,
            },
        )

    plain = scan_sessions(root, limit=10)
    assert all(not r.isHubRemote for r in plain)

    flagged = scan_sessions(root, limit=10, hub_remote_ids={live_id})
    by_id = {r.sessionId: r for r in flagged}
    assert by_id[live_id].isHubRemote is True
    assert by_id[live_id].isWorking is True
    assert by_id[other_id].isHubRemote is False
