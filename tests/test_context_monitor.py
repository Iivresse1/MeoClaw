"""graph.nodes 上下文监控测试：token 估算、压缩判定与路由。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from meoclaw.core.state import RuntimeState
from meoclaw.graph.nodes import (
    CONTEXT_COMPRESSOR_NODE,
    DEFAULT_CONTEXT_TOKEN_LIMIT,
    DEFAULT_NEXT_NODE,
    FINAL,
    context_monitor_node,
    context_monitor_route,
    estimate_context_tokens,
)
from meoclaw.graph.state import MeoGraphState


class FakeTokenizer:
    """只实现 token 计数所需接口的假模型。"""

    def __init__(self, tokens: int | None = 1234) -> None:
        self.tokens = tokens
        self.seen: list[list[BaseMessage]] = []

    def get_num_tokens_from_messages(self, messages: list[BaseMessage], **_: Any) -> int:
        self.seen.append(list(messages))
        if self.tokens is None:
            raise RuntimeError("这个模型不支持分词")
        return self.tokens

    def bind_tools(self, tools: Any) -> "FakeTokenizer":
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        return AIMessage(content="")


@pytest.fixture()
def runtime(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path / "ws")


@pytest.fixture()
def state(runtime: RuntimeState) -> dict[str, Any]:
    return {
        "task": "给 utils.py 加 add(a, b)",
        "runtime": runtime,
        "messages": [HumanMessage(content="你好"), AIMessage(content="在的")],
    }


# --------------------------------------------------------------------------- #
# token 估算
# --------------------------------------------------------------------------- #
def test_estimate_tokens_uses_model_tokenizer(state: dict[str, Any]) -> None:
    model = FakeTokenizer(tokens=4321)
    state["runtime"].metadata["model"] = model

    payload = HumanMessage(content="[layered memory] ...")
    assert estimate_context_tokens(state, None, memory_payload=payload) == 4321

    # 记忆快照也被算进去了
    assert model.seen[0][-1] is payload
    assert len(model.seen[0]) == len(state["messages"]) + 1


def test_estimate_tokens_falls_back_when_model_fails(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = FakeTokenizer(tokens=None)

    # 消息内容："你好"(2) + "在的"(2) + 记忆 payload
    payload = HumanMessage(content="abcd" * 3)
    expected = (len("你好") + len("在的") + len("abcd" * 3)) // 4

    assert estimate_context_tokens(state, None, memory_payload=payload) == expected


def test_estimate_tokens_falls_back_without_model(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("no api key")

    monkeypatch.setattr("meoclaw.core.context.create_model", boom)

    assert estimate_context_tokens(state) == len("你好\n在的") // 4


def test_estimate_tokens_without_messages(runtime: RuntimeState) -> None:
    state = {"runtime": runtime}
    assert estimate_context_tokens(state) == 0


# --------------------------------------------------------------------------- #
# context_monitor_node
# --------------------------------------------------------------------------- #
def test_context_monitor_node_counts_and_flags(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = FakeTokenizer(tokens=150)
    state["context_token_limit"] = 100

    update = context_monitor_node(state)

    assert update["context_token_count"] == 150
    assert update["context_token_limit"] == 100
    assert update["context_should_compress"] is True
    assert update["context_next_node"] == DEFAULT_NEXT_NODE == "verifier"
    # 顺带写记忆快照（运行时组装记忆的落点）
    assert set(update["memory_snapshot"]) == {
        "rules",
        "working_memory",
        "history_summary_store",
    }
    assert update["memory_snapshot"]["working_memory"]["node"] == "context_monitor"


def test_context_monitor_node_no_compress_under_limit(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = FakeTokenizer(tokens=99)
    state["context_token_limit"] = 100

    update = context_monitor_node(state)

    assert update["context_should_compress"] is False


def test_context_monitor_node_uses_default_limit(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = FakeTokenizer(tokens=10)

    update = context_monitor_node(state)

    assert update["context_token_limit"] == DEFAULT_CONTEXT_TOKEN_LIMIT == 400_000
    assert update["context_should_compress"] is False


def test_context_monitor_node_keeps_upstream_next_node(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = FakeTokenizer(tokens=10)
    state["context_next_node"] = "planner"

    assert context_monitor_node(state)["context_next_node"] == "planner"


def test_context_monitor_node_emits_event(state: dict[str, Any]) -> None:
    state["runtime"].metadata["model"] = FakeTokenizer(tokens=500)
    state["context_token_limit"] = 100
    events: list[dict[str, Any]] = []

    context_monitor_node(state, {"configurable": {"emit": events.append}})

    assert [event["type"] for event in events] == ["node_start", "context_usage"]
    usage = events[-1]
    assert usage["node"] == "context_monitor"
    assert usage["token_count"] == 500
    assert usage["should_compress"] is True
    assert usage["next_node"] == "verifier"


def test_context_monitor_node_inside_stategraph(state: dict[str, Any]) -> None:
    """真的放进 StateGraph 跑一遍：验证 state 增量合法（新字段都有 channel）。"""
    state["runtime"].metadata["model"] = FakeTokenizer(tokens=42)

    graph = StateGraph(MeoGraphState)
    graph.add_node("context_monitor", context_monitor_node)
    graph.add_edge(START, "context_monitor")
    graph.add_edge("context_monitor", END)
    compiled = graph.compile()

    result = compiled.invoke(state)

    assert result["context_token_count"] == 42
    assert result["context_should_compress"] is False
    assert result["context_next_node"] == "verifier"
    assert result["memory_snapshot"]["working_memory"]["node"] == "context_monitor"


# --------------------------------------------------------------------------- #
# context_monitor_route
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"passed": True}, FINAL),
        # 已通过优先于"该压缩"
        ({"passed": True, "context_should_compress": True}, FINAL),
        ({"context_should_compress": True}, CONTEXT_COMPRESSOR_NODE),
        ({"context_should_compress": False, "context_next_node": "planner"}, "planner"),
        ({"context_should_compress": False, "context_next_node": "verifier"}, "verifier"),
        # 上游没设置就用默认
        ({}, DEFAULT_NEXT_NODE),
        ({"context_next_node": ""}, DEFAULT_NEXT_NODE),
    ],
)
def test_context_monitor_route(state: dict[str, Any], expected: str) -> None:
    assert context_monitor_route(state) == expected
