"""Context projection, middleware, and SQLite checkpoint regression tests."""

from __future__ import annotations

import json
import shutil as shutil_module
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from meoclaw.core.context_builder import ContextBuilder, tag_message_context
from meoclaw.core.middleware import build_agent_middleware
from meoclaw.core.react import stream_react_events
from meoclaw.core.sandbox import SandboxUnavailableError, build_sandbox_shell_middleware
from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import HISTORY_SUMMARY_FILENAME
from meoclaw.graph.workflow import build_workflow, initial_state


class ScriptedModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Any, **_: Any) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class ToolFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **_: Any) -> ToolFakeModel:
        return self


def _plan() -> AIMessage:
    return AIMessage(
        content=json.dumps(
            {
                "plan_summary": "直接完成",
                "todos": [
                    {"id": "t1", "content": "完成任务", "status": "pending", "note": ""}
                ],
                "acceptance_criteria": ["输出完成"],
                "verification_commands": [],
            },
            ensure_ascii=False,
        )
    )


def test_context_builder_projects_canonical_messages_by_role(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path / "context-ws")
    runtime.metadata.update({"task_id": "task-2", "session_id": "session-1"})
    old = tag_message_context(
        AIMessage(content="上一个任务的结论"),
        role="final",
        task_id="task-1",
        visible_to=["planner", "final"],
    )
    current_user = tag_message_context(
        HumanMessage(content="当前任务"),
        role="planner",
        task_id="task-2",
        visible_to=["planner", "actor", "verifier", "final"],
    )
    ai_call = tag_message_context(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "FileReadTool", "args": {"file_path": "x"}, "id": "c1"}
            ],
        ),
        role="actor",
        task_id="task-2",
        visible_to=["actor", "verifier", "final"],
    )
    tool_result = tag_message_context(
        ToolMessage(content='{"ok": true}', name="FileReadTool", tool_call_id="c1"),
        role="actor",
        task_id="task-2",
        visible_to=["actor", "verifier", "final"],
    )
    state = {
        "task": "当前任务",
        "runtime": runtime,
        "messages": [old, current_user, ai_call, tool_result],
        "todos": [],
    }

    builder = ContextBuilder()
    planner = builder.build("planner", state, "planner system")
    actor = builder.build("actor", state, "actor system")
    verifier = builder.build("verifier", state, "verifier system")

    assert any("上一个任务的结论" in str(m.content) for m in planner.messages)
    assert any(isinstance(m, AIMessage) and m.tool_calls for m in actor.messages)
    assert any(isinstance(m, ToolMessage) for m in actor.messages)
    assert not any(isinstance(m, ToolMessage) for m in verifier.messages)
    assert "tool_evidence" in str(verifier.messages[-1].content)


def test_sqlite_checkpoint_persists_outer_messages(tmp_path: Path) -> None:
    db = tmp_path / "checkpoints.sqlite"
    conn = sqlite3.connect(db, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    runtime = RuntimeState(workspace=tmp_path / "ws")
    model = ScriptedModel(
        [
            _plan(),
            AIMessage(content="完成"),
            AIMessage(
                content=json.dumps(
                    {
                        "passed": True,
                        "reason": "完成",
                        "checks": [],
                        "recommended_next_instruction": "",
                    }
                )
            ),
            AIMessage(content="已总结"),
        ]
    )
    state = initial_state(
        "持久化任务",
        runtime=runtime,
        task_id="task-checkpoint",
        session_id="session-checkpoint",
    )
    config = {
        "configurable": {
            "thread_id": "session-checkpoint",
            "model": model,
        }
    }
    result = build_workflow(checkpointer=saver).invoke(state, config)
    conn.close()

    conn2 = sqlite3.connect(db, check_same_thread=False)
    saver2 = SqliteSaver(conn2)
    snapshot = build_workflow(checkpointer=saver2).get_state(config)
    conn2.close()

    assert result["passed"] is True
    assert any(isinstance(message, HumanMessage) for message in snapshot.values["messages"])
    assert snapshot.values["runtime"].workspace == runtime.workspace
    assert db.stat().st_size > 0


def test_human_in_the_loop_interrupt_and_resume(tmp_path: Path) -> None:
    @tool
    def echo(value: str) -> str:
        """Echo a value."""
        return value

    runtime = RuntimeState(workspace=tmp_path / "hitl-ws")
    runtime.metadata.update({"task_id": "hitl-task", "thread_id": "hitl-task"})
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "echo",
                        "args": {"value": "approved"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="完成"),
        ]
    )
    saver = InMemorySaver()
    first = list(
        stream_react_events(
            "测试审批",
            runtime=runtime,
            model=model,
            tools=[echo],
            approval_tools=["echo"],
            checkpointer=saver,
            task_id="hitl-task",
        )
    )
    approval = next(event for event in first if event["type"] == "approval_request")
    assert approval["tool"] == "echo"
    assert first[-1]["stopped_reason"] == "approval_required"

    resumed = list(
        stream_react_events(
            "测试审批",
            runtime=runtime,
            model=model,
            tools=[echo],
            approval_tools=["echo"],
            checkpointer=saver,
            task_id="hitl-task",
            resume={"type": "approve"},
        )
    )
    assert any(
        event["type"] == "tool_result" and event["ok"] for event in resumed
    )
    assert resumed[-1]["content"] == "完成"


def test_same_session_second_task_reuses_message_timeline(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "timeline.sqlite", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    graph = build_workflow(checkpointer=saver)
    config = {"configurable": {"thread_id": "shared-session"}}

    models: list[ScriptedModel] = []

    def model_for(summary: str) -> ScriptedModel:
        model = ScriptedModel(
            [
                _plan(),
                AIMessage(content="完成"),
                AIMessage(
                    content=json.dumps(
                        {
                            "passed": True,
                            "reason": "完成",
                            "checks": [],
                            "recommended_next_instruction": "",
                        }
                    )
                ),
                AIMessage(content=summary),
            ]
        )
        models.append(model)
        return model

    runtime1 = RuntimeState(workspace=tmp_path / "timeline-ws")
    state1 = initial_state(
        "第一个任务",
        runtime=runtime1,
        task_id="task-1",
        session_id="shared-session",
    )
    graph.invoke(state1, {**config, "configurable": {**config["configurable"], "model": model_for("总结一")}})

    runtime2 = RuntimeState(workspace=tmp_path / "timeline-ws")
    state2 = initial_state(
        "第二个任务",
        runtime=runtime2,
        task_id="task-2",
        session_id="shared-session",
    )
    graph.invoke(state2, {**config, "configurable": {**config["configurable"], "model": model_for("总结二")}})

    snapshot = graph.get_state(config)
    contents = [str(message.content) for message in snapshot.values["messages"]]
    assert "第一个任务" in contents
    assert "第二个任务" in contents
    assert "总结一" in contents
    assert "总结二" in contents

    history = (
        runtime1.workspace / HISTORY_SUMMARY_FILENAME
    ).read_text(encoding="utf-8")
    assert "第一个任务" in history
    assert "第二个任务" in history

    second_task_input = "\n".join(
        str(message.content) for call in models[1].calls for message in call
    )
    assert "第一个任务" in second_task_input
    assert "- Outcome: PASSED" in second_task_input
    conn.close()


def test_common_middleware_stack_uses_langchain_builtins(tmp_path: Path) -> None:
    model = ToolFakeModel(responses=[AIMessage(content="ok")])
    runtime = RuntimeState(workspace=tmp_path / "middleware-ws")
    stack = build_agent_middleware(
        model,
        max_model_calls=4,
        summary_token_limit=1000,
    )
    assert any(isinstance(item, ToolErrorMiddleware) for item in stack)
    assert any(isinstance(item, ToolRetryMiddleware) for item in stack)
    assert any(isinstance(item, ModelCallLimitMiddleware) for item in stack)
    assert any(isinstance(item, SummarizationMiddleware) for item in stack)

    assert runtime.workspace.is_dir()


def test_docker_sandbox_fails_closed_when_docker_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = RuntimeState(workspace=tmp_path / "sandbox-ws")
    monkeypatch.setattr(shutil_module, "which", lambda _: None)
    with pytest.raises(SandboxUnavailableError):
        build_sandbox_shell_middleware(runtime)


def test_docker_sandbox_mounts_workspace_at_container_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = RuntimeState(workspace=tmp_path / "sandbox-ws")
    monkeypatch.setattr(shutil_module, "which", lambda _: "docker")
    middleware = build_sandbox_shell_middleware(runtime)
    command = middleware._execution_policy._build_command(
        runtime.workspace,
        {},
        ("/bin/bash",),
    )

    mount = f"{runtime.workspace.resolve()}:/workspace"
    assert mount in command
    assert command[command.index("-w") + 1] == "/workspace"
    assert "--network" in command
    assert command[command.index("--network") + 1] == "none"
