"""core.react 测试：用脚本化的假模型跑底层 ReAct 循环，不联网、不需要 API Key。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from meoclaw.core.react import (
    ACTOR_PROMPT,
    execute_tool,
    message_text,
    serialize_tool_result,
    stream_react_events,
)
from meoclaw.tools.registry import TOOL_NAMES


class ScriptedModel:
    """按预设脚本返回 AIMessage 的假模型（鸭子类型，够 bind_tools/invoke 用）。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.received: list[list[BaseMessage]] = []
        self.bound_tools: list[Any] = []

    def bind_tools(self, tools: Any) -> "ScriptedModel":
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        self.received.append(list(messages))
        if not self._responses:
            raise AssertionError("脚本里的回应已经用完了")
        return self._responses.pop(0)


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def test_stream_react_events_tool_then_final_answer(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    model = ScriptedModel(
        [
            _tool_call("FileWriteTool", {"file_path": "hello.txt", "content": "hi\n"}),
            AIMessage(content="已完成，创建了 hello.txt"),
        ]
    )

    events = list(
        stream_react_events("创建 hello.txt", workspace=workspace, max_loops=5, model=model)
    )
    kinds = [event["type"] for event in events]

    assert kinds == ["ai_message", "tool_call", "tool_result", "ai_message", "final_answer"]

    call_event = events[1]
    assert call_event["name"] == "FileWriteTool"
    assert call_event["args"] == {"file_path": "hello.txt", "content": "hi\n"}

    result_event = events[2]
    assert result_event["ok"] is True
    assert "已创建" in result_event["result"]

    final_event = events[-1]
    assert final_event["content"] == "已完成，创建了 hello.txt"
    assert final_event["stopped_reason"] == "no_tool_calls"
    assert final_event["loops"] == 2

    # 工具真的在 workspace 里生效了（workspace 本身也会被自动创建）
    assert workspace.is_dir()
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hi\n"


def test_initial_messages_and_tool_binding(tmp_path: Path) -> None:
    model = ScriptedModel([AIMessage(content="什么都不做")])

    list(stream_react_events("看看情况", workspace=tmp_path / "ws", model=model))

    assert [tool.name for tool in model.bound_tools] == list(TOOL_NAMES)

    first_call = model.received[0]
    assert isinstance(first_call[0], SystemMessage)
    assert first_call[0].content == ACTOR_PROMPT
    assert isinstance(first_call[1], HumanMessage)
    assert first_call[1].content == "看看情况"


def test_tool_message_is_json_and_keeps_call_id(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            _tool_call("FileWriteTool", {"file_path": "a.txt", "content": "x"}, call_id="call_abc"),
            AIMessage(content="done"),
        ]
    )

    events = list(stream_react_events("写个文件", workspace=tmp_path / "ws", model=model))

    second_call_messages = model.received[1]
    tool_message = second_call_messages[-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_abc"
    # ToolMessage.content 是 JSON 串，反序列化后就是工具返回值本身
    assert json.loads(tool_message.content) == events[2]["result"]


def test_stops_at_max_loops(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            _tool_call("FileReadTool", {"file_path": "missing.txt"}, "c1"),
            _tool_call("FileReadTool", {"file_path": "missing.txt"}, "c2"),
            _tool_call("FileReadTool", {"file_path": "missing.txt"}, "c3"),
        ]
    )

    events = list(stream_react_events("读文件", workspace=tmp_path / "ws", max_loops=2, model=model))

    assert [event["type"] for event in events].count("ai_message") == 2
    assert events[-1]["type"] == "final_answer"
    assert events[-1]["stopped_reason"] == "max_loops"
    assert events[-1]["loops"] == 2
    assert len(model.received) == 2  # 第 3 条脚本没有被消费


def test_unknown_tool_does_not_break_the_loop(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            _tool_call("NopeTool", {"x": 1}, "c1"),
            AIMessage(content="我换个工具"),
        ]
    )

    events = list(stream_react_events("试试", workspace=tmp_path / "ws", model=model))
    result_event = next(event for event in events if event["type"] == "tool_result")

    assert result_event["ok"] is False
    assert "未知工具" in result_event["result"]
    assert events[-1]["type"] == "final_answer"
    assert events[-1]["content"] == "我换个工具"


def test_real_tool_failure_is_reported_as_ok_false(tmp_path: Path) -> None:
    """真实工具失败（这里是读不存在的文件）必须 ok=False，且循环继续。"""
    model = ScriptedModel(
        [
            _tool_call("FileReadTool", {"file_path": "missing.txt"}, "c1"),
            AIMessage(content="文件不存在，我换个思路"),
        ]
    )

    events = list(stream_react_events("读文件", workspace=tmp_path / "ws", model=model))
    result_event = next(event for event in events if event["type"] == "tool_result")

    assert result_event["ok"] is False
    assert "不存在" in result_event["result"]
    assert events[-1]["type"] == "final_answer"
    assert events[-1]["content"] == "文件不存在，我换个思路"


def test_multiple_tool_calls_in_one_turn(tmp_path: Path) -> None:
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "FileWriteTool", "args": {"file_path": "a.txt", "content": "A"}, "id": "c1", "type": "tool_call"},
            {"name": "FileWriteTool", "args": {"file_path": "b.txt", "content": "B"}, "id": "c2", "type": "tool_call"},
        ],
    )
    model = ScriptedModel([response, AIMessage(content="两个文件都写好了")])

    events = list(stream_react_events("写两个文件", workspace=tmp_path / "ws", model=model))

    assert [event["type"] for event in events] == [
        "ai_message",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "ai_message",
        "final_answer",
    ]
    assert [event["name"] for event in events if event["type"] == "tool_call"] == [
        "FileWriteTool",
        "FileWriteTool",
    ]
    # 每个 tool_call 都要有配对的 ToolMessage，否则下一轮请求会被服务端拒绝
    tool_messages = [m for m in model.received[1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert (tmp_path / "ws" / "a.txt").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "ws" / "b.txt").read_text(encoding="utf-8") == "B"


def test_blank_tool_call_id_is_backfilled(tmp_path: Path) -> None:
    """个别网关回空 id：合成的 id 必须同时回填进 AIMessage.tool_calls。"""
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "BashTool", "args": {"command": "echo hi"}, "id": "", "type": "tool_call"}
        ],
    )
    model = ScriptedModel([response, AIMessage(content="done")])

    events = list(stream_react_events("跑个命令", workspace=tmp_path / "ws", model=model))

    call_event = next(event for event in events if event["type"] == "tool_call")
    tool_message = model.received[1][-1]
    assert isinstance(tool_message, ToolMessage)
    assert call_event["id"] == tool_message.tool_call_id
    # AIMessage 里的 tool_calls 与 ToolMessage 严格配对
    assert response.tool_calls[0]["id"] == tool_message.tool_call_id


def test_execute_tool_wraps_exceptions() -> None:
    class BoomTool:
        name = "BoomTool"

        def invoke(self, args: dict[str, Any]) -> str:
            raise RuntimeError("炸了")

    ok, result = execute_tool({"BoomTool": BoomTool()}, {"name": "BoomTool", "args": {}})  # type: ignore[arg-type]
    assert ok is False
    assert "炸了" in result


@pytest.mark.parametrize("task", ["", "   ", None])
def test_blank_task_is_rejected(tmp_path: Path, task: Any) -> None:
    with pytest.raises(ValueError):
        list(stream_react_events(task, workspace=tmp_path / "ws", max_loops=3))
    # 参数校验发生在建工作区之前
    assert not (tmp_path / "ws").exists()


@pytest.mark.parametrize("max_loops", [0, -1, 2.5, True])
def test_bad_max_loops_is_rejected(tmp_path: Path, max_loops: Any) -> None:
    with pytest.raises(ValueError):
        list(stream_react_events("任务", workspace=tmp_path / "ws", max_loops=max_loops))
    assert not (tmp_path / "ws").exists()


def test_message_text_normalises_content() -> None:
    assert message_text("hi") == "hi"
    assert message_text(None) == ""
    assert message_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert message_text(["x", {"text": "y"}]) == "x\ny"


def test_serialize_tool_result_is_json() -> None:
    assert json.loads(serialize_tool_result("ok")) == "ok"
    assert json.loads(serialize_tool_result({"a": 1})) == {"a": 1}
    assert json.loads(serialize_tool_result(Path("x"))) == "x"
