"""SQLite 会话与消息持久化（桌面层自有，不依赖后端）。

会话懒创建：首条消息才落库，避免侧边栏堆积空「新会话」记录。
"""
import json
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config

DB = config.BASE / "store.db"


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


def init() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, title TEXT, mode TEXT, created_at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, meta TEXT, created_at REAL)")


def create_session(title: str, mode: str) -> str:
    sid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (sid, title[:60] or "新会话", mode, time.time()))
    return sid


def list_sessions(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def session_exists(sid: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
    return row is not None


def delete_session(sid: str) -> bool:
    if not config.SESSION_ID_RE.fullmatch(str(sid or "")):
        return False
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if exists is None:
            return False
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    root = config.SESSIONS.resolve()
    workspace = (root / sid).resolve()
    if workspace.parent == root and workspace.exists():
        shutil.rmtree(workspace)
    return True


def add_message(sid: str, role: str, content: str, meta: dict | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO messages(session_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
                  (sid, role, content, json.dumps(meta or {}, ensure_ascii=False), time.time()))


def get_messages(sid: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    return [dict(r) for r in rows]
