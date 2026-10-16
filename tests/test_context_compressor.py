"""context_compressor_node 测试：LLM 压缩、兜底、消息清空、落盘与截断。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import HISTORY_SUMMARY_FILENAME
from meoclaw.graph.nodes import (
    COMPRESSED_NOTES_LIMIT,
    MAX_COMPRESSED_SOURCES,
    MAX_COMPRESSION_EVENTS,
    _coerce_sources,
    _render_compression_digest,
    context_compressor_node,
)
from meoclaw.graph.state import MeoGraphState
from meoclaw.prompts.stage4 import CONTEXT_COMPRESSION_PROMPT


class ScriptedModel:
    """按脚本返回 AIMessage 的假模型。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Any) -> "ScriptedModel":
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("脚本里的回应已经用完了")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


DIGEST: dict[str, Any] = {
    "summary": "已实现 add，测试还没跑过。",
    "active_goal": "让 add 通过测试",
    "completed_work": ["utils.py 里的 add 已实现", "跑了 py_compile"],
    "open_todos": ["补 pytest 用例"],
    "important_files": ["utils.py", "tests/test_utils.py"],
    "tool_findings": "pytest 未安装 pytest-asyncio，异步用例会挂",
    "sources": [
        {"title": "pytest 文档", "url": "https://docs.pytest.org", "snippet": "x" * 300},
        "裸链接 https://example.com",
    ],
    "next_steps": "先装 pytest-asyncio 再跑测试",
    "risks": "依赖没装齐可能反复失败",
}


def digest_message(**overrides: Any) -> AIMessage:
    payload = dict(DIGEST)
    payload.update(overrides)
    return AIMessage(content=json.dumps(payload, ensure_ascii=False))


@pytest.fixture()
def runtime(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path / "ws")


@pytest.fixture()
def state(runtime: RuntimeState) -> dict[str, Any]:
    return {
        "task": "给 utils.py 加 add(a, b) 并写测试",
        "runtime": runtime,
        "plan_summary": "先实现 add，再补测试",
        "todos": [
            {"id": "t1", "content": "实现 add", "status": "completed", "note": ""},
            {"id": "t2", "content": "补测试", "status": "pending", "note": ""},
        ],
        "last_error": "验收命令失败：exit_code=1",
        "next_instruction": "补测试",
        "attempts": 1,
        "max_attempts": 3,
        "context_token_count": 12_345,
        "agent_handoffs": [
            {"from_agent": "planner", "to_agent": "codeAgent", "instruction": f"任务{i}", "result": "ok"}
            for i in range(9)
        ],
        "messages": [
            SystemMessage(content="system"),
            HumanMessage(content="给 utils.py 加 add"),
            AIMessage(content="先写文件"),
            HumanMessage(content="[tool] 已创建 utils.py"),
            AIMessage(content="写完了"),
        ],
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def test_compressor_replaces_messages_with_summary(state: dict[str, Any]) -> None:
    model = ScriptedModel([digest_message()])
    state["runtime"].metadata["model"] = model

    update = context_compressor_node(state)

    # 消息历史被清空并换成"摘要 + 新起点"
    assert len(update["messages"]) == 2
    assert isinstance(update["messages"][0], RemoveMessage)
    assert update["messages"][0].id == REMOVE_ALL_MESSAGES
    assert isinstance(update["messages"][1], AIMessage)
    assert update["messages"][1].content == DIGEST["summary"]

    # 压缩事件
    assert len(update["compression_events"]) == 1
    event = update["compression_events"][0]
    assert event["node"] == "context_compressor"
    assert event["tokens_before"] == 12_345
    assert event["tokens_after"] == update["context_token_count"]
    assert event["dropped_messages"] == 5
    assert event["used_llm"] is True
    assert event["history_path"] == HISTORY_SUMMARY_FILENAME

    # 刚压缩过，不需要再压
    assert update["context_should_compress"] is False
    assert update["history_summary"] == DIGEST["summary"]

    # 结构化摘要（9 个字段）都进了 context_summary
    for title in ("Active goal", "Completed work", "Open todos", "Important files",
                  "Tool findings", "Sources", "Next steps", "Risks"):
        assert f"## {title}" in update["context_summary"]

    # 检索要点与来源
    assert "pytest-asyncio" in update["research_notes"]
    assert update["sources"][0] == {"title": "pytest 文档", "url": "https://docs.pytest.org"}

    # 交接记录被裁到最近 6 条
    assert len(update["agent_handoffs"]) == 6

    # 落盘
    history = (state["runtime"].workspace / HISTORY_SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert "# HISTORY SUMMARY" in history
    assert "## Compression" in history
    assert "pytest-asyncio" in history

    # 压缩提示词用对了
    assert isinstance(model.calls[0][0], SystemMessage)
    assert model.calls[0][0].content == CONTEXT_COMPRESSION_PROMPT
    assert "Transcript to compress" in model.calls[0][1].content


def test_compressor_emits_events(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = ScriptedModel([digest_message()])
    events: list[dict[str, Any]] = []

    context_compressor_node(state, {"configurable": {"emit": events.append}})

    assert [event["type"] for event in events] == ["node_start", "context_compressed"]
    compressed = events[-1]
    assert compressed["dropped_messages"] == 5
    assert compressed["used_llm"] is True


# --------------------------------------------------------------------------- #
# 兜底
# --------------------------------------------------------------------------- #
def test_compressor_falls_back_when_model_raises(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = ScriptedModel([RuntimeError("模型挂了")])

    update = context_compressor_node(state)

    assert update["compression_events"][0]["used_llm"] is False
    # 兜底摘要来自状态本身
    assert "给 utils.py 加 add(a, b) 并写测试" in update["history_summary"]
    assert "验收命令失败" in update["history_summary"]
    assert len(update["messages"]) == 2  # 依然完成替换


def test_compressor_falls_back_when_json_invalid(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = ScriptedModel([AIMessage(content="抱歉，我不确定")])

    update = context_compressor_node(state)

    assert update["compression_events"][0]["used_llm"] is False
    assert update["context_should_compress"] is False
    assert "## Open todos" in update["context_summary"]  # 兜底也带待办


def test_compressor_falls_back_when_json_has_wrong_shape(state: dict[str, Any]) -> None:
    """回归：合法 JSON 但字段全不对（例如把 verifier 的结论丢过来）。

    早期版本会因此产出空摘要，并在落盘时抛 ValueError 把整张图打挂。
    """
    state["runtime"].metadata["model"] = ScriptedModel(
        [AIMessage(content=json.dumps({"passed": False, "reason": "还没有文件"}))]
    )

    update = context_compressor_node(state)

    assert update["compression_events"][0]["used_llm"] is False
    assert update["history_summary"]  # 兜底摘要非空
    assert update["compression_events"][0]["history_path"] == HISTORY_SUMMARY_FILENAME
    assert len(update["messages"]) == 2


def test_compressor_clips_long_fields(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = ScriptedModel(
        [
            digest_message(
                tool_findings="f" * 9000,
                sources=[{"title": f"t{i}", "url": f"https://e.com/{i}"} for i in range(50)],
            )
        ]
    )
    state["research_notes"] = "r" * 5000

    update = context_compressor_node(state)

    assert len(update["research_notes"]) <= COMPRESSED_NOTES_LIMIT
    assert len(update["sources"]) == MAX_COMPRESSED_SOURCES


def test_compression_events_are_capped(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = ScriptedModel([digest_message()])
    state["compression_events"] = [{"node": f"old{i}"} for i in range(MAX_COMPRESSION_EVENTS + 5)]

    update = context_compressor_node(state)

    assert len(update["compression_events"]) == MAX_COMPRESSION_EVENTS
    assert update["compression_events"][-1]["node"] == "context_compressor"


def test_compressor_handles_empty_messages(runtime: RuntimeState) -> None:
    runtime.metadata["model"] = ScriptedModel([digest_message()])

    update = context_compressor_node({"runtime": runtime})

    assert update["compression_events"][0]["dropped_messages"] == 0
    assert len(update["messages"]) == 2


# --------------------------------------------------------------------------- #
# 真 StateGraph：RemoveMessage 的语义
# --------------------------------------------------------------------------- #
def test_compressor_clears_history_inside_stategraph(state: dict[str, Any]) -> None:
    """在真图里验证 `RemoveMessage(REMOVE_ALL_MESSAGES)` 确实清空 add_messages 通道。"""
    state["runtime"].metadata["model"] = ScriptedModel([digest_message()])

    graph = StateGraph(MeoGraphState)
    graph.add_node("context_compressor", context_compressor_node)
    graph.add_edge(START, "context_compressor")
    graph.add_edge("context_compressor", END)
    compiled = graph.compile()

    result = compiled.invoke(state)

    # add_messages 会消费掉 RemoveMessage（它本身不留在通道里），只留下新的摘要消息
    assert len(result["messages"]) == 1
    assert result["messages"][0].content == DIGEST["summary"]
    assert "已创建 utils.py" not in result["messages"][0].content
    assert result["context_should_compress"] is False
    assert result["compression_events"][0]["tokens_before"] == 12_345
    # 压缩后 token 数重算过：只算新消息 + 记忆快照
    assert result["context_token_count"] < 12_345


# --------------------------------------------------------------------------- #
# 渲染与规范化
# --------------------------------------------------------------------------- #
def test_render_compression_digest_keeps_all_sections() -> None:
    text = _render_compression_digest(DIGEST)

    assert text.startswith(DIGEST["summary"])
    assert "## Active goal" in text
    assert "## Risks" in text
    assert "utils.py" in text
    assert "https://docs.pytest.org" in text


def test_render_compression_digest_skips_empty_fields() -> None:
    text = _render_compression_digest({"summary": "只有摘要"})

    assert text == "只有摘要"


def test_coerce_sources_variants() -> None:
    assert _coerce_sources([{"title": "a", "url": "u"}, "b", {"name": "c", "link": "l"}]) == [
        {"title": "a", "url": "u"},
        {"title": "b", "url": ""},
        {"title": "c", "url": "l"},
    ]
    assert _coerce_sources(None) == []
    assert _coerce_sources([]) == []
