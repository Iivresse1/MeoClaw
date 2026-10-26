"""graph.state 测试：字段齐备 + messages 走 add_messages 合并语义。"""

from __future__ import annotations

from typing import get_args, get_origin, get_type_hints

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

from meoclaw.core.state import RuntimeState
from meoclaw.graph.state import MeoGraphState, TodoItem, VerificationResult


def test_messages_field_uses_add_messages_reducer() -> None:
    hints = get_type_hints(MeoGraphState, include_extras=True)
    args = get_args(hints["messages"])

    assert get_origin(args[0]) is list
    assert get_args(args[0])[0] is BaseMessage
    assert args[1] is add_messages


def test_add_messages_merges_instead_of_overwriting() -> None:
    reducer = get_args(get_type_hints(MeoGraphState, include_extras=True)["messages"])[1]
    merged = reducer([HumanMessage(content="你好")], [AIMessage(content="在的")])
    assert [type(message).__name__ for message in merged] == ["HumanMessage", "AIMessage"]


def test_state_is_total_false() -> None:
    assert MeoGraphState.__total__ is False
    assert MeoGraphState.__optional_keys__ == frozenset(
        {
            "task",
            "runtime",
            "messages",
            "plan_summary",
            "todos",
            "acceptance_criteria",
            "verification_commands",
            "verification_results",
            "verification_checks",
            "passed",
            "attempts",
            "max_attempts",
            "last_actor_summary",
            "last_error",
            "next_instruction",
            "final_answer",
            # 检索 / 专家 Agent
            "research_notes",
            "sources",
            "agent_handoffs",
            "code_agent_summary",
            "verifier_summary",
        }
    )


def test_memory_related_types_exist() -> None:
    """三层记忆用到的结构化类型都能导入。"""
    from meoclaw.graph.state import AgentHandoff, SourceItem

    assert set(AgentHandoff.__optional_keys__) == {
        "from_agent",
        "to_agent",
        "instruction",
        "result",
    }
    assert set(SourceItem.__optional_keys__) == {"title", "url", "snippet"}


def test_todo_item_and_verification_result_keys() -> None:
    todo: TodoItem = {"id": "1", "content": "写代码", "status": "pending", "note": ""}
    result: VerificationResult = {
        "command": "python -m pytest",
        "ok": False,
        "exit_code": 1,
        "stdout": "",
        "stderr": "1 failed",
    }
    assert set(todo) == {"id", "content", "status", "note"}
    assert set(result) == {"command", "ok", "exit_code", "stdout", "stderr"}


def test_state_accepts_partial_payload() -> None:
    """total=False：节点只写自己关心的字段。"""
    state: MeoGraphState = {"task": "做个加法", "attempts": 1}
    assert state["task"] == "做个加法"


def test_runtime_field_holds_runtime_state(tmp_path) -> None:
    runtime = RuntimeState(workspace=tmp_path / "ws")
    state: MeoGraphState = {"runtime": runtime}
    assert state["runtime"].workspace.is_dir()
