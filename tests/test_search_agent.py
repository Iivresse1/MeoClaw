"""agents.search_agent 测试：离线跑 searchAgent，检查检索事件/来源去重/摘要。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from meoclaw.agents.search_agent import (
    SEARCH_AGENT_NAME,
    build_search_agent_tools,
    compose_search_message,
    run_search_agent,
)
from meoclaw.core.state import RuntimeState
from meoclaw.prompts.stage3 import SEARCH_AGENT_PROMPT
from meoclaw.tools.web_search_tool import WEB_SEARCH_TOOL_NAME


class Fake:
    """按脚本返回 AIMessage 的假模型。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[list[BaseMessage]] = []
        self.bound_tool_names: list[str] = []

    def bind_tools(self, tools: Any) -> "Fake":
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("脚本里的回应已经用完了")
        return self._responses.pop(0)


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _fake_web_search(query: str, max_results: int = 5, include_answer: bool = True) -> dict[str, Any]:
    return {
        "ok": True,
        "query": query,
        "answer": f"关于 {query} 的回答",
        "results": [
            {"title": "官方文档", "url": "https://example.com/docs", "content": "…", "score": 0.9},
            {"title": "重复来源", "url": "https://example.com/docs", "content": "…", "score": 0.8},
            {"title": "百科条目", "url": "https://example.com/wiki", "content": "…", "score": 0.7},
        ],
    }


@pytest.fixture()
def runtime(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path / "ws")


@pytest.fixture()
def state(runtime: RuntimeState) -> dict[str, Any]:
    return {
        "task": "调研 LangGraph 的 add_messages 语义",
        "runtime": runtime,
        "research_notes": "",
    }


@pytest.fixture()
def fake_search(monkeypatch: pytest.MonkeyPatch) -> None:
    from meoclaw.tools import web_search_tool

    monkeypatch.setattr(web_search_tool, "web_search", _fake_web_search)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def test_run_search_agent_collects_queries_and_sources(
    state: dict[str, Any], fake_search: None
) -> None:
    model = Fake(
        [
            tool_call("WebSearchTool", {"query": "LangGraph add_messages"}, "q1"),
            AIMessage(content="研究结论：add_messages 是按 id 合并追加的 reducer。"),
        ]
    )
    events: list[dict[str, Any]] = []

    result = run_search_agent(state, "查 add_messages 语义", writer=events.append, model=model)

    assert result["ok"] is True
    assert result["agent"] == SEARCH_AGENT_NAME
    assert result["stopped_reason"] == "no_tool_calls"
    assert result["summary"] == "研究结论：add_messages 是按 id 合并追加的 reducer。"
    assert result["queries"] == ["LangGraph add_messages"]
    # 重复 URL 被去重，只保留 title/url
    assert result["sources"] == [
        {"title": "官方文档", "url": "https://example.com/docs"},
        {"title": "百科条目", "url": "https://example.com/wiki"},
    ]

    # 事件带着 agent 标记，writer 收到同样的事件
    assert all(event["agent"] == SEARCH_AGENT_NAME for event in result["tool_events"])
    assert result["tool_events"] == events
    assert [event["type"] for event in result["tool_events"]][:3] == [
        "ai_message",
        "tool_call",
        "tool_result",
    ]

    # 系统提示词与工具集：检索专家只有 WebSearchTool
    assert isinstance(model.calls[0][0], SystemMessage)
    assert model.calls[0][0].content == SEARCH_AGENT_PROMPT
    assert model.bound_tool_names == [WEB_SEARCH_TOOL_NAME]


def test_search_agent_falls_back_to_answers(state: dict[str, Any], fake_search: None) -> None:
    """收尾没有文本时，摘要回落到各次检索的 answer 拼接。"""
    model = Fake(
        [
            tool_call("WebSearchTool", {"query": "LangGraph"}, "q1"),
            AIMessage(content=""),  # 收尾没有文本
        ]
    )

    result = run_search_agent(state, "查资料", model=model)

    assert result["summary"] == "关于 LangGraph 的回答"


def test_search_agent_skips_failed_search_results(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """检索失败（缺 Key 等）不进 sources，也不打断循环。"""
    from meoclaw.tools import web_search_tool

    def failing_search(query: str, max_results: int = 5, include_answer: bool = True) -> dict[str, Any]:
        return {"ok": False, "query": query, "error": "missing required setting: TAVILY_API_KEY"}

    monkeypatch.setattr(web_search_tool, "web_search", failing_search)
    model = Fake(
        [
            tool_call("WebSearchTool", {"query": "hello"}, "q1"),
            AIMessage(content="检索不可用，基于已有知识回答"),
        ]
    )

    result = run_search_agent(state, "查一下", model=model)

    assert result["sources"] == []
    assert result["summary"] == "检索不可用，基于已有知识回答"


def test_compose_search_message_contains_all_parts(state: dict[str, Any]) -> None:
    state["research_notes"] = "已有笔记：LangGraph 是状态机框架"

    message = compose_search_message(state, "补充 add_messages 的细节")

    assert "## Task" in message
    assert "调研 LangGraph 的 add_messages 语义" in message
    assert "## Research instruction from planner" in message
    assert "补充 add_messages 的细节" in message
    assert "## Existing research notes" in message
    assert "已有笔记：LangGraph 是状态机框架" in message
    assert "## Memory snapshot" in message
    assert "Search as needed" in message


def test_build_search_agent_tools_only_contains_web_search() -> None:
    assert [tool.name for tool in build_search_agent_tools()] == [WEB_SEARCH_TOOL_NAME]


def test_run_search_agent_uses_model_from_runtime_metadata(
    state: dict[str, Any], fake_search: None
) -> None:
    model = Fake([AIMessage(content="来自 metadata 的模型")])
    state["runtime"].metadata["model"] = model

    result = run_search_agent(state, "随便查点")

    assert result["summary"] == "来自 metadata 的模型"


def test_run_search_agent_reports_max_loops(state: dict[str, Any], fake_search: None) -> None:
    model = Fake(
        [tool_call("WebSearchTool", {"query": f"q{i}"}, f"c{i}") for i in range(3)]
    )

    result = run_search_agent(state, "查资料", max_loops=3, model=model)

    assert result["ok"] is False
    assert result["stopped_reason"] == "max_loops"
    assert result["loops"] == 3
    assert len(result["queries"]) == 3


def test_run_search_agent_accepts_runtime_state_directly(
    runtime: RuntimeState, fake_search: None
) -> None:
    model = Fake([AIMessage(content="直接给 RuntimeState 也行")])

    result = run_search_agent(runtime, "看看情况", model=model)

    assert result["ok"] is True
    assert "## Research instruction from planner" in model.calls[0][1].content


@pytest.mark.parametrize("instruction", ["", "   ", None])
def test_run_search_agent_rejects_blank_instruction(
    state: dict[str, Any], instruction: Any
) -> None:
    with pytest.raises(ValueError):
        run_search_agent(state, instruction, model=Fake([]))


@pytest.mark.parametrize("max_loops", [0, -1, 2.5, True])
def test_run_search_agent_rejects_bad_max_loops(
    state: dict[str, Any], max_loops: Any
) -> None:
    with pytest.raises(ValueError):
        run_search_agent(state, "任务", max_loops=max_loops, model=Fake([]))
