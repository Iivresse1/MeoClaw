"""Checkpoint factories used by the outer graph and nested agent loops."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

__all__ = ["CheckpointStore", "create_checkpointer"]


@dataclass
class CheckpointStore:
    """Own a checkpoint saver and the SQLite connection behind it."""

    saver: BaseCheckpointSaver
    _connection: sqlite3.Connection | None = None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def create_checkpointer(
    path: str | Path | None,
) -> CheckpointStore:
    """Create a persistent SQLite saver, or an in-memory saver for tests.

    A ``None`` path deliberately means ephemeral storage.  Production code must
    pass a filesystem path so interrupted HITL runs survive process restarts.
    """
    if path is None:
        return CheckpointStore(saver=InMemorySaver())

    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return CheckpointStore(saver=saver, _connection=conn)
