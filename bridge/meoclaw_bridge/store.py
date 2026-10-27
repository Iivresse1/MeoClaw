"""SQLite 会话与消息持久化。

会话只支持归档/恢复，不提供永久删除。Schema v2 会清空旧版会话表，由 Bridge
启动流程同时移除旧 workspace 和 checkpoint。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config

DB = config.BASE / "store.db"
SCHEMA_VERSION = 2


@contextmanager
def _conn():
    """连接即用即关，避免 Windows 下文件被长期占用。"""
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> bool:
    """Create the current schema and report whether legacy data was discarded."""
    DB.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        version = int(c.execute("PRAGMA user_version").fetchone()[0])
        reset_legacy = version < SCHEMA_VERSION
        if reset_legacy:
            c.execute("DROP TABLE IF EXISTS messages")
            c.execute("DROP TABLE IF EXISTS sessions")
        c.execute(
            "CREATE TABLE IF NOT EXISTS sessions("
            "id TEXT PRIMARY KEY, title TEXT, permission TEXT, "
            "created_at REAL, archived_at REAL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS messages("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
            "content TEXT, meta TEXT, created_at REAL)"
        )
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return reset_legacy


def create_session(title: str, permission: str) -> str:
    sid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,NULL)",
            (sid, title[:60] or "新会话", permission, time.time()),
        )
    return sid


def list_sessions(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["archived"] = item.pop("archived_at") is not None
        result.append(item)
    return result


def session_exists(sid: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
    return row is not None


def session_is_archived(sid: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT archived_at FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
    return row is None or row["archived_at"] is not None


def set_session_permission(sid: str, permission: str) -> bool:
    if (
        not config.SESSION_ID_RE.fullmatch(str(sid or ""))
        or permission not in {"human", "ai", "full"}
    ):
        return False
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if exists is None:
            return False
        c.execute(
            "UPDATE sessions SET permission=? WHERE id=?",
            (permission, sid),
        )
    return True


def set_session_archived(sid: str, archived: bool) -> bool:
    if not config.SESSION_ID_RE.fullmatch(str(sid or "")):
        return False
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if exists is None:
            return False
        c.execute(
            "UPDATE sessions SET archived_at=? WHERE id=?",
            (time.time() if archived else None, sid),
        )
    return True


def add_message(
    sid: str,
    role: str,
    content: str,
    meta: dict | None = None,
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO messages(session_id,role,content,meta,created_at) "
            "VALUES(?,?,?,?,?)",
            (
                sid,
                role,
                content,
                json.dumps(meta or {}, ensure_ascii=False),
                time.time(),
            ),
        )


def get_messages(sid: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
    return [dict(r) for r in rows]
