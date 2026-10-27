"""Bridge authentication and filesystem-boundary regression tests."""

from __future__ import annotations

import queue
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meoclaw_bridge import config, server, store
from meoclaw_bridge.adapters.runner import ApprovalRegistry


def test_session_workspace_rejects_traversal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(config, "SESSIONS", sessions)

    valid = config.session_workspace("a" * 12)
    assert valid.parent == sessions.resolve()

    for value in ("..", "C:\\Windows", "a" * 11, "../" + "a" * 12):
        with pytest.raises(ValueError):
            config.session_workspace(value)


def test_sessions_archive_and_restore_without_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    db = tmp_path / "store.db"
    monkeypatch.setattr(config, "SESSIONS", sessions)
    monkeypatch.setattr(store, "DB", db)
    store.init()

    sid = store.create_session("测试会话", "human")
    assert store.set_session_archived(sid, True) is True
    assert store.session_is_archived(sid) is True
    assert store.list_sessions()[0]["archived"] is True
    assert store.set_session_archived(sid, False) is True
    assert store.session_is_archived(sid) is False
    assert not hasattr(store, "delete_session")


def test_legacy_schema_reset_returns_empty_session_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(store, "DB", db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE sessions("
            "id TEXT PRIMARY KEY, title TEXT, mode TEXT, created_at REAL)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES('aaaaaaaaaaaa','旧会话','chat',1)"
        )

    assert store.init() is True
    assert store.list_sessions() == []
    assert store.init() is False


def test_archive_api_has_no_delete_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(store, "DB", tmp_path / "store.db")
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    store.init()
    sid = store.create_session("可归档会话", "human")
    monkeypatch.setattr(server, "BRIDGE_TOKEN", "test-secret")
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer test-secret"}

    archived = client.post(
        f"/api/sessions/{sid}/archive",
        headers=headers,
        json={"archived": True},
    )

    assert archived.status_code == 200
    assert client.get("/api/sessions", headers=headers).json()[0]["archived"] is True
    assert client.delete(
        f"/api/sessions/{sid}", headers=headers
    ).status_code in {404, 405}
    blocked = client.post(
        "/api/task",
        headers=headers,
        json={"task": "继续", "permission": "human", "session_id": sid},
    )
    assert blocked.json()["ok"] is False
    assert "恢复会话" in blocked.json()["message"]


def test_bridge_token_and_origins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "bridge_token"
    monkeypatch.setattr(config, "BRIDGE_TOKEN_FILE", token_file)
    monkeypatch.delenv("MEOCLAW_BRIDGE_TOKEN", raising=False)
    first = config.get_or_create_bridge_token()
    second = config.get_or_create_bridge_token()
    assert first == second
    assert len(first) >= 32

    monkeypatch.setenv("MEOCLAW_ALLOWED_ORIGINS", "http://localhost:5173,null")
    assert config.allowed_origins() == ["http://localhost:5173", "null"]


def test_rest_api_requires_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "BRIDGE_TOKEN", "test-secret")
    client = TestClient(server.app)

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sessions").status_code == 401


def test_unauthorized_response_keeps_cors_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "BRIDGE_TOKEN", "test-secret")
    client = TestClient(server.app)

    response = client.get(
        "/api/sessions",
        headers={
            "Origin": "http://localhost:5173",
            "Authorization": "Bearer wrong-token",
        },
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_approval_request_is_resolved_once() -> None:
    registry = ApprovalRegistry()
    events = queue.Queue()
    result: dict[str, bool] = {}

    def wait_for_decision() -> None:
        result["approved"] = registry.request(
            events,
            "BashTool",
            "运行测试",
            timeout=5,
        )

    worker = threading.Thread(target=wait_for_decision)
    worker.start()
    request = events.get(timeout=2)
    approval_id = request["id"]

    assert registry.resolve(approval_id, True) is True
    worker.join(timeout=2)
    assert result["approved"] is True
    assert registry.resolve(approval_id, False) is False
