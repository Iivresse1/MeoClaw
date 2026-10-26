"""Bridge authentication and filesystem-boundary regression tests."""

from __future__ import annotations

import queue
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meoclaw_bridge import config, server, store
from meoclaw_bridge.adapters.runner import ResumeRegistry


def test_session_workspace_rejects_traversal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(config, "SESSIONS", sessions)

    valid = config.session_workspace("a" * 12)
    assert valid.parent == sessions.resolve()

    for value in ("..", "C:\\Windows", "a" * 11, "../" + "a" * 12):
        with pytest.raises(ValueError):
            config.session_workspace(value)


def test_delete_session_never_rmtree_arbitrary_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    db = tmp_path / "store.db"
    monkeypatch.setattr(config, "SESSIONS", sessions)
    monkeypatch.setattr(store, "DB", db)
    store.init()

    outside = tmp_path / "must-survive"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    assert store.delete_session(str(outside)) is False
    assert store.delete_session("..") is False
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


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


def test_approval_resume_is_consumed_once() -> None:
    registry = ResumeRegistry()
    events = queue.Queue()
    registry.register_queue("task-1", events)
    registry.register_approval("approval-1", "task-1")

    task_id, returned_queue = registry.take_approval("approval-1")
    assert task_id == "task-1"
    assert returned_queue is events
    assert registry.take_approval("approval-1") is None
