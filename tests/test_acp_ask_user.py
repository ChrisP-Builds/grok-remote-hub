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
        "outcome": "accepted",
        "answers": {
            "q0": ["opt0", "other text"],
            "q1": ["only"],
        },
        "partial_answers": {},
    }


def test_build_accepted_already_shaped() -> None:
    """Legacy {"values": [...]} input is unwrapped to list[str] (StringOrVec)."""
    result = build_accepted_result({"q0": {"values": ["a", "b"]}})
    assert result["outcome"] == "accepted"
    assert result["answers"]["q0"] == ["a", "b"]
    assert result["partial_answers"] == {}


def test_build_accepted_empty() -> None:
    empty = {
        "outcome": "accepted",
        "answers": {},
        "partial_answers": {},
    }
    assert build_accepted_result(None) == empty
    assert build_accepted_result({}) == empty


def test_build_cancelled_result_shape() -> None:
    result = build_cancelled_result()
    assert result == {"outcome": "cancelled"}
    assert result["outcome"] == "cancelled"


def test_outcome_is_string_not_dict() -> None:
    """Agent deserializes as internally tagged enum; outcome must be str, not map."""
    accepted = build_accepted_result({"q0": ["opt0"]})
    cancelled = build_cancelled_result()

    assert isinstance(accepted["outcome"], str)
    assert not isinstance(accepted["outcome"], dict)
    assert accepted["outcome"] == "accepted"

    assert isinstance(cancelled["outcome"], str)
    assert not isinstance(cancelled["outcome"], dict)
    assert cancelled["outcome"] == "cancelled"


def test_multi_values_list() -> None:
    result = build_accepted_result({"q-multi": ["opt0", "opt2", "custom"]})
    assert result["outcome"] == "accepted"
    values = result["answers"]["q-multi"]
    assert values == ["opt0", "opt2", "custom"]
    assert isinstance(values, list)
    # Must be StringOrVec, not {"values": [...]}
    assert not isinstance(values, dict)


def test_internally_tagged_enum_shape() -> None:
    """Accepted puts answers/partial_answers at top level beside outcome string."""
    accepted = build_accepted_result({"q0": ["opt0"]})
    cancelled = build_cancelled_result()

    assert set(accepted.keys()) == {"outcome", "answers", "partial_answers"}
    assert accepted["outcome"] == "accepted"
    assert accepted["answers"] == {"q0": ["opt0"]}
    assert accepted["partial_answers"] == {}

    assert set(cancelled.keys()) == {"outcome"}
    assert cancelled["outcome"] == "cancelled"

    # Permission replies stay nested; ask_user must not mirror that shape.
    permission_like = {"outcome": {"outcome": "selected", "optionId": "allow"}}
    assert isinstance(permission_like["outcome"], dict)
    assert not isinstance(accepted["outcome"], dict)
