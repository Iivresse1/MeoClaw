"""graph.workflow 测试：build_workflow 组装的图能离线跑完整回路。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from meoclaw.core.agent import stream_agent_events
from meoclaw.graph.workflow import (
    build_complex_workflow,
    build_workflow,
    initial_state,
)

PASS_COMMAND = f'"{sys.executable}" -c "print(1)"'


class Fake:
    """按脚本返回 AIMessage 的假模型（不联网、不需要 API Key）。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Any) -> Fake:
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("脚本里的回应已经用完了")
        return self._responses.pop(0)


def plan_call(summary: str = "写 hello.txt") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "TodoWriteTool",
                "args": {
                    "plan_summary": summary,
                    "todos": [
                        {"id": "t1", "content": "写入 hello.txt", "status": "pending", "note": ""}
                    ],
                    "acceptance_criteria": ["hello.txt 内容为 hello"],
                    "verification_commands": [PASS_COMMAND],
                },
                "id": "plan-1",
                "type": "tool_call",
            }
        ],
    )


def write_call(content: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "FileWriteTool",
                "args": {"file_path": "hello.txt", "content": content},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def verdict(passed: bool, reason: str, instruction: str = "") -> AIMessage:
    return AIMessage(
        content=json.dumps(
            {
                "passed": passed,
                "reason": reason,
                "checks": [{"name": "content", "passed": passed, "detail": reason}],
                "recommended_next_instruction": instruction,
            },
            ensure_ascii=False,
        )
    )


def test_initial_state_requires_task(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        initial_state("   ", workspace=tmp_path / "ws")


def test_initial_state_requires_workspace_or_runtime() -> None:
    with pytest.raises(ValueError):
        initial_state("做点什么")


def test_initial_state_creates_workspace_and_injects_model(tmp_path: Path) -> None:
    model = Fake([AIMessage(content="x")])

    state = initial_state("任务", workspace=tmp_path / "ws", model=model, max_attempts=2)

    assert state["runtime"].workspace.is_dir()
    assert state["runtime"].metadata["model"] is model
    assert state["max_attempts"] == 2
    assert state["attempts"] == 0
    assert state["passed"] is False


def test_build_workflow_retries_then_passes(tmp_path: Path) -> None:
    """第一轮内容写错被 verifier 打回，第二轮按 last_error 修好并收尾。"""
    model = Fake(
        [
            # attempt 1
            plan_call(),
            write_call("bad\n", "w1"),
            AIMessage(content="第一次写完了"),
            verdict(False, "内容应为 hello", "把内容改成 hello"),
            # attempt 2
            plan_call("修订：内容改成 hello"),
            write_call("hello\n", "w2"),
            AIMessage(content="第二次写完了"),
            verdict(True, "内容正确"),
            # final_node 的总结
            AIMessage(content="已完成：hello.txt 内容为 hello，验收命令通过。"),
        ]
    )
    runtime_state = initial_state("创建 hello.txt 并写 hello", workspace=tmp_path / "ws", model=model)

    events: list[dict[str, Any]] = []
    result = build_workflow().invoke(
        runtime_state, {"configurable": {"emit": events.append}}
    )

    assert result["passed"] is True
    assert result["attempts"] == 2
    assert (tmp_path / "ws" / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert all(todo["status"] == "completed" for todo in result["todos"])
    assert result["final_answer"] == "已完成：hello.txt 内容为 hello，验收命令通过。"

    # 第二轮 planner 拿到了第一轮的失败原因
    second_plan_brief = "\n".join(
        str(message.content)
        for call in model.calls
        for message in call
    )
    assert "failed verification" in second_plan_brief
    assert "内容应为 hello" in second_plan_brief

    # 事件流：actor/verifier 的实时事件 + 验收命令
    kinds = {event["type"] for event in events}
    assert {"ai_message", "tool_call", "tool_result", "verification_command"} <= kinds


def test_build_workflow_final_answer_is_deterministic_without_model(tmp_path: Path) -> None:
    """没注入模型也不给 Key 时，final_node 仍能用确定性文本收尾。"""
    model = Fake(
        [
            plan_call(),
            write_call("hello\n", "w1"),
            AIMessage(content="写完了"),
            verdict(True, "没问题"),
        ]
    )
    state = initial_state("创建 hello.txt", workspace=tmp_path / "ws", model=model)

    result = build_workflow().invoke(
        state, {"configurable": {"summarize_final": False, "emit": None}}
    )

    assert result["passed"] is True
    assert "已完成并通过验证" in result["final_answer"]
    assert "尝试 1/3 轮" in result["final_answer"]


def test_build_workflow_stops_after_max_attempts(tmp_path: Path) -> None:
    model = Fake(
        [
            plan_call(),
            AIMessage(content="做了一轮"),
            verdict(False, "还是不对", "继续改"),
            # final_node 的总结
            AIMessage(content="未完成：需要继续修复。"),
        ]
    )
    state = initial_state(
        "不可能完成的任务", workspace=tmp_path / "ws", model=model, max_attempts=1
    )

    result = build_workflow().invoke(state, {"configurable": {"emit": None}})

    assert result["passed"] is False
    assert result["attempts"] == 1
    assert result["final_answer"] == "未完成：需要继续修复。"
    assert "还是不对" in result["last_error"]


# --------------------------------------------------------------------------- #
# 复杂拓扑（build_complex_workflow：没有 actor 的 supervisor 图）
# --------------------------------------------------------------------------- #
def test_complex_workflow_has_no_actor_node() -> None:
    nodes = {node for node in build_complex_workflow().get_graph().nodes if not node.startswith("__")}
    assert nodes == {"planner", "verifier", "final"}
    assert "actor" not in nodes


def test_complex_workflow_passes_end_to_end(tmp_path: Path) -> None:
    model = Fake(
        [
            plan_call(),
            AIMessage(content="计划就绪，等待验证"),  # supervisor 收尾（没有更多工具调用）
            verdict(True, "工作区里已经有 hello.txt"),
            AIMessage(content="总结：任务已满足。"),
        ]
    )
    state = initial_state("创建 hello.txt", workspace=tmp_path / "ws", model=model)

    result = build_complex_workflow().invoke(state, {"configurable": {"emit": None}})

    assert result["passed"] is True
    assert result["attempts"] == 1
    assert result["final_answer"] == "总结：任务已满足。"


def test_complex_workflow_supervisor_delegates_to_code_agent(tmp_path: Path) -> None:
    """supervisor 拓扑端到端：planner 通过 CallCodeAgentTool 真正改文件并验收通过。"""
    model = Fake(
        [
            plan_call(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "CallCodeAgentTool",
                        "args": {"instruction": "写入 hello.txt，内容为 hello"},
                        "id": "d1",
                        "type": "tool_call",
                    }
                ],
            ),
            write_call("hello\n", "w1"),  # codeAgent 的 ReAct 轮
            AIMessage(content="已写入 hello.txt"),
            AIMessage(content="委派完成，等待验证"),
            verdict(True, "文件内容正确"),
            AIMessage(content="总结：已完成。"),
        ]
    )
    state = initial_state(
        "创建 hello.txt 并写 hello", workspace=tmp_path / "ws", model=model
    )

    events: list[dict[str, Any]] = []
    result = build_complex_workflow().invoke(state, {"configurable": {"emit": events.append}})

    assert result["passed"] is True
    assert result["attempts"] == 1
    assert (tmp_path / "ws" / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert result["code_agent_summary"] == "已写入 hello.txt"
    assert result["agent_handoffs"][-1]["to_agent"] == "codeAgent"
    kinds = {event["type"] for event in events}
    assert {"handoff", "handoff_result", "plan_snapshot"} <= kinds


def test_complex_workflow_terminates_when_attempts_exhausted(tmp_path: Path) -> None:
    """回归：试满 max_attempts 后由 verifier_route 收敛到 final。"""
    model = Fake(
        [
            plan_call(),
            AIMessage(content="第一轮安排完毕"),
            verdict(False, "还没做"),
            plan_call("再试一次"),
            AIMessage(content="第二轮安排完毕"),
            verdict(False, "还是没做"),
            AIMessage(content="总结：未完成。"),
        ]
    )
    state = initial_state(
        "创建 hello.txt", workspace=tmp_path / "ws", model=model, max_attempts=2
    )

    result = build_complex_workflow().invoke(state, {"configurable": {"emit": None}})

    assert result["passed"] is False
    assert result["attempts"] == 2  # 没有无限重试
    assert result["final_answer"] == "总结：未完成。"
    assert "还是没做" in result["last_error"]  # last_error 是最新一轮的失败


# 统一事件流（core.agent.stream_agent_events：驱动整张图）
# --------------------------------------------------------------------------- #
def test_stream_agent_events_yields_unified_events(tmp_path: Path) -> None:
    model = Fake(
        [
            plan_call(),
            write_call("hello\n", "w1"),
            AIMessage(content="写完了"),
            verdict(True, "没问题"),
            AIMessage(content="总结：已完成。"),
        ]
    )

    events = list(
        stream_agent_events(
            "创建 hello.txt", workspace=tmp_path / "ws", max_attempts=3, model=model
        )
    )
    kinds = [event["type"] for event in events]

    # 第一个事件是节点开始，最后一个一定是 workflow_end
    assert kinds[0] == "node_start"
    assert kinds[-1] == "workflow_end"

    # 四个节点按顺序各报一次开始（走的是 langgraph 的 custom 流）
    starts = [event["node"] for event in events if event["type"] == "node_start"]
    assert starts == ["planner", "actor", "verifier", "final"]

    # actor 的工具往返带着 node 标签
    tool_calls = [event for event in events if event["type"] == "tool_call"]
    assert [event["node"] for event in tool_calls] == ["actor"]
    assert tool_calls[0]["name"] == "FileWriteTool"

    # 各节点的产出事件
    plan = next(event for event in events if event["type"] == "plan")
    assert plan["node"] == "planner"
    assert len(plan["todos"]) == 1

    summary = next(event for event in events if event["type"] == "actor_summary")
    assert summary["node"] == "actor"
    assert summary["content"] == "写完了"
    assert [todo["id"] for todo in summary["todos"]] == ["t1"]

    verification = next(event for event in events if event["type"] == "verification")
    assert verification["passed"] is True
    assert verification["attempts"] == 1
    assert any(check["name"].startswith("command:") for check in verification["checks"])

    commands = [event for event in events if event["type"] == "verification_command"]
    assert [event["node"] for event in commands] == ["verifier"]
    assert commands[0]["ok"] is True

    final = next(event for event in events if event["type"] == "final_answer")
    assert final["node"] == "final"
    assert final["content"] == "总结：已完成。"

    # 节点内部 ReAct 的收尾被改名成 react_done，不会和 final 节点撞名
    react_done = [event for event in events if event["type"] == "react_done"]
    assert [event["node"] for event in react_done] == ["actor", "verifier"]

    end = events[-1]
    assert end["passed"] is True
    assert end["attempts"] == 1
    assert end["final_answer"] == "总结：已完成。"
    assert end["last_error"] == ""


def test_stream_agent_events_reports_failure(tmp_path: Path) -> None:
    model = Fake(
        [
            plan_call(),
            AIMessage(content="做了一轮"),
            verdict(False, "内容不对", "改成 hello"),
            AIMessage(content="未完成"),
        ]
    )

    events = list(
        stream_agent_events(
            "创建 hello.txt", workspace=tmp_path / "ws", max_attempts=1, model=model
        )
    )
    end = events[-1]

    assert end["type"] == "workflow_end"
    assert end["passed"] is False
    assert end["attempts"] == 1
    assert "内容不对" in end["last_error"]
    assert end["next_instruction"] == "改成 hello"


@pytest.mark.parametrize("task", ["", "   ", None])
def test_stream_agent_events_rejects_blank_task(tmp_path: Path, task: Any) -> None:
    with pytest.raises(ValueError):
        list(stream_agent_events(task, workspace=tmp_path / "ws", model=Fake([])))


@pytest.mark.parametrize("max_attempts", [0, -1, 1.5, True])
def test_stream_agent_events_rejects_bad_max_attempts(tmp_path: Path, max_attempts: Any) -> None:
    with pytest.raises(ValueError):
        list(
            stream_agent_events(
                "任务", workspace=tmp_path / "ws", max_attempts=max_attempts, model=Fake([])
            )
        )
