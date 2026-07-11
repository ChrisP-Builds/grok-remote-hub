"""Unit tests for ACP ask_user_question helpers."""

from __future__ import annotations

from hub.acp_ask_user import (
    build_accepted_result,
    build_cancelled_result,
    normalize_questions,
)


def test_normalize_camel_case() -> None:
    params = {
        "questions": [
            {
                "id": "q-a",
                "text": "Pick one?",
                "multiSelect": False,
                "options": [
                    {"id": "o1", "label": "A", "description": "desc A", "preview": "pA"},
                    {"id": "o2", "label": "B"},
                ],
            }
        ]
    }
    qs = normalize_questions(params)
    assert len(qs) == 1
    assert qs[0]["id"] == "q-a"
    assert qs[0]["text"] == "Pick one?"
    assert qs[0]["multiSelect"] is False
    assert qs[0]["options"][0] == {
        "id": "o1",
        "label": "A",
        "description": "desc A",
        "preview": "pA",
    }
    assert qs[0]["options"][1]["id"] == "o2"


def test_normalize_snake_case() -> None:
    params = {
        "questions": [
            {
                "id": "q1",
                "question": "Choose many",
                "multi_select": True,
                "options": [
                    {"id": "x", "label": "X", "description": "", "preview": ""},
                ],
            }
        ]
    }
    qs = normalize_questions(params)
    assert qs[0]["text"] == "Choose many"
    assert qs[0]["multiSelect"] is True
    assert qs[0]["options"][0]["label"] == "X"


def test_missing_ids_generated() -> None:
    params = {
        "questions": [
            {
                "text": "First?",
                "options": [
                    {"label": "Yes"},
                    {"label": "No"},
                ],
            },
            {
                "text": "Second?",
                "options": [],
            },
        ]
    }
    qs = normalize_questions(params)
    assert qs[0]["id"] == "q0"
    assert qs[0]["options"][0]["id"] == "opt0"
    assert qs[0]["options"][1]["id"] == "opt1"
    assert qs[1]["id"] == "q1"


def test_strip_empty_question_and_options() -> None:
    params = {
        "questions": [
            {"id": "empty", "text": "  ", "options": [{"label": "A"}]},
            {
                "id": "ok",
                "text": "Keep",
                "options": [
                    {"id": "blank", "label": ""},
                    {"id": "keep", "label": "Keep me"},
                    "not-a-map",
                ],
            },
            "not-a-map",
        ]
    }
    qs = normalize_questions(params)
    assert len(qs) == 1
    assert qs[0]["id"] == "ok"
    assert len(qs[0]["options"]) == 1
    assert qs[0]["options"][0]["id"] == "keep"


def test_normalize_empty_params() -> None:
    assert normalize_questions(None) == []
    assert normalize_questions({}) == []
    assert normalize_questions({"questions": None}) == []
    assert normalize_questions({"questions": "nope"}) == []


def test_build_accepted_result_shape() -> None:
    result = build_accepted_result({"q0": ["opt0", "other text"], "q1": ["only"]})
    assert result == {
        "outcome": {
            "outcome": "accepted",
            "answers": {
                "q0": {"values": ["opt0", "other text"]},
                "q1": {"values": ["only"]},
            },
        }
    }


def test_build_accepted_already_shaped() -> None:
    result = build_accepted_result({"q0": {"values": ["a", "b"]}})
    assert result["outcome"]["outcome"] == "accepted"
    assert result["outcome"]["answers"]["q0"]["values"] == ["a", "b"]


def test_build_accepted_empty() -> None:
    empty = {"outcome": {"outcome": "accepted", "answers": {}}}
    assert build_accepted_result(None) == empty
    assert build_accepted_result({}) == empty


def test_build_cancelled_result_shape() -> None:
    result = build_cancelled_result()
    assert result == {"outcome": {"outcome": "cancelled"}}
    assert result["outcome"]["outcome"] == "cancelled"


def test_multi_values_list() -> None:
    result = build_accepted_result({"q-multi": ["opt0", "opt2", "custom"]})
    assert result["outcome"]["outcome"] == "accepted"
    values = result["outcome"]["answers"]["q-multi"]["values"]
    assert values == ["opt0", "opt2", "custom"]
    assert isinstance(values, list)


def test_permission_style_nesting() -> None:
    """Mirrors permission shape: outer outcome is dict; inner has string outcome key."""
    accepted = build_accepted_result({"q0": ["opt0"]})
    cancelled = build_cancelled_result()

    assert isinstance(accepted["outcome"], dict)
    assert isinstance(accepted["outcome"]["outcome"], str)
    assert accepted["outcome"]["outcome"] == "accepted"
    assert "answers" in accepted["outcome"]

    assert isinstance(cancelled["outcome"], dict)
    assert isinstance(cancelled["outcome"]["outcome"], str)
    assert cancelled["outcome"]["outcome"] == "cancelled"

    # Same nesting pattern as permission selected:
    # {"outcome": {"outcome": "selected", "optionId": ...}}
    permission_like = {"outcome": {"outcome": "selected", "optionId": "allow"}}
    assert set(permission_like["outcome"].keys()) >= {"outcome"}
    assert isinstance(permission_like["outcome"]["outcome"], str)
    assert set(accepted["outcome"].keys()) >= {"outcome"}
    assert isinstance(accepted["outcome"]["outcome"], str)
