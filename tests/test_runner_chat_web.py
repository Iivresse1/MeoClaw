"""Chat mode can use the global web tools without changing the public task API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from meoclaw_bridge.adapters import runner


class ScriptedChatModel:
    def __init__(self) -> None:
        self.responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "WebSearchTool",
                        "args": {"query": "MeoClaw"},
                        "id": "search-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已联网确认。"),
        ]
        self.bound_tool_names: list[str] = []

    def bind_tools(self, tools: Any) -> ScriptedChatModel:
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        return self.responses.pop(0)


def test_chat_mode_streams_a_web_tool_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meoclaw.tools import web_search_tool

    model = ScriptedChatModel()
    monkeypatch.setattr(runner.model_factory, "build_model", lambda settings: model)
    monkeypatch.setattr(runner.config, "get_tavily_api_key", lambda: "test-key")
    monkeypatch.setattr(
        web_search_tool,
        "web_search",
        lambda query, **kwargs: {
            "ok": True,
            "query": query,
            "answer": "MeoClaw 是本地 Agent。",
            "results": [],
        },
    )

    events = runner.start(
        "chat",
        "检查 MeoClaw 是否可联网",
        {},
        tmp_path / "ws",
        history=[],
        task_id="task-chat",
        session_id="session-chat",
        checkpointer=InMemorySaver(),
    )

    pushed: list[dict[str, Any]] = []
    while True:
        event = events.get(timeout=10)
        pushed.append(event)
        if event["type"] == "ui_done":
            break

    assert model.bound_tool_names == ["WebSearchTool", "WebFetchTool"]
    assert any(
        event["type"] == "tool_call" and event["name"] == "WebSearchTool"
        for event in pushed
    )
    final = next(event for event in pushed if event["type"] == "final_answer")
    assert final["content"] == "已联网确认。"
