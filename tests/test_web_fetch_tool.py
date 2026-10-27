"""WebFetchTool public behavior tests."""

from __future__ import annotations

import sys
import types

import pytest


def test_web_fetch_returns_bounded_page_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from meoclaw.tools import web_fetch_tool

    class FakeTavilyClient:
        def __init__(self, api_key: str) -> None:
            assert api_key == "test-key"

        def extract(
            self,
            urls,
            extract_depth,
            format,
            query,
            timeout,
        ):
            assert urls == ["https://example.com/docs"]
            assert extract_depth == "basic"
            assert format == "markdown"
            assert query == "installation"
            assert timeout == 30
            return {
                "results": [
                    {
                        "url": "https://example.com/docs",
                        "raw_content": "# Install\n\nRun the setup command.",
                    }
                ]
            }

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setitem(
        sys.modules,
        "tavily",
        types.SimpleNamespace(TavilyClient=FakeTavilyClient),
    )

    result = web_fetch_tool.web_fetch(
        "https://example.com/docs",
        query="installation",
        max_chars=24,
    )

    assert result["ok"] is True
    assert result["url"] == "https://example.com/docs"
    assert result["content"] == "# Install\n\nRun the setup"


def test_web_fetch_rejects_non_http_url() -> None:
    from meoclaw.tools.web_fetch_tool import web_fetch

    result = web_fetch("file:///C:/Windows/System32/drivers/etc/hosts")

    assert result["ok"] is False
    assert "http" in result["error"].lower()


def test_web_fetch_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from meoclaw.tools import web_access

    monkeypatch.setattr(web_access, "load_env", lambda: None)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    from meoclaw.tools.web_fetch_tool import web_fetch

    result = web_fetch("https://example.com")

    assert result["ok"] is False
    assert "TAVILY_API_KEY" in result["error"]


def test_web_fetch_tool_schema_and_name() -> None:
    from meoclaw.tools.web_fetch_tool import WEB_FETCH_TOOL_NAME, build_web_fetch_tool

    tool = build_web_fetch_tool()

    assert tool.name == WEB_FETCH_TOOL_NAME
    schema = tool.args_schema.model_json_schema()
    assert set(schema["properties"]) == {
        "url",
        "query",
        "extract_depth",
        "max_chars",
    }
