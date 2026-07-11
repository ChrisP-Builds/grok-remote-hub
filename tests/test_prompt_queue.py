"""Unit tests for hub.prompt_queue.PromptQueue."""

from __future__ import annotations

import pytest

from hub.prompt_queue import PromptQueue


def test_enqueue_returns_one_based_position() -> None:
    q = PromptQueue(max_size=10)
    assert q.try_enqueue({"text": "a"}) == 1
    assert q.try_enqueue({"text": "b"}) == 2
    assert len(q) == 2


def test_full_returns_none() -> None:
    q = PromptQueue(max_size=2)
    assert q.try_enqueue({"n": 1}) == 1
    assert q.try_enqueue({"n": 2}) == 2
    assert q.try_enqueue({"n": 3}) is None
    assert len(q) == 2


def test_pop_fifo() -> None:
    q = PromptQueue(max_size=10)
    q.try_enqueue({"id": "first"})
    q.try_enqueue({"id": "second"})
    assert q.pop() == {"id": "first"}
    assert q.pop() == {"id": "second"}
    assert q.pop() is None
    assert len(q) == 0


def test_clear() -> None:
    q = PromptQueue(max_size=5)
    q.try_enqueue({"a": 1})
    q.try_enqueue({"a": 2})
    q.clear()
    assert len(q) == 0
    assert q.pop() is None
    assert q.try_enqueue({"a": 3}) == 1


def test_max_size_one() -> None:
    q = PromptQueue(max_size=1)
    assert q.try_enqueue({"x": 1}) == 1
    assert q.try_enqueue({"x": 2}) is None
    assert q.pop() == {"x": 1}
    assert q.try_enqueue({"x": 3}) == 1


def test_invalid_max_size() -> None:
    with pytest.raises(ValueError):
        PromptQueue(max_size=0)
    with pytest.raises(ValueError):
        PromptQueue(max_size=-1)
