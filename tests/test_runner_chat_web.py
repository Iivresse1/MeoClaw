"""The unified Main Agent routes conversation to the read-only responder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from meoclaw_bridge.adapters import runner


class ScriptedChatModel:
    def __init__(self) -> None:
        self.responses = [
            AIMessage(content='{"workflow_kind":"answer"}'),
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


class ScriptedTaskModel(ScriptedChatModel):
    def __init__(self) -> None:
        self.responses = [
            AIMessage(content='{"workflow_kind":"task"}'),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "TodoWriteTool",
                        "args": {
                            "plan_summary": "写文件",
                            "todos": [
                                {
                                    "id": "t1",
                                    "content": "写 hello.txt",
                                    "status": "pending",
                                    "note": "",
                                }
                            ],
                            "acceptance_criteria": ["文件存在"],
                            "verification_commands": [],
                        },
                        "id": "plan-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "FileWriteTool",
                        "args": {"file_path": "hello.txt", "content": "hello\n"},
                        "id": "write-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已写入"),
            AIMessage(
                content=json.dumps(
                    {
                        "passed": True,
                        "reason": "文件存在",
                        "checks": [],
                        "recommended_next_instruction": "",
                    }
                )
            ),
            AIMessage(content="任务完成"),
        ]
        self.bound_tool_names: list[str] = []


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
        "human",
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
        event["type"] == "node_start" and event["node"] == "responder"
        for event in pushed
    )
    assert not any(event["type"] == "plan" for event in pushed)
    assert any(
        event["type"] == "tool_call" and event["name"] == "WebSearchTool"
        for event in pushed
    )
    final = next(event for event in pushed if event["type"] == "final_answer")
    assert final["content"] == "已联网确认。"


def test_human_approval_resumes_blocked_workspace_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedTaskModel()
    monkeypatch.setenv("MEOCLAW_SANDBOX", "host")
    monkeypatch.setattr(runner.model_factory, "build_model", lambda settings: model)
    monkeypatch.setattr(runner.config, "get_tavily_api_key", lambda: "")

    events = runner.start(
        "human",
        "创建 hello.txt",
        {},
        tmp_path / "ws",
        history=None,
        task_id="task-human",
        session_id="session-human",
        checkpointer=InMemorySaver(),
    )

    pushed: list[dict[str, Any]] = []
    approval: dict[str, Any] | None = None
    while True:
        event = events.get(timeout=10)
        pushed.append(event)
        if event["type"] == "approval_request":
            approval = event
            assert runner.resume_approval(event["id"], True) is True
        if event["type"] == "ui_done":
            break

    assert approval is not None
    assert approval["tool"] == "FileWriteTool"
    assert (tmp_path / "ws" / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert any(event["type"] == "policy_decision" for event in pushed)
