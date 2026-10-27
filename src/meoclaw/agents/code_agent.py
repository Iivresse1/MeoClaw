"""codeAgent：把 planner 的指令落地成文件/命令改动的实现专家。

用法（节点或 supervisor 调用）：

    result = run_code_agent(state, "实现 add(a, b) 并补测试", writer=emit)
    result["ok"], result["summary"], result["todos"], result["tool_events"]

内部复用底层 ReAct 循环（`core.react.stream_react_events`），所以 tool_call_id 配对、
工具失败降级成 `ok=False`、输出截断这些行为与 actor 节点完全一致。与 actor 节点的区别：

- 系统提示词是 `CODE_AGENT_PROMPT`，并被要求显式维护 todo 进度；
- 工具集额外带 `NotepadReadTool` / `NotepadAppendTool`（长期笔记，抗上下文压缩）；
- 收尾时把 todos 持久化到工作区 `TODO.md`。

注意：本模块**不 import `graph.nodes`**，而是共用 `core.context` 里的上下文工具，
这样将来 planner 节点直接调用本模块也不会形成 import 环。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from meoclaw.core.context import Emit, require_runtime, resolve_emit, resolve_model
from meoclaw.core.context_builder import ContextBuilder
from meoclaw.core.react import stream_react_events
from meoclaw.core.sandbox import build_sandbox_shell_middleware, sandbox_mode
from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import (
    RULES_LAYER,
    build_layered_memory,
    format_layered_memory_for_prompt,
)
from meoclaw.prompts.stage3 import CODE_AGENT_PROMPT
from meoclaw.tools.notepad_tool import build_notepad_tools
from meoclaw.tools.registry import build_tools
from meoclaw.tools.todo_tool import (
    apply_todo_updates,
    build_todo_update_tool,
    drain_todo_updates,
    persist_todos,
)

__all__ = [
    "CODE_AGENT_NAME",
    "DEFAULT_MAX_LOOPS",
    "RULES_LAYER",
    "build_code_agent_tools",
    "build_memory_snapshot",
    "compose_human_message",
    "run_code_agent",
]

#: Agent 名字（事件里用它标记来源）
CODE_AGENT_NAME = "codeAgent"
#: 一轮最多几次"模型 → 工具"往返
DEFAULT_MAX_LOOPS = 10


def build_code_agent_tools(state: RuntimeState) -> list:
    """codeAgent 的工具集：通用工具 + TodoUpdateTool + 笔记工具。"""
    return [
        *build_tools(state, include_bash=sandbox_mode(state) != "docker"),
        build_todo_update_tool(state),
        *build_notepad_tools(state),
    ]


def build_memory_snapshot(state: Mapping[str, Any] | RuntimeState) -> str:
    """构建分层记忆快照文本，直接拼进提示词。

    实现委托给 [`graph/memory.py`](../graph/memory.py) 的 `build_layered_memory()`
    ——三层 Memory（Rules / Working / History Summary）只有那一处实现。
    """
    memory = build_layered_memory(state, node=CODE_AGENT_NAME)
    return format_layered_memory_for_prompt(memory)


def compose_human_message(
    state: Mapping[str, Any] | RuntimeState,
    instruction: str,
    *,
    memory: Mapping[str, Any] | None = None,
) -> str:
    """HumanMessage = 任务 + planner 指令 + session 上下文 + memory 快照。

    `memory` 可以外部传入（避免同一轮重复组装）。
    """
    view: Mapping[str, Any] = state if isinstance(state, Mapping) else {"runtime": state}
    parts: list[str] = []

    task = str(view.get("task") or "").strip()
    if task:
        parts.append(f"## Task\n{task}")

    instruction = (instruction or "").strip()
    if instruction:
        parts.append(f"## Instruction from planner\n{instruction}")

    context: list[str] = []
    plan_summary = str(view.get("plan_summary") or "").strip()
    if plan_summary:
        context.append(f"- Plan summary: {plan_summary}")
    for todo in view.get("todos") or []:
        note = str(todo.get("note") or "").strip()
        context.append(
            f"- Todo {todo.get('id')} [{todo.get('status', 'pending')}]: {todo.get('content')}"
            + (f" (note: {note})" if note else "")
        )
    last_error = str(view.get("last_error") or "").strip()
    if last_error:
        context.append(f"- Previous failure: {last_error}")
    next_instruction = str(view.get("next_instruction") or "").strip()
    if next_instruction:
        context.append(f"- Verifier suggestion: {next_instruction}")
    if context:
        parts.append("## Session context\n" + "\n".join(context))

    snapshot = memory if memory is not None else build_layered_memory(view, node=CODE_AGENT_NAME)
    if snapshot:
        parts.append(f"## Memory snapshot\n{format_layered_memory_for_prompt(snapshot)}")

    return "\n\n".join(parts)


def run_code_agent(
    state: Mapping[str, Any] | RuntimeState,
    instruction: str,
    *,
    writer: Emit | None = None,
    max_loops: int = DEFAULT_MAX_LOOPS,
    model: BaseChatModel | None = None,
) -> dict[str, Any]:
    """跑一轮 codeAgent，返回本轮结果。

    Args:
        state: 图状态（含 `runtime` / `task` / `todos` …），也可以直接给 `RuntimeState`。
        instruction: planner 交给专家的具体指令。
        writer: 事件出口；为 None 时用 langgraph 的 custom stream writer（在图里跑时）。
        max_loops: 最多几次"模型 → 工具"往返。
        model: 可注入模型（测试用）；为空时按 config/metadata/create_model 顺序解析。

    Returns:
        `{ok, summary, todos, messages, tool_events, agent, stopped_reason, loops, todo_file}`
        - `ok`：本轮是否正常收尾（没有撞到 `max_loops`）；
        - `todos`：TodoUpdateTool 的变更已合并进去；
        - `messages`：本轮新增的 AI / ToolMessage（可直接进图状态）；
        - `tool_events`：本轮的全部事件（带 `agent` 标记）。

    Raises:
        ValueError: instruction 为空或 max_loops 非法。
    """
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction 不能为空")
    if not isinstance(max_loops, int) or isinstance(max_loops, bool) or max_loops < 1:
        raise ValueError(f"max_loops 必须是大于 0 的整数，收到 {max_loops!r}")

    runtime = require_runtime(state)
    view: Mapping[str, Any] = state if isinstance(state, Mapping) else {"runtime": runtime}
    chat_model = model if model is not None else resolve_model(view)
    emit = writer if writer is not None else resolve_emit()

    tools = build_code_agent_tools(runtime)
    extra_middleware = (
        [build_sandbox_shell_middleware(runtime)]
        if sandbox_mode(runtime) == "docker"
        else []
    )

    # 三层记忆快照只组装一次：既拼进提示词，也随结果返回（调用方可存进 state["memory_snapshot"]）
    memory_snapshot = build_layered_memory(view, node=CODE_AGENT_NAME)
    task_id = str((runtime.metadata or {}).get("task_id") or "")
    bundle = ContextBuilder().build(
        "actor",
        view,
        CODE_AGENT_PROMPT,
        task_id=task_id,
        instruction=instruction,
    )

    messages: list[BaseMessage] = []
    tool_events: list[dict[str, Any]] = []
    summary = ""
    stopped_reason = ""
    loops = 0

    for event in stream_react_events(
        instruction,
        runtime=runtime,
        max_loops=max_loops,
        model=chat_model,
        tools=tools,
        system_prompt=bundle.system_prompt,
        input_messages=bundle.messages,
        role="actor",
        task_id=task_id,
        permission_policy=str(
            (runtime.metadata or {}).get("permission") or "full"
        ),
        extra_middleware=extra_middleware,
        message_sink=messages,
        stream_tokens=True,
    ):
        tagged = {**event, "agent": CODE_AGENT_NAME}
        tool_events.append(tagged)
        if emit is not None:
            emit(tagged)
        if event.get("type") == "final_answer":
            summary = str(event.get("content") or "")
            stopped_reason = str(event.get("stopped_reason") or "")
            loops = int(event.get("loops") or 0)

    # TodoUpdateTool 的变更记在 runtime.metadata 里，这里合并回待办并持久化
    todos = apply_todo_updates(view.get("todos"), drain_todo_updates(runtime))
    todo_file = None
    if todos:
        todo_file = persist_todos(
            runtime, todos, plan_summary=str(view.get("plan_summary") or "")
        )

    return {
        "ok": stopped_reason == "no_tool_calls",
        "summary": summary,
        "todos": todos,
        "messages": messages,
        "tool_events": tool_events,
        "agent": CODE_AGENT_NAME,
        "stopped_reason": stopped_reason,
        "loops": loops,
        "todo_file": todo_file,
        "memory_snapshot": memory_snapshot,
    }
