"""FIFO prompt queue while an agent turn is running."""

from __future__ import annotations

from typing import Any


class PromptQueue:
    """In-memory FIFO queue with a fixed max size.

    Pure data structure (no locks). Callers serialize access if needed.
    """

    def __init__(self, max_size: int = 10) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self.max_size = max_size
        self._items: list[dict[str, Any]] = []

    def try_enqueue(self, item: dict[str, Any]) -> int | None:
        """Append item. Return 1-based position, or None if full."""
        if len(self._items) >= self.max_size:
            return None
        self._items.append(item)
        return len(self._items)

    def pop(self) -> dict[str, Any] | None:
        """Remove and return the oldest item, or None if empty."""
        if not self._items:
            return None
        return self._items.pop(0)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
