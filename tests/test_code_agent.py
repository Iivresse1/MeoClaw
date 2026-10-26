"""agents.code_agent 测试：离线跑 codeAgent，检查 todo/笔记/事件/持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from meoclaw.agents.code_agent import (
    CODE_AGENT_NAME,
    RULES_LAYER,
    build_code_agent_tools,
    build_memory_snapshot,
    compose_human_message,
    run_code_agent,
)
from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import append_history_summary
from meoclaw.prompts.stage3 import CODE_AGENT_PROMPT
from meoclaw.tools.notepad_tool import NOTEPAD_FILENAME, notepad_append, notepad_read
from meoclaw.tools.todo_tool import TODO_FILENAME, render_todos_markdown


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
            {"id": "t1", "content": "实现 add", "status": "pending", "note": ""},
            {"id": "t2", "content": "补测试", "status": "pending", "note": ""},
        ],
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def test_run_code_agent_implements_and_updates_todos(state: dict[str, Any]) -> None:
    model = Fake(
        [
            tool_call(
                "TodoUpdateTool",
                {"todo_id": "t1", "status": "in_progress", "note": ""},
                "c1",
            ),
            tool_call(
                "FileWriteTool",
                {"file_path": "utils.py", "content": "def add(a, b):\n    return a + b\n"},
                "c2",
            ),
            tool_call(
                "TodoUpdateTool",
                {"todo_id": "t1", "status": "completed", "note": "已实现 add"},
                "c3",
            ),
            AIMessage(content="已完成：新增 utils.py 的 add()；未跑测试。"),
        ]
    )
    events: list[dict[str, Any]] = []

    result = run_code_agent(state, "实现 add(a, b)", writer=events.append, model=model)

    assert result["ok"] is True
    assert result["agent"] == CODE_AGENT_NAME
    assert result["stopped_reason"] == "no_tool_calls"
    assert result["summary"] == "已完成：新增 utils.py 的 add()；未跑测试。"

    # 文件真的写进 workspace
    assert "def add(a, b)" in (state["runtime"].workspace / "utils.py").read_text(
        encoding="utf-8"
    )

    # TodoUpdateTool 的变更合并进 todos
    assert result["todos"][0]["status"] == "completed"
    assert result["todos"][0]["note"] == "已实现 add"
    assert result["todos"][1]["status"] == "pending"

    # TODO.md 持久化
    todo_md = (state["runtime"].workspace / TODO_FILENAME).read_text(encoding="utf-8")
    assert "- [x] t1: 实现 add" in todo_md
    assert "- [ ] t2: 补测试" in todo_md
    assert result["todo_file"] == TODO_FILENAME

    # 三层记忆快照随结果返回（调用方可存进 state["memory_snapshot"]）
    assert result["memory_snapshot"]["working_memory"]["node"] == CODE_AGENT_NAME

    # messages：3 轮工具往返（各 1 AI + 1 ToolMessage）+ 1 条收尾 AI
    assert [type(message).__name__ for message in result["messages"]] == [
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert all(event["agent"] == CODE_AGENT_NAME for event in result["tool_events"])
    assert [event["type"] for event in result["tool_events"]][:3] == [
        "ai_message",
        "tool_call",
        "tool_result",
    ]
    assert result["tool_events"] == events  # writer 收到同样的事件

    # 系统提示词与工具集
    assert isinstance(model.calls[0][0], SystemMessage)
    assert model.calls[0][0].content == CODE_AGENT_PROMPT
    assert model.bound_tool_names == [
        "FileReadTool",
        "FileWriteTool",
        "FileEditTool",
        "GrepTool",
        "BashTool",
        "TodoUpdateTool",
        "NotepadReadTool",
        "NotepadAppendTool",
    ]


def test_human_message_contains_task_instruction_context_and_memory(
    state: dict[str, Any],
) -> None:
    state["last_error"] = "上一轮测试没过"
    state["next_instruction"] = "先修 add 的边界情况"
    notepad_append(state["runtime"], "决策", "用纯函数实现，不引入依赖")

    message = compose_human_message(state, "实现 add(a, b)")

    assert "## Task" in message
    assert "给 utils.py 加 add(a, b) 并写测试" in message
    assert "## Instruction from planner" in message
    assert "实现 add(a, b)" in message
    assert "## Session context" in message
    assert "- Todo t1 [pending]: 实现 add" in message
    assert "上一轮测试没过" in message
    assert "先修 add 的边界情况" in message
    # memory 快照是三层记忆的 JSON
    assert "## Memory snapshot" in message
    assert '"rules"' in message
    assert '"working_memory"' in message
    assert '"history_summary_store"' in message
    assert "用纯函数实现，不引入依赖" in message


def test_agent_can_write_and_read_notepad(state: dict[str, Any]) -> None:
    model = Fake(
        [
            tool_call(
                "NotepadAppendTool",
                {"heading": "阻塞点", "content": "缺少 pytest-asyncio"},
                "n1",
            ),
            tool_call("NotepadReadTool", {"limit": 1000}, "n2"),
            AIMessage(content="已记录阻塞点"),
        ]
    )

    result = run_code_agent(state, "记录一下阻塞点", model=model)

    notepad = (state["runtime"].workspace / NOTEPAD_FILENAME).read_text(encoding="utf-8")
    assert "## 阻塞点" in notepad
    assert "缺少 pytest-asyncio" in notepad

    read_result = next(
        event
        for event in result["tool_events"]
        if event["type"] == "tool_result" and event["name"] == "NotepadReadTool"
    )
    assert "缺少 pytest-asyncio" in read_result["result"]


def test_memory_snapshot_uses_three_layers(runtime: RuntimeState, state: dict[str, Any]) -> None:
    """codeAgent 的记忆快照直接复用 graph/memory.py 的三层实现（只有一处实现）。"""
    snapshot = json.loads(build_memory_snapshot(state))

    assert snapshot["rules"] == RULES_LAYER
    assert snapshot["working_memory"]["node"] == "codeAgent"
    store = snapshot["history_summary_store"]
    assert store["notepad_exists"] is False
    assert store["history_exists"] is False

    notepad_append(runtime, "重要文件", "utils.py")
    append_history_summary(runtime, "上一轮的历史结论")
    snapshot = json.loads(build_memory_snapshot(state))
    store = snapshot["history_summary_store"]

    assert store["notepad_exists"] is True
    assert "utils.py" in store["notepad"]
    assert store["history_exists"] is True
    assert "上一轮的历史结论" in store["history_summary"]


def test_run_code_agent_accepts_runtime_state_directly(runtime: RuntimeState) -> None:
    model = Fake([AIMessage(content="没有待办，直接说明")])

    result = run_code_agent(runtime, "看看情况", model=model)

    assert result["ok"] is True
    assert result["todos"] == []
    assert result["todo_file"] is None
    assert "## Role instruction" in "\n".join(
        str(message.content) for message in model.calls[0]
    )


def test_run_code_agent_reports_max_loops(state: dict[str, Any]) -> None:
    model = Fake(
        [
            tool_call("FileReadTool", {"file_path": "missing.txt"}, f"c{i}")
            for i in range(3)
        ]
    )

    result = run_code_agent(state, "读文件", max_loops=3, model=model)

    assert result["ok"] is False
    assert result["stopped_reason"] == "max_loops"
    assert result["loops"] == 3
    assert len(model.calls) == 3


def test_run_code_agent_uses_model_from_runtime_metadata(state: dict[str, Any]) -> None:
    model = Fake([AIMessage(content="来自 metadata 的模型")])
    state["runtime"].metadata["model"] = model

    result = run_code_agent(state, "随便做点")

    assert result["summary"] == "来自 metadata 的模型"


@pytest.mark.parametrize("instruction", ["", "   ", None])
def test_run_code_agent_rejects_blank_instruction(
    state: dict[str, Any], instruction: Any
) -> None:
    with pytest.raises(ValueError):
        run_code_agent(state, instruction, model=Fake([]))


@pytest.mark.parametrize("max_loops", [0, -1, 2.5, True])
def test_run_code_agent_rejects_bad_max_loops(
    state: dict[str, Any], max_loops: Any
) -> None:
    with pytest.raises(ValueError):
        run_code_agent(state, "任务", max_loops=max_loops, model=Fake([]))


# --------------------------------------------------------------------------- #
# 工具与笔记的最小可用性
# --------------------------------------------------------------------------- #
def test_build_code_agent_tools_names(runtime: RuntimeState) -> None:
    names = [tool.name for tool in build_code_agent_tools(runtime)]
    assert names == [
        "FileReadTool",
        "FileWriteTool",
        "FileEditTool",
        "GrepTool",
        "BashTool",
        "TodoUpdateTool",
        "NotepadReadTool",
        "NotepadAppendTool",
    ]


def test_notepad_read_without_file(runtime: RuntimeState) -> None:
    assert "还没有内容" in notepad_read(runtime)


def test_render_todos_markdown_marks_statuses() -> None:
    text = render_todos_markdown(
        [
            {"id": "t1", "content": "做事", "status": "completed", "note": "ok"},
            {"id": "t2", "content": "卡住", "status": "blocked", "note": "缺依赖"},
            {"id": "t3", "content": "进行中", "status": "in_progress", "note": ""},
        ],
        plan_summary="计划摘要",
    )
    assert text.startswith("# TODO")
    assert "计划摘要" in text
    assert "- [x] t1: 做事  (completed) — ok" in text
    assert "- [!] t2: 卡住  (blocked) — 缺依赖" in text
    assert "- [~] t3: 进行中  (in_progress)" in text


def test_render_todos_markdown_empty() -> None:
    assert "(暂无待办)" in render_todos_markdown([])
