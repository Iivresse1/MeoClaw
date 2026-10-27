"""Tavily credential resolution scoped to a single task."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from meoclaw.providers.openai_provider import load_env

_TASK_TAVILY_KEY: ContextVar[str | None] = ContextVar(
    "meoclaw_tavily_api_key",
    default=None,
)


@contextmanager
def tavily_api_key_scope(value: str | None) -> Iterator[None]:
    """Make a key available only while the current task is running."""
    token = _TASK_TAVILY_KEY.set((value or "").strip() or None)
    try:
        yield
    finally:
        _TASK_TAVILY_KEY.reset(token)


def resolve_tavily_api_key(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    scoped = (_TASK_TAVILY_KEY.get() or "").strip()
    if scoped:
        return scoped
    load_env()
    return (os.getenv("TAVILY_API_KEY") or "").strip()
