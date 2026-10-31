"""session_matches_query pure helper."""

from __future__ import annotations

from hub.session_search import session_matches_query


def test_empty_query_matches_all() -> None:
    assert session_matches_query("Hello", "abc-123", r"D:\Projects\Foo", "") is True
    assert session_matches_query("", "", "", "   ") is True


def test_matches_title() -> None:
    assert session_matches_query("Fix login bug", "sid", r"C:\x", "login") is True
    assert session_matches_query("Fix login bug", "sid", r"C:\x", "logout") is False


def test_matches_session_id_substring() -> None:
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert session_matches_query("t", sid, r"C:\x", "bbbb") is True
    assert session_matches_query("t", sid, r"C:\x", "aaaa") is True
    assert session_matches_query("t", sid, r"C:\x", "zzzz") is False


def test_matches_cwd_basename() -> None:
    assert (
        session_matches_query("t", "sid", r"D:\Projects\Grok Remote Hub", "remote")
        is True
    )
    assert (
        session_matches_query("t", "sid", r"D:\Projects\Grok Remote Hub", "otherproj")
        is False
    )


def test_matches_full_cwd_path() -> None:
    assert session_matches_query("t", "sid", r"D:\Projects\Alpha", "projects") is True


def test_case_insensitive() -> None:
    assert session_matches_query("Hello World", "ABC", r"C:\Foo", "hello") is True
    assert session_matches_query("Hello World", "ABC", r"C:\Foo", "abc") is True
    assert session_matches_query("Hello World", "ABC", r"C:\Foo", "FOO") is True
