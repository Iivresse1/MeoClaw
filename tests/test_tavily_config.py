"""Tavily setup through the Bridge settings API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from meoclaw_bridge import config, server


def _client() -> tuple[TestClient, dict[str, str]]:
    server.BRIDGE_TOKEN = "test-secret"
    return TestClient(server.app), {"Authorization": "Bearer test-secret"}


def test_tavily_status_never_exposes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-value")
    client, headers = _client()

    response = client.get("/api/tavily", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "configured": True, "source": "env"}
    assert "tvly-secret-value" not in response.text


def test_tavily_key_prefers_environment_and_uses_dedicated_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        config,
        "set_secret",
        lambda name, value: stored.append((name, value)),
    )
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    config.set_tavily_api_key("tvly-keyring")

    assert stored == [(config.TAVILY_SECRET_NAME, "tvly-keyring")]
    monkeypatch.setattr(
        config,
        "get_secret",
        lambda name: "tvly-keyring" if name == config.TAVILY_SECRET_NAME else "",
    )
    assert config.tavily_key_status() == ("tvly-keyring", "keyring")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env")
    assert config.tavily_key_status() == ("tvly-env", "env")


def test_tavily_key_is_saved_only_after_successful_connection_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meoclaw.tools import web_search_tool

    saved: list[str] = []
    monkeypatch.setattr(config, "set_tavily_api_key", saved.append)
    monkeypatch.setattr(
        web_search_tool,
        "web_search",
        lambda *args, **kwargs: {"ok": True, "query": args[0], "results": []},
    )
    client, headers = _client()

    response = client.post(
        "/api/tavily",
        headers=headers,
        json={"api_key": "tvly-valid"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert saved == ["tvly-valid"]


def test_tavily_key_is_not_saved_when_connection_test_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meoclaw.tools import web_search_tool

    saved: list[str] = []
    monkeypatch.setattr(config, "set_tavily_api_key", saved.append)
    monkeypatch.setattr(
        web_search_tool,
        "web_search",
        lambda *args, **kwargs: {"ok": False, "error": "invalid api key"},
    )
    client, headers = _client()

    response = client.post(
        "/api/tavily",
        headers=headers,
        json={"api_key": "tvly-invalid"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert saved == []


def test_tavily_key_can_be_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[str] = []
    monkeypatch.setattr(config, "set_tavily_api_key", saved.append)
    client, headers = _client()

    response = client.delete("/api/tavily", headers=headers)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert saved == [""]
