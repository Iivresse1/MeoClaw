"""graph.nodes 测试：用脚本化假模型离线跑 planner / actor / verifier / 路由。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import HISTORY_SUMMARY_FILENAME
from meoclaw.graph.nodes import (
    CALL_CODE_AGENT_TOOL,
    CALL_SEARCH_AGENT_TOOL,
    DEFAULT_MAX_ATTEMPTS,
    FINAL,
    MAX_ACTOR_LOOPS,
    PlannerError,
    actor_node,
    extract_json_object,
    extract_plan,
    final_node,
    parse_verdict,
    planner_node,
    resolve_emit,
    resolve_model,
    run_verification_commands,
    supervisor_node,
    verifier_node,
    verifier_route,
)
from meoclaw.graph.state import MeoGraphState
from meoclaw.prompts import (
    FINAL_PROMPT,
    PLANNER_PROMPT,
    SUPERVISOR_PROMPT,
    VERIFIER_PROMPT,
)
from meoclaw.tools.todo_tool import TodoWriteTool

PASS_COMMAND = f'"{sys.executable}" -c "print(1)"'
FAIL_COMMAND = f'"{sys.executable}" -c "import sys; sys.exit(2)"'


class ScriptedModel:
    """按脚本返回 AIMessage 的假模型；记录每次绑定的工具与收到的消息。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.calls: list[list[BaseMessage]] = []
        self.bound_tool_names: list[list[str]] = []

    def bind_tools(self, tools: Any) -> ScriptedModel:
        self.bound_tool_names.append([tool.name for tool in tools])
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("脚本里的回应已经用完了")
        return self._responses.pop(0)


def _plan_call(**overrides: Any) -> AIMessage:
    args: dict[str, Any] = {
        "plan_summary": "创建 hello.txt 并写一行内容",
        "todos": [
            {"id": "t1", "content": "写入 hello.txt", "status": "pending", "note": ""},
            {"id": "t2", "content": "确认文件内容", "status": "pending", "note": ""},
        ],
        "acceptance_criteria": ["hello.txt 存在且内容为 hello"],
        "verification_commands": [PASS_COMMAND],
    }
    args.update(overrides)
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "TodoWriteTool", "args": args, "id": "plan-1", "type": "tool_call"}
        ],
    )


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _verdict(passed: bool, **overrides: Any) -> AIMessage:
    payload = {
        "passed": passed,
        "reason": "看起来完成了" if passed else "文件内容不对",
        "checks": [{"name": "file content", "passed": passed, "detail": "checked"}],
        "recommended_next_instruction": "" if passed else "修正 hello.txt 的内容",
    }
    payload.update(overrides)
    return AIMessage(content=json.dumps(payload, ensure_ascii=False))


@pytest.fixture()
def runtime(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path / "ws")


@pytest.fixture()
def state(runtime: RuntimeState) -> dict[str, Any]:
    return {"task": "创建 hello.txt 并写一行 hello", "runtime": runtime}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"passed": true}', {"passed": True}),
        ('```json\n{"passed": true}\n```', {"passed": True}),
        ('好的，结论如下：\n{"passed": true}\n以上。', {"passed": True}),
        (
            '{"passed": true, "reason": "含 } 与 { 的字符串"}',
            {"passed": True, "reason": "含 } 与 { 的字符串"},
        ),
    ],
)
def test_extract_json_object(text: str, expected: dict[str, Any]) -> None:
    assert extract_json_object(text) == expected


def test_extract_json_object_returns_none() -> None:
    assert extract_json_object("没有 JSON") is None
    assert extract_json_object("") is None


def test_resolve_model_prefers_config_then_metadata(runtime: RuntimeState) -> None:
    from_config = ScriptedModel([])
    from_metadata = ScriptedModel([])
    runtime.metadata["model"] = from_metadata

    state = {"runtime": runtime}
    assert resolve_model(state, {"configurable": {"model": from_config}}) is from_config
    assert resolve_model(state) is from_metadata


def test_resolve_emit() -> None:
    calls: list[dict[str, Any]] = []
    emit = calls.append
    assert resolve_emit(None) is None
    assert resolve_emit({"configurable": {}}) is None
    assert resolve_emit({"configurable": {"emit": "not-callable"}}) is None
    assert resolve_emit({"configurable": {"emit": emit}}) is emit


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
def test_planner_node_uses_todo_write_tool(state: dict[str, Any]) -> None:
    model = ScriptedModel([_plan_call()])
    state["runtime"].metadata["model"] = model

    update = planner_node(state)

    assert model.bound_tool_names == [[TodoWriteTool.name]]
    assert isinstance(model.calls[0][0], SystemMessage)
    assert model.calls[0][0].content == PLANNER_PROMPT

    assert update["plan_summary"] == "创建 hello.txt 并写一行内容"
    assert [todo["id"] for todo in update["todos"]] == ["t1", "t2"]
    assert all(todo["status"] == "pending" for todo in update["todos"])
    assert update["acceptance_criteria"] == ["hello.txt 存在且内容为 hello"]
    assert update["verification_commands"] == [PASS_COMMAND]
    assert update["passed"] is False
    assert update["max_attempts"] == DEFAULT_MAX_ATTEMPTS


def test_planner_node_revises_with_last_error(state: dict[str, Any]) -> None:
    state.update(
        {
            "todos": [{"id": "t1", "content": "旧待办", "status": "in_progress", "note": ""}],
            "last_error": "验收命令失败：exit_code=2",
            "next_instruction": "修正 hello.txt",
            "attempts": 1,
        }
    )
    model = ScriptedModel([_plan_call(plan_summary="修订后的计划")])
    state["runtime"].metadata["model"] = model

    update = planner_node(state)

    brief = "\n".join(str(message.content) for message in model.calls[0])
    assert "failed verification" in brief
    assert "验收命令失败" in brief
    assert "修正 hello.txt" in brief
    assert update["plan_summary"] == "修订后的计划"


def test_planner_node_falls_back_to_json_content(state: dict[str, Any]) -> None:
    payload = {
        "plan_summary": "直接给 JSON",
        "todos": [{"id": "t1", "content": "做事"}],
        "acceptance_criteria": [],
        "verification_commands": [],
    }
    model = ScriptedModel([AIMessage(content=f"```json\n{json.dumps(payload)}\n```")])
    state["runtime"].metadata["model"] = model

    update = planner_node(state)

    assert update["plan_summary"] == "直接给 JSON"
    assert update["todos"][0]["content"] == "做事"


def test_planner_node_raises_without_plan(state: dict[str, Any]) -> None:
    model = ScriptedModel([AIMessage(content="抱歉，我不知道怎么做")])
    state["runtime"].metadata["model"] = model

    with pytest.raises(PlannerError):
        planner_node(state)


def test_planner_node_falls_back_to_single_todo(state: dict[str, Any]) -> None:
    """模型给了空 todos 时，至少把任务本身当成一条待办。"""
    model = ScriptedModel([_plan_call(todos=[])])
    state["runtime"].metadata["model"] = model

    update = planner_node(state)

    assert len(update["todos"]) == 1
    assert update["todos"][0]["content"] == state["task"]


def test_extract_plan_prefers_tool_call() -> None:
    response = _plan_call(plan_summary="来自工具调用")
    assert extract_plan(response)["plan_summary"] == "来自工具调用"


# --------------------------------------------------------------------------- #
# actor
# --------------------------------------------------------------------------- #
def test_actor_node_runs_tools_and_updates_todos(state: dict[str, Any]) -> None:
    state.update(
        {
            "plan_summary": "写文件",
            "todos": [{"id": "t1", "content": "写 hello.txt", "status": "pending", "note": ""}],
        }
    )
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "FileWriteTool",
                "args": {"file_path": "hello.txt", "content": "hello\n"},
                "id": "c1",
                "type": "tool_call",
            },
            {
                "name": "TodoUpdateTool",
                "args": {"todo_id": "t1", "status": "completed", "note": "已写入"},
                "id": "c2",
                "type": "tool_call",
            },
        ],
    )
    model = ScriptedModel([response, AIMessage(content="已完成：写入 hello.txt")])
    state["runtime"].metadata["model"] = model

    events: list[dict[str, Any]] = []
    update = actor_node(state, {"configurable": {"emit": events.append}})

    # 工具真的生效
    assert (state["runtime"].workspace / "hello.txt").read_text(encoding="utf-8") == "hello\n"

    # messages 增量：两轮 AI 回应 + 两条 ToolMessage（message_sink 记录全部消息）
    assert [type(message).__name__ for message in update["messages"]] == [
        "AIMessage",
        "ToolMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert isinstance(update["messages"][1], ToolMessage)
    assert {message.tool_call_id for message in update["messages"][1:3]} == {"c1", "c2"}
    assert update["messages"][-1].content == "已完成：写入 hello.txt"

    # TodoUpdateTool 的变更被合并回 todos
    assert update["todos"][0]["status"] == "completed"
    assert update["todos"][0]["note"] == "已写入"
    assert update["last_actor_summary"] == "已完成：写入 hello.txt"

    # 事件被转发给 emit（第一个是 node_start；ReAct 收尾事件会被改名成 react_done）
    kinds = [event["type"] for event in events]
    assert kinds[0] == "node_start"
    assert events[0]["node"] == "actor"
    assert "ai_message" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "react_done"
    assert all(event.get("node") == "actor" for event in events)


def test_actor_node_binds_todo_update_tool(state: dict[str, Any]) -> None:
    model = ScriptedModel([AIMessage(content="什么都不做")])
    state["runtime"].metadata["model"] = model

    actor_node(state)

    assert model.bound_tool_names[0] == [
        "FileReadTool",
        "FileWriteTool",
        "FileEditTool",
        "GrepTool",
        "BashTool",
        "WebSearchTool",
        "WebFetchTool",
        "TodoUpdateTool",
    ]


def test_actor_node_marks_max_loops(state: dict[str, Any]) -> None:
    model = ScriptedModel(
        [_tool_call("FileReadTool", {"file_path": "missing.txt"}, f"c{i}") for i in range(MAX_ACTOR_LOOPS)]
    )
    state["runtime"].metadata["model"] = model

    update = actor_node(state)

    assert "达到最大轮数" in update["last_actor_summary"]
    assert len(model.calls) == MAX_ACTOR_LOOPS


# --------------------------------------------------------------------------- #
# verifier
# --------------------------------------------------------------------------- #
def _verifier_state(state: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    state.update(
        {
            "plan_summary": "写文件",
            "todos": [{"id": "t1", "content": "写 hello.txt", "status": "in_progress", "note": ""}],
            "acceptance_criteria": ["hello.txt 存在"],
            "verification_commands": [PASS_COMMAND],
            "last_actor_summary": "已写入 hello.txt",
        }
    )
    state.update(overrides)
    return state


def test_build_read_only_tools_excludes_writers() -> None:
    """只读集合必须排除一切会改工作区的工具（含 NotepadAppendTool）。"""
    from meoclaw.tools.registry import READ_ONLY_TOOL_NAMES

    names = list(READ_ONLY_TOOL_NAMES)
    assert names == [
        "FileReadTool",
        "GrepTool",
        "NotepadReadTool",
        "WebSearchTool",
        "WebFetchTool",
    ]
    for forbidden in (
        "FileWriteTool",
        "FileEditTool",
        "BashTool",
        "NotepadAppendTool",
        "TodoUpdateTool",
    ):
        assert forbidden not in names


def test_verifier_prompt_lives_in_stage3() -> None:
    """VERIFIER_PROMPT 只在 stage3 定义一处，避免两份提示词漂移。"""
    from meoclaw.prompts import stage2, stage3

    assert stage3.VERIFIER_PROMPT == VERIFIER_PROMPT
    assert not hasattr(stage2, "VERIFIER_PROMPT")
    assert "NotepadReadTool" in VERIFIER_PROMPT


def test_verifier_node_passes(state: dict[str, Any]) -> None:
    _verifier_state(state)
    model = ScriptedModel([_verdict(True)])
    state["runtime"].metadata["model"] = model

    update = verifier_node(state)

    assert update["passed"] is True
    assert update["attempts"] == 1
    assert update["last_error"] == ""
    assert update["next_instruction"] == ""
    assert update["todos"][0]["status"] == "completed"
    assert [result["ok"] for result in update["verification_results"]] == [True]
    # 只读工具：能查证，但改不了东西
    assert model.bound_tool_names == [
        [
            "FileReadTool",
            "GrepTool",
            "NotepadReadTool",
            "WebSearchTool",
            "WebFetchTool",
        ]
    ]
    assert model.calls[0][0].content == VERIFIER_PROMPT


def test_verifier_node_command_failure_overrides_optimistic_verdict(
    state: dict[str, Any],
) -> None:
    """LLM 说通过，但验收命令失败 —— fail-closed，判为未通过。"""
    _verifier_state(state, verification_commands=[FAIL_COMMAND])
    model = ScriptedModel([_verdict(True)])
    state["runtime"].metadata["model"] = model

    update = verifier_node(state)

    assert update["passed"] is False
    assert "exit_code=2" in update["last_error"]
    assert any(
        check["name"].startswith("command:") and check["passed"] is False
        for check in update["verification_checks"]
    )


def test_verifier_node_failed_verdict_sets_last_error(state: dict[str, Any]) -> None:
    _verifier_state(state, attempts=1)
    model = ScriptedModel([_verdict(False)])
    state["runtime"].metadata["model"] = model

    update = verifier_node(state)

    assert update["passed"] is False
    assert update["attempts"] == 2
    assert "文件内容不对" in update["last_error"]
    assert "修正 hello.txt 的内容" in update["last_error"]
    assert update["next_instruction"] == "修正 hello.txt 的内容"


def test_verifier_node_unparseable_output_fails_closed(state: dict[str, Any]) -> None:
    _verifier_state(state)
    model = ScriptedModel([AIMessage(content="我觉得挺好的，应该没问题。")])
    state["runtime"].metadata["model"] = model

    update = verifier_node(state)

    assert update["passed"] is False
    assert "JSON" in update["last_error"]


def test_parse_verdict_normalises_checks() -> None:
    verdict = parse_verdict('{"passed": true, "checks": [{"name": "a", "passed": false}]}')

    assert verdict["passed"] is True
    assert verdict["checks"] == [{"name": "a", "passed": False, "detail": ""}]


def test_run_verification_commands_collects_results(runtime: RuntimeState) -> None:
    events: list[dict[str, Any]] = []
    results = run_verification_commands(
        runtime, [PASS_COMMAND, FAIL_COMMAND, "  "], emit=events.append
    )

    assert [result["ok"] for result in results] == [True, False]  # 空命令被跳过
    assert [result["exit_code"] for result in results] == [0, 2]
    assert [event["type"] for event in events] == [
        "verification_command",
        "verification_command",
    ]


def test_run_verification_commands_fails_closed_without_docker(
    runtime: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.metadata["sandbox"] = "docker"
    monkeypatch.setattr("meoclaw.core.sandbox.shutil.which", lambda _: None)

    results = run_verification_commands(runtime, [PASS_COMMAND])

    assert results[0]["ok"] is False
    assert results[0]["exit_code"] is None
    assert "Docker 未安装" in results[0]["stderr"]


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"passed": True}, FINAL),
        ({"passed": True, "attempts": 1, "max_attempts": 3}, FINAL),
        ({"passed": False, "attempts": 1, "max_attempts": 3}, "planner"),
        ({"passed": False, "attempts": 2, "max_attempts": 3}, "planner"),
        ({"passed": False, "attempts": 3, "max_attempts": 3}, FINAL),
        ({"passed": False, "attempts": 5, "max_attempts": 3}, FINAL),
        # 缺 max_attempts 时用默认值
        ({"passed": False, "attempts": DEFAULT_MAX_ATTEMPTS - 1}, "planner"),
        ({"passed": False, "attempts": DEFAULT_MAX_ATTEMPTS}, FINAL),
    ],
)
def test_verifier_route(state: dict[str, Any], expected: str) -> None:
    assert verifier_route(state) == expected


# --------------------------------------------------------------------------- #
# final
# --------------------------------------------------------------------------- #
def test_final_node_formats_passed_report(state: dict[str, Any]) -> None:
    state.update(
        {
            "plan_summary": "写文件",
            "todos": [
                {"id": "t1", "content": "写 hello.txt", "status": "completed", "note": ""}
            ],
            "passed": True,
            "attempts": 1,
            "max_attempts": 3,
            "last_actor_summary": "写完了",
            "verification_checks": [{"name": "content", "passed": True, "detail": "ok"}],
            "verification_results": [
                {"command": PASS_COMMAND, "ok": True, "exit_code": 0, "stdout": "1", "stderr": ""}
            ],
        }
    )

    update = final_node(state, {"configurable": {"summarize_final": False}})
    text = update["final_answer"]

    assert update["passed"] is True
    assert "已完成并通过验证" in text
    assert "尝试 1/3 轮" in text
    assert "写完了" in text
    assert "[OK] content" in text
    assert "exit_code=0 OK" in text


def test_final_node_writes_and_deduplicates_task_history(
    state: dict[str, Any],
) -> None:
    state["runtime"].metadata["task_id"] = "task-final-history"
    state.update(
        {
            "plan_summary": "写文件",
            "passed": True,
            "attempts": 2,
            "max_attempts": 3,
            "last_actor_summary": "修正后写入 hello.txt",
            "verification_results": [
                {"command": PASS_COMMAND, "ok": True, "exit_code": 0}
            ],
        }
    )

    final_node(state, {"configurable": {"summarize_final": False}})
    final_node(state, {"configurable": {"summarize_final": False}})

    history = (
        state["runtime"].workspace / HISTORY_SUMMARY_FILENAME
    ).read_text(encoding="utf-8")

    assert history.count("<!-- history-entry:task-final-history -->") == 1
    assert "- Outcome: PASSED" in history
    assert "- Attempts: 2/3" in history
    assert "修正后写入 hello.txt" in history


def test_final_node_failed_report_includes_error_and_next_step(state: dict[str, Any]) -> None:
    state.update(
        {
            "passed": False,
            "attempts": 3,
            "max_attempts": 3,
            "todos": [{"id": "t1", "content": "写文件", "status": "blocked", "note": ""}],
            "last_error": "内容应为 hello",
            "next_instruction": "改成 hello",
        }
    )

    text = final_node(state, {"configurable": {"summarize_final": False}})["final_answer"]

    assert "未通过验证" in text
    assert "尝试 3/3 轮" in text
    assert "内容应为 hello" in text
    assert "改成 hello" in text


def test_final_node_uses_model_when_injected(state: dict[str, Any]) -> None:
    state.update({"passed": True, "attempts": 1, "last_actor_summary": "写完了"})
    model = ScriptedModel([AIMessage(content="总结：已完成，改动了 hello.txt。")])
    state["runtime"].metadata["model"] = model

    update = final_node(state)

    assert update["final_answer"] == "总结：已完成，改动了 hello.txt。"
    assert model.calls[0][0].content == FINAL_PROMPT


def test_final_node_falls_back_when_model_unavailable(state: dict[str, Any]) -> None:
    """总结是尽力而为：模型报错时退回确定性文本，不抛异常。"""
    state.update({"passed": True, "attempts": 1, "last_actor_summary": "写完了"})
    state["runtime"].metadata["model"] = ScriptedModel([])  # 没有脚本 → invoke 抛错

    update = final_node(state)

    assert "已完成并通过验证" in update["final_answer"]


# --------------------------------------------------------------------------- #
# 串起来跑一遍（不建 StateGraph，手动按路由循环）
# --------------------------------------------------------------------------- #
def test_full_loop_with_fake_model(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path / "ws")
    model = ScriptedModel(
        [
            # attempt 1：计划 → 干活 → 验证失败
            _plan_call(),
            _tool_call(
                "FileWriteTool",
                {"file_path": "hello.txt", "content": "bad\n"},
                "w1",
            ),
            AIMessage(content="第一次尝试完成"),
            _verdict(False, reason="内容应为 hello"),
            # attempt 2：按 last_error 修订 → 重写 → 验证通过
            _plan_call(plan_summary="修订：把内容改成 hello"),
            _tool_call(
                "FileWriteTool",
                {"file_path": "hello.txt", "content": "hello\n"},
                "w2",
            ),
            AIMessage(content="第二次尝试完成"),
            _verdict(True),
        ]
    )
    runtime.metadata["model"] = model

    state: dict[str, Any] = {"task": "创建 hello.txt 并写 hello", "runtime": runtime}
    events: list[dict[str, Any]] = []
    config = {"configurable": {"emit": events.append}}

    route = "planner"
    steps = 0
    while route != FINAL and steps < 10:
        steps += 1
        state.update(planner_node(state, config))
        state.update(actor_node(state, config))
        state.update(verifier_node(state, config))
        route = verifier_route(state)

    assert route == FINAL
    assert state["passed"] is True
    assert state["attempts"] == 2
    assert (runtime.workspace / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert all(todo["status"] == "completed" for todo in state["todos"])

    # 第二次规划时，brief 里带上了第一轮的失败原因
    revise_brief = "\n".join(
        str(message.content)
        for call in model.calls
        for message in call
    )
    assert "failed verification" in revise_brief
    assert "内容应为 hello" in revise_brief

    # 事件流里有工具调用与验收命令
    kinds = {event["type"] for event in events}
    assert {"ai_message", "tool_call", "tool_result", "verification_command"} <= kinds


def test_nodes_work_inside_a_real_stategraph(tmp_path: Path) -> None:
    """真实的 LangGraph 回路：既验证 state 增量合法，也验证 config 注入（emit 收得到事件）。

    这条测试专门防一个坑：如果 nodes.py 里加回 `from __future__ import annotations`，
    langgraph 会因为 config 参数注解变成字符串而**静默跳过** config 注入，
    emit 回调就再也收不到任何事件。
    """
    runtime = RuntimeState(workspace=tmp_path / "ws")
    model = ScriptedModel(
        [
            _plan_call(),
            _tool_call(
                "FileWriteTool", {"file_path": "hello.txt", "content": "hello\n"}, "w1"
            ),
            AIMessage(content="写完了"),
            _verdict(True),
        ]
    )
    runtime.metadata["model"] = model

    graph = StateGraph(MeoGraphState)
    graph.add_node("planner", planner_node)
    graph.add_node("actor", actor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "actor")
    graph.add_edge("actor", "verifier")
    graph.add_conditional_edges(
        "verifier", verifier_route, {"planner": "planner", FINAL: END}
    )
    compiled = graph.compile()

    events: list[dict[str, Any]] = []
    final = compiled.invoke(
        {"task": "创建 hello.txt", "runtime": runtime, "max_attempts": 3},
        {"configurable": {"emit": events.append}},
    )

    assert final["passed"] is True
    assert final["attempts"] == 1
    assert (runtime.workspace / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    # add_messages 把各节点返回的消息合并起来了
    assert len(final["messages"]) >= 4
    # config 注入成功：emit 收到了事件
    kinds = {event["type"] for event in events}
    assert {"ai_message", "tool_call", "tool_result", "verification_command"} <= kinds


def test_full_loop_stops_when_attempts_exhausted(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path / "ws")
    responses: list[AIMessage] = []
    for _ in range(2):  # max_attempts=2：两轮都失败
        responses.extend(
            [
                _plan_call(),
                AIMessage(content="做完一轮"),
                _verdict(False, recommended_next_instruction="继续修"),
            ]
        )
    model = ScriptedModel(responses)
    runtime.metadata["model"] = model

    state: dict[str, Any] = {
        "task": "不可能完成的任务",
        "runtime": runtime,
        "max_attempts": 2,
    }

    route = "planner"
    while route != FINAL:
        state.update(planner_node(state))
        state.update(actor_node(state))
        state.update(verifier_node(state))
        route = verifier_route(state)

    assert state["passed"] is False
    assert state["attempts"] == 2
    assert state["last_error"]


# --------------------------------------------------------------------------- #
# supervisor（委派形态的 planner，build_complex_workflow 里的 "planner" 节点）
# --------------------------------------------------------------------------- #
def _delegate_call(name: str, instruction: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": {"instruction": instruction}, "id": call_id, "type": "tool_call"}
        ],
    )


def test_supervisor_node_binds_delegation_tools(state: dict[str, Any]) -> None:
    """supervisor 绑定三个工具：TodoWriteTool + 两个专家委派入口。"""
    model = ScriptedModel([AIMessage(content="直接收尾")])
    state["runtime"].metadata["model"] = model

    update = supervisor_node(state)

    assert model.bound_tool_names == [[TodoWriteTool.name, CALL_SEARCH_AGENT_TOOL, CALL_CODE_AGENT_TOOL]]
    assert isinstance(model.calls[0][0], SystemMessage)
    assert model.calls[0][0].content == SUPERVISOR_PROMPT

    # 模型没发计划：把任务本身兜底成一条待办，并持久化 TODO.md
    assert [todo["id"] for todo in update["todos"]] == ["t1"]
    assert update["todos"][0]["content"] == state["task"]
    assert update["plan_summary"] == state["task"]
    assert (state["runtime"].workspace / "TODO.md").is_file()
    assert update["passed"] is False
    assert update["research_notes"] == ""
    assert update["sources"] == []
    assert update["agent_handoffs"] == []


def test_supervisor_node_applies_plan_and_persists_todo(state: dict[str, Any]) -> None:
    model = ScriptedModel([_plan_call(), AIMessage(content="计划就绪，等待验证")])
    state["runtime"].metadata["model"] = model
    events: list[dict[str, Any]] = []

    update = supervisor_node(state, {"configurable": {"emit": events.append}})

    assert update["plan_summary"] == "创建 hello.txt 并写一行内容"
    assert [todo["id"] for todo in update["todos"]] == ["t1", "t2"]
    assert update["verification_commands"] == [PASS_COMMAND]
    todo_md = (state["runtime"].workspace / "TODO.md").read_text(encoding="utf-8")
    assert "t1: 写入 hello.txt" in todo_md
    # 计划被实时发出去（CLI / 测试都能看到）
    assert any(event["type"] == "plan_snapshot" for event in events)
    assert events[0]["type"] == "node_start"
    assert events[0]["node"] == "planner"


def test_supervisor_node_delegates_to_code_agent(state: dict[str, Any]) -> None:
    """委派真的会跑 codeAgent：文件写进 workspace，交接记录进 state。"""
    model = ScriptedModel(
        [
            _plan_call(),
            _delegate_call(CALL_CODE_AGENT_TOOL, "写入 hello.txt", "d1"),
            _tool_call("FileWriteTool", {"file_path": "hello.txt", "content": "hello\n"}, "w1"),
            AIMessage(content="已写入 hello.txt"),
            AIMessage(content="委派完成，等待验证"),
        ]
    )
    state["runtime"].metadata["model"] = model
    events: list[dict[str, Any]] = []

    update = supervisor_node(state, {"configurable": {"emit": events.append}})

    # codeAgent 真的写了文件
    assert (state["runtime"].workspace / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert update["code_agent_summary"] == "已写入 hello.txt"
    assert update["last_actor_summary"] == "已写入 hello.txt"
    assert update["agent_handoffs"][-1] == {
        "from_agent": "planner",
        "to_agent": "codeAgent",
        "instruction": "写入 hello.txt",
        "result": "已写入 hello.txt",
    }
    # 事件流里有委派交接与本轮工具调用
    kinds = {event["type"] for event in events}
    assert {"handoff", "handoff_result", "plan_snapshot", "tool_call", "tool_result"} <= kinds


def test_supervisor_node_delegates_search_and_merges_results(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """委派 searchAgent：研究笔记合并、来源按 URL 去重。"""
    from meoclaw.tools import web_search_tool

    def fake_web_search(query: str, max_results: int = 5, include_answer: bool = True) -> dict[str, Any]:
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

    monkeypatch.setattr(web_search_tool, "web_search", fake_web_search)

    model = ScriptedModel(
        [
            _plan_call(),
            _delegate_call(CALL_SEARCH_AGENT_TOOL, "查一下相关资料", "s1"),
            _tool_call("WebSearchTool", {"query": "hello"}, "q1"),
            AIMessage(content="研究结论：hello 是常用问候语"),
            AIMessage(content="委派完成，等待验证"),
        ]
    )
    state["runtime"].metadata["model"] = model

    update = supervisor_node(state)

    assert update["research_notes"] == "研究结论：hello 是常用问候语"
    assert update["sources"] == [
        {"title": "官方文档", "url": "https://example.com/docs"},
        {"title": "百科条目", "url": "https://example.com/wiki"},
    ]
    assert update["agent_handoffs"][-1]["to_agent"] == "searchAgent"
    # 只委派了研究：code_agent_summary 为空，last_actor_summary 回落到 supervisor 自己的收尾
    assert update["code_agent_summary"] == ""
    assert update["last_actor_summary"] == "委派完成，等待验证"


def test_supervisor_node_unknown_tool_degrades(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型调了不存在的工具：ok=False 的结果回到模型，循环不中断。"""
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[_tool_call("NonsenseTool", {}, "x1").tool_calls[0]]),
            AIMessage(content="收到，那我直接收尾"),
        ]
    )
    state["runtime"].metadata["model"] = model
    events: list[dict[str, Any]] = []

    supervisor_node(state, {"configurable": {"emit": events.append}})

    failed = [event for event in events if event["type"] == "tool_result" and not event["ok"]]
    assert len(failed) == 1
    assert "未知工具" in str(failed[0]["result"])
    # 循环没有被失败打断，supervisor 正常收尾


def test_supervisor_node_requires_task(runtime: RuntimeState) -> None:
    with pytest.raises(PlannerError):
        supervisor_node({"runtime": runtime})
