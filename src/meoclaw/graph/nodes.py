"""planner / actor / verifier 三个核心节点 + 路由函数。

图形状（由调用方组装 StateGraph）：

    START → planner → actor → verifier → (passed 或 attempts 用尽) → final → END
                     ↑                       │
                     └───────── planner ←────┘   未通过且还有机会

约定：

- 每个节点都从 `state["runtime"]` 取 `RuntimeState`；模型优先取
  `config["configurable"]["model"]`，其次 `runtime.metadata["model"]`，
  都没有才 `create_model()` —— 所以测试可以完全离线注入假模型。
- 节点返回值就是 LangGraph 的 state 增量；`messages` 只返回**本轮新增**的消息，
  由 `add_messages` reducer 合并。
- 实时事件：actor / verifier 内部复用 `core/react.stream_react_events()`，
  把事件转发给 langgraph 的 custom stream writer（以及 config 里的 emit 回调）。

注意：本模块**刻意不使用** `from __future__ import annotations`。
langgraph 的节点包装器用 `inspect.signature(...).parameters[...].annotation`
判断"要不要注入 config"，它只认真实的 `RunnableConfig` / `Optional[RunnableConfig]`
对象（或恰好写成 `"Optional[RunnableConfig]"` 的字符串）。一旦注解变成
`"RunnableConfig | None"` 字符串，langgraph 会**静默跳过 config 注入**，
`emit` 回调就永远收不到事件。Python >= 3.10 因此可以直接写真实注解。
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, ToolException
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from meoclaw.agents.code_agent import CODE_AGENT_NAME, run_code_agent
from meoclaw.agents.search_agent import SEARCH_AGENT_NAME, run_search_agent
from meoclaw.core.context import Emit, require_runtime, resolve_emit, resolve_model
from meoclaw.core.react import (
    message_text,
    serialize_tool_result,
    stream_react_events,
)
from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import (
    append_history_summary,
    build_layered_memory,
    format_layered_memory_for_prompt,
    short_text,
    trim_handoffs,
)
from meoclaw.graph.state import MeoGraphState
from meoclaw.prompts import (
    ACTOR_PROMPT,
    CONTEXT_COMPRESSION_PROMPT,
    FINAL_PROMPT,
    PLANNER_PROMPT,
    SUPERVISOR_PROMPT,
    VERIFIER_PROMPT,
)
from meoclaw.tools.bash_tool import execute_command
from meoclaw.tools.registry import build_read_only_tools, build_tools
from meoclaw.tools.todo_tool import (
    TodoWriteTool,
    apply_todo_updates,
    build_todo_update_tool,
    drain_todo_updates,
    normalize_plan,
    persist_todos,
)

__all__ = [
    "CALL_CODE_AGENT_TOOL",
    "CALL_SEARCH_AGENT_TOOL",
    "CONTEXT_COMPRESSOR_NODE",
    "DEFAULT_CONTEXT_TOKEN_LIMIT",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_NEXT_NODE",
    "FINAL",
    "MAX_ACTOR_LOOPS",
    "MAX_SUPERVISOR_LOOPS",
    "MAX_VERIFIER_LOOPS",
    "PlannerError",
    "actor_node",
    "build_todo_update_tool",
    "compose_actor_prompt",
    "compose_final_report",
    "compose_supervisor_brief",
    "compose_verifier_brief",
    "context_compressor_node",
    "context_monitor_node",
    "context_monitor_route",
    "estimate_context_tokens",
    "execute_supervisor_tool",
    "extract_json_object",
    "extract_plan",
    "final_node",
    "merge_research_notes",
    "merge_sources",
    "parse_verdict",
    "planner_node",
    "resolve_emit",
    "resolve_model",
    "run_verification_commands",
    "supervisor_node",
    "verifier_node",
    "verifier_route",
]

#: actor 每轮最多几次"模型 → 工具"往返
MAX_ACTOR_LOOPS = 10
#: supervisor（委派形态的 planner）最多几次"模型 → 工具"往返
MAX_SUPERVISOR_LOOPS = 8
#: supervisor 委派给专家 Agent 的两个工具名
CALL_SEARCH_AGENT_TOOL = "CallSearchAgentTool"
CALL_CODE_AGENT_TOOL = "CallCodeAgentTool"
#: verifier 只读 agent 的最大往返次数（它主要是查证，不需要很多轮）
MAX_VERIFIER_LOOPS = 6
#: 默认的最大尝试轮数（verifier 每跑一次算一轮）
DEFAULT_MAX_ATTEMPTS = 3
#: 路由终点的名字
FINAL = "final"
#: 上下文超过这个 token 数就要压缩
DEFAULT_CONTEXT_TOKEN_LIMIT = 400_000
#: `context_next_node` 的默认去向（拿不到上游设置时）
DEFAULT_NEXT_NODE = "verifier"
#: 压缩节点的名字（`context_monitor_route` 会返回它）
CONTEXT_COMPRESSOR_NODE = "context_compressor"


class PlannerError(RuntimeError):
    """planner 没有产出可用计划时抛出。"""


# 公共小工具（require_runtime / resolve_model / resolve_emit / Emit）统一放在
# meoclaw.core.context：专家 Agent 也要用同一套，放 core 层可避免 graph ↔ agents 成环。


def _emit_node_start(emit: Emit | None, node: str) -> None:
    """告诉消费方"这个节点开始了"（CLI 用它显示 📋/🔧/✅/📝 标题）。"""
    if emit is not None:
        emit({"type": "node_start", "node": node})


def _forward_react_event(emit: Emit | None, node: str, event: Mapping[str, Any]) -> None:
    """把底层 ReAct 事件转发到统一事件流，并打上 `node` 标签。

    底层 ReAct 的收尾事件也叫 `final_answer`，会和 final 节点的 `final_answer`
    撞名，这里统一改名成 `react_done`（外表只关心 final 节点那一个）。
    """
    if emit is None:
        return
    forwarded = {**event, "node": node}
    if forwarded.get("type") == "final_answer":
        forwarded["type"] = "react_done"
    emit(forwarded)


def _clip(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从文本里抠出第一个 JSON 对象。

    容忍 ```json 代码块和前后废话；字符串里的花括号不会干扰配对。
    """
    if not text:
        return None

    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()

    start = candidate.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : index + 1])
                    except json.JSONDecodeError:
                        break
        start = candidate.find("{", start + 1)
    return None


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
def extract_plan(response: Any) -> dict[str, Any]:
    """从模型回应里取计划：优先 TodoWriteTool 的 tool_call，其次正文 JSON。"""
    for call in getattr(response, "tool_calls", None) or []:
        if isinstance(call, Mapping) and call.get("name") == TodoWriteTool.name:
            args = call.get("args")
            if isinstance(args, Mapping):
                return normalize_plan(args)

    content = message_text(getattr(response, "content", ""))
    payload = extract_json_object(content)
    if payload is None:
        raise PlannerError(
            "planner 既没有调用 TodoWriteTool，正文里也找不到 JSON 计划；"
            f"原始输出：{_clip(content, 200)!r}"
        )
    return normalize_plan(payload)


def _planner_brief(state: Mapping[str, Any], task: str, *, revising: bool) -> str:
    if not revising:
        return f"Task:\n{task}\n\nCall TodoWriteTool with your plan."

    payload = {
        "task": task,
        "previous_plan_summary": state.get("plan_summary", ""),
        "previous_todos": state.get("todos", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "verification_failure": state.get("last_error", ""),
        "recommended_next_instruction": state.get("next_instruction", ""),
        "attempts": state.get("attempts", 0),
    }
    return (
        "The previous attempt failed verification. Revise the plan and call "
        "TodoWriteTool again.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def planner_node(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """生成或修订计划。

    - 没有 todos：让 LLM 用 `TodoWriteTool` 给出完整计划。
    - 已有 todos 且 verifier 失败过：带上 `last_error` / `next_instruction` 修订计划。

    Returns:
        `plan_summary` / `todos` / `acceptance_criteria` / `verification_commands`
        （以及 `max_attempts`、`passed=False`）。

    Raises:
        PlannerError: 模型既没调工具也没给出可解析的 JSON。
    """
    require_runtime(state)  # 先校验 runtime 存在（模型与后续节点都依赖它）
    model = resolve_model(state, config)
    emit = resolve_emit(config)
    _emit_node_start(emit, "planner")

    task = str(state.get("task") or "").strip()
    if not task:
        raise PlannerError("state['task'] 为空，无法制定计划")

    revising = bool(state.get("todos")) and bool(str(state.get("last_error") or "").strip())
    brief = _planner_brief(state, task, revising=revising)

    planner = model.bind_tools([TodoWriteTool])
    response = planner.invoke(
        [SystemMessage(content=PLANNER_PROMPT), HumanMessage(content=brief)]
    )
    plan = extract_plan(response)

    # 兜底：模型没给出可用待办时，至少把任务本身当成一条待办，保证 actor 有活干
    todos = plan["todos"] or [
        {"id": "t1", "content": task, "status": "pending", "note": ""}
    ]

    return {
        "plan_summary": plan["plan_summary"] or task,
        "todos": todos,
        "acceptance_criteria": plan["acceptance_criteria"],
        "verification_commands": plan["verification_commands"],
        "max_attempts": int(state.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
        "passed": False,
        # 规划完该去验证（supervisor 拓扑 planner → context_monitor → verifier）。
        # 简单拓扑里 planner 后面还有 actor，但这个值会在 verifier 那里被覆盖，
        # 不会被 context_monitor_route 读到。
        "context_next_node": "verifier",
    }


# --------------------------------------------------------------------------- #
# supervisor（委派形态的 planner，供 build_complex_workflow 使用）
# --------------------------------------------------------------------------- #
def merge_research_notes(existing: Any, addition: Any) -> str:
    """合并两段研究笔记（都非空时用空行分隔）。"""
    left = "" if existing is None else str(existing).strip()
    right = "" if addition is None else str(addition).strip()
    if left and right:
        return f"{left}\n\n{right}"
    return left or right


def merge_sources(existing: Any, addition: Any) -> list[dict[str, str]]:
    """合并来源列表并按 URL 去重（只保留 title / url）。"""
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in [*(existing or []), *(addition or [])]:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append({"title": str(item.get("title") or url), "url": url})
    return merged


def compose_supervisor_brief(
    state: Mapping[str, Any],
    task: str,
    memory: Mapping[str, Any],
) -> str:
    """supervisor 的输入：任务 +（失败时的）修订上下文 + 三层记忆快照。"""
    parts = [f"## Task\n{task}", f"Attempt: {int(state.get('attempts') or 0) + 1}"]

    if state.get("todos") and str(state.get("last_error") or "").strip():
        payload = {
            "previous_plan_summary": state.get("plan_summary", ""),
            "previous_todos": state.get("todos", []),
            "verification_failure": state.get("last_error", ""),
            "recommended_next_instruction": state.get("next_instruction", ""),
            "attempts": state.get("attempts", 0),
        }
        parts.append(
            "The previous attempt failed verification. Revise the plan and "
            "delegate only the missing fix.\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    parts.append("## Memory snapshot\n" + format_layered_memory_for_prompt(memory))
    parts.append(
        "Call TodoWriteTool with your plan first, then delegate research via "
        "CallSearchAgentTool and implementation via CallCodeAgentTool as needed. "
        "Finish with a concise supervisor summary."
    )
    return "\n\n".join(parts)


def _build_supervisor_tools(
    working: dict[str, Any],
    emit: Emit | None,
) -> list[StructuredTool]:
    """构造 supervisor 的三个工具：TodoWriteTool + 两个专家委派入口。

    委派工具是闭包：真正执行 `run_search_agent` / `run_code_agent`，
    并把研究笔记、来源、交接记录合并进可变的 `working` 视图。
    TodoWriteTool 的"应用计划"语义由 `execute_supervisor_tool` 特殊处理，
    这里只负责把它绑给模型（schema 亮相）。
    """

    def call_search_agent(instruction: str) -> dict[str, Any]:
        instruction = (instruction or "").strip()
        if not instruction:
            return {"ok": False, "error": "instruction 不能为空"}
        if emit is not None:
            emit(
                {
                    "type": "handoff",
                    "node": "planner",
                    "from": "planner",
                    "to": SEARCH_AGENT_NAME,
                    "instruction": instruction,
                }
            )
        result = run_search_agent(working, instruction, writer=emit)
        working["research_notes"] = merge_research_notes(
            working.get("research_notes", ""), result["summary"]
        )
        working["sources"] = merge_sources(working.get("sources"), result["sources"])
        working["agent_handoffs"] = [
            *list(working.get("agent_handoffs") or []),
            {
                "from_agent": "planner",
                "to_agent": SEARCH_AGENT_NAME,
                "instruction": instruction,
                "result": result["summary"],
            },
        ]
        if emit is not None:
            emit(
                {
                    "type": "handoff_result",
                    "node": "planner",
                    "from": SEARCH_AGENT_NAME,
                    "to": "planner",
                    "result": result["summary"],
                }
            )
        return {
            "ok": True,
            "summary": result["summary"],
            "queries": result["queries"],
            "sources": working["sources"],
        }

    def call_code_agent(instruction: str) -> dict[str, Any]:
        instruction = (instruction or "").strip()
        if not instruction:
            return {"ok": False, "error": "instruction 不能为空"}
        if emit is not None:
            emit(
                {
                    "type": "handoff",
                    "node": "planner",
                    "from": "planner",
                    "to": CODE_AGENT_NAME,
                    "instruction": instruction,
                }
            )
        result = run_code_agent(working, instruction, writer=emit)
        working["todos"] = result["todos"] or list(working.get("todos") or [])
        working["code_agent_summary"] = result["summary"]
        working["agent_handoffs"] = [
            *list(working.get("agent_handoffs") or []),
            {
                "from_agent": "planner",
                "to_agent": CODE_AGENT_NAME,
                "instruction": instruction,
                "result": result["summary"],
            },
        ]
        if emit is not None:
            emit(
                {
                    "type": "handoff_result",
                    "node": "planner",
                    "from": CODE_AGENT_NAME,
                    "to": "planner",
                    "result": result["summary"],
                }
            )
        return {"ok": True, "summary": result["summary"], "todos": working["todos"]}

    return [
        TodoWriteTool,
        StructuredTool.from_function(
            func=call_search_agent,
            name=CALL_SEARCH_AGENT_TOOL,
            description="Delegate web research to searchAgent. Args: instruction.",
        ),
        StructuredTool.from_function(
            func=call_code_agent,
            name=CALL_CODE_AGENT_TOOL,
            description="Delegate file/code implementation work to codeAgent. Args: instruction.",
        ),
    ]


def execute_supervisor_tool(
    working: dict[str, Any],
    emit: Emit | None,
    call: Any,
    tool_map: Mapping[str, StructuredTool],
) -> ToolMessage:
    """执行一次 supervisor 工具调用，结果写回可变的 `working` 视图。

    - `TodoWriteTool`：规范化计划 → 更新 working → 持久化 `TODO.md` → 发 `plan_snapshot`；
    - 两个委派工具：`tool.invoke`（闭包里已合并 research/sources/handoffs）；
    - 未知工具 / 执行异常：收敛成 `ok=False` 的结果文本，不中断循环。
    """
    name = str((call.get("name") if isinstance(call, Mapping) else "") or "")
    args = (call.get("args") if isinstance(call, Mapping) else None) or {}
    call_id = str((call.get("id") if isinstance(call, Mapping) else "") or "") or f"{name}-call"

    if name == TodoWriteTool.name:
        plan = normalize_plan(args)
        if not plan["todos"]:
            result: Any = {"ok": False, "error": "todos 不能为空：请给出至少一条可验证的待办"}
        else:
            working["plan_summary"] = plan["plan_summary"]
            working["todos"] = plan["todos"]
            working["acceptance_criteria"] = plan["acceptance_criteria"]
            working["verification_commands"] = plan["verification_commands"]
            persist_todos(
                require_runtime(working), plan["todos"], plan_summary=plan["plan_summary"]
            )
            if emit is not None:
                emit({"type": "plan_snapshot", "node": "planner", **plan})
            result = {"ok": True, **plan}
    else:
        tool = tool_map.get(name)
        if tool is None:
            result = {
                "ok": False,
                "error": f"未知工具 {name!r}；可用工具：{', '.join(tool_map)}",
            }
        else:
            try:
                result = tool.invoke(args)
            except KeyboardInterrupt:  # pragma: no cover - 交给上层
                raise
            except Exception as exc:  # noqa: BLE001 - 单个工具失败不中断 supervisor
                result = {"ok": False, "error": f"工具执行失败：{type(exc).__name__}: {exc}"}

    return ToolMessage(
        content=serialize_tool_result(result),
        name=name or "supervisor_tool",
        tool_call_id=call_id,
    )


def supervisor_node(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """planner 的 supervisor 形态：制定计划，并把专家工作委派出去。

    与 `planner_node` 的区别：绑定 `CallSearchAgentTool` / `CallCodeAgentTool`，
    在一个最多 `MAX_SUPERVISOR_LOOPS` 轮的工具循环里把研究/实现交给专家 Agent，
    自己只做协调（stage-3 的 supervisor 拓扑，`build_complex_workflow` 用的就是它）。

    Returns:
        planner 的全部计划字段，外加委派产出的 `research_notes` / `sources` /
        `agent_handoffs` / `code_agent_summary` / `messages`（本轮新增消息）。

    Raises:
        PlannerError: state['task'] 为空。
    """
    runtime = require_runtime(state)
    task = str(state.get("task") or "").strip()
    if not task:
        # 先校验输入再解析模型：缺 Key 的环境里同样能得到明确的错误
        raise PlannerError("state['task'] 为空，无法制定计划")

    model = resolve_model(state, config)
    emit = resolve_emit(config)
    _emit_node_start(emit, "planner")

    working = dict(state)  # 工具循环里的计划/交接变更先写进这个可变视图
    memory = build_layered_memory(working, node="planner")
    tools = _build_supervisor_tools(working, emit)
    tool_map = {tool.name: tool for tool in tools}
    supervisor = model.bind_tools(tools)

    messages: list[BaseMessage] = [
        SystemMessage(content=SUPERVISOR_PROMPT),
        HumanMessage(content=compose_supervisor_brief(working, task, memory)),
    ]
    collected: list[BaseMessage] = []
    summary = ""

    for _ in range(MAX_SUPERVISOR_LOOPS):
        response = supervisor.invoke(messages)
        collected.append(response)
        messages.append(response)

        text = message_text(getattr(response, "content", ""))
        if text.strip():
            summary = text
        calls = list(getattr(response, "tool_calls", None) or [])
        if not calls:
            break

        for call in calls:
            name = str((call.get("name") if isinstance(call, Mapping) else "") or "")
            args = (call.get("args") if isinstance(call, Mapping) else None) or {}
            if emit is not None:
                emit({"type": "tool_call", "node": "planner", "name": name, "args": args})
            tool_message = execute_supervisor_tool(working, emit, call, tool_map)
            collected.append(tool_message)
            messages.append(tool_message)
            if emit is not None:
                parsed: Any = None
                try:
                    parsed = json.loads(tool_message.content)
                except (TypeError, json.JSONDecodeError):
                    parsed = tool_message.content
                emit(
                    {
                        "type": "tool_result",
                        "node": "planner",
                        "name": name,
                        "result": parsed,
                        "ok": not (
                            isinstance(parsed, Mapping) and parsed.get("ok") is False
                        ),
                    }
                )
    else:
        summary = (
            (summary + "\n\n" if summary else "")
            + f"[supervisor 达到最大轮数 {MAX_SUPERVISOR_LOOPS}，委派可能未完成]"
        )

    plan_summary = str(working.get("plan_summary") or "").strip() or task
    todos = [dict(todo) for todo in (working.get("todos") or [])]
    if not todos:
        # 模型一轮计划都没发：把任务本身当成一条待办，保证后续节点有东西可验证
        todos = [{"id": "t1", "content": task, "status": "pending", "note": ""}]
        persist_todos(runtime, todos, plan_summary=plan_summary)

    return {
        "plan_summary": plan_summary,
        "todos": todos,
        "acceptance_criteria": list(working.get("acceptance_criteria") or []),
        "verification_commands": list(working.get("verification_commands") or []),
        "max_attempts": int(state.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
        "passed": False,
        "messages": collected,
        "research_notes": str(working.get("research_notes") or ""),
        "sources": list(working.get("sources") or []),
        "agent_handoffs": list(working.get("agent_handoffs") or []),
        "code_agent_summary": str(working.get("code_agent_summary") or ""),
        "last_actor_summary": str(
            working.get("code_agent_summary")
            or working.get("last_actor_summary")
            or summary
        ),
        "context_next_node": "verifier",
    }


# --------------------------------------------------------------------------- #
# actor
# --------------------------------------------------------------------------- #
def compose_actor_prompt(state: Mapping[str, Any]) -> str:
    """ACTOR_PROMPT + 当前计划 + 验收标准 + 上一轮失败原因。"""
    sections: list[str] = [ACTOR_PROMPT.rstrip()]

    plan_summary = str(state.get("plan_summary") or "").strip()
    if plan_summary:
        sections.append(f"## Plan summary\n{plan_summary}")

    todos = state.get("todos") or []
    if todos:
        lines = []
        for todo in todos:
            note = str(todo.get("note") or "").strip()
            lines.append(
                f"- [{todo.get('status', 'pending')}] {todo.get('id')}: "
                f"{todo.get('content')}" + (f" (note: {note})" if note else "")
            )
        sections.append("## Todos\n" + "\n".join(lines))

    criteria = state.get("acceptance_criteria") or []
    if criteria:
        sections.append(
            "## Acceptance criteria\n" + "\n".join(f"- {item}" for item in criteria)
        )

    commands = state.get("verification_commands") or []
    if commands:
        sections.append(
            "## Verification commands (the verifier will run these)\n"
            + "\n".join(f"- {item}" for item in commands)
        )

    last_error = str(state.get("last_error") or "").strip()
    if last_error:
        sections.append(f"## Previous verification failure (fix this first)\n{last_error}")
        instruction = str(state.get("next_instruction") or "").strip()
        if instruction:
            sections.append(f"## Verifier instruction\n{instruction}")

    return "\n\n".join(sections)


def actor_node(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """执行 ReAct 循环：把计划变成实际的文件/命令改动。

    工具集 = `build_tools(runtime)` + `TodoUpdateTool`（后者只记录状态变更，
    由本节点合并回 `todos`）。

    Returns:
        `messages`（本轮新增的 AI/工具消息）、`last_actor_summary`、`todos`。
    """
    runtime = require_runtime(state)
    model = resolve_model(state, config)
    emit = resolve_emit(config)
    _emit_node_start(emit, "actor")

    task = str(state.get("task") or "").strip()
    if not task:
        raise ValueError("state['task'] 为空，actor 无任务可执行")

    tools = [*build_tools(runtime), build_todo_update_tool(runtime)]
    collected: list[BaseMessage] = []
    summary = ""
    stopped_reason = ""

    for event in stream_react_events(
        task,
        runtime=runtime,
        max_loops=MAX_ACTOR_LOOPS,
        model=model,
        tools=tools,
        system_prompt=compose_actor_prompt(state),
        message_sink=collected,
    ):
        if emit is not None:
            _forward_react_event(emit, "actor", event)
        if event.get("type") == "final_answer":
            summary = str(event.get("content") or "")
            stopped_reason = str(event.get("stopped_reason") or "")

    if stopped_reason == "max_loops":
        summary = f"{summary}\n\n[actor 达到最大轮数 {MAX_ACTOR_LOOPS}，任务可能未完成]".strip()

    updates = drain_todo_updates(runtime)
    todos = apply_todo_updates(state.get("todos"), updates)

    return {
        "messages": collected,
        "last_actor_summary": summary,
        "todos": todos,
        # 执行完就该验证了（context_monitor 会把上下文路由到 verifier）
        "context_next_node": "verifier",
    }


# --------------------------------------------------------------------------- #
# verifier
# --------------------------------------------------------------------------- #
def run_verification_commands(
    runtime: RuntimeState,
    commands: Sequence[str],
    *,
    emit: Emit | None = None,
    timeout_seconds: int | None = None,
    node: str = "verifier",
) -> list[dict[str, Any]]:
    """依次执行验收命令，返回 `VerificationResult` 形状的结果列表。"""
    results: list[dict[str, Any]] = []

    for raw_command in commands:
        command = str(raw_command).strip()
        if not command:
            continue

        try:
            outcome = execute_command(runtime, command, timeout_seconds=timeout_seconds)
            result: dict[str, Any] = {
                "command": outcome["command"],
                "ok": bool(outcome["ok"]),
                "exit_code": outcome["exit_code"],
                "stdout": outcome["stdout"],
                "stderr": outcome["stderr"],
            }
        except ToolException as exc:
            # 命令压根启动不了：也算一条失败结果，而不是让整张图崩掉
            result = {
                "command": command,
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": str(exc),
            }

        results.append(result)
        if emit is not None:
            emit(
                {
                    "type": "verification_command",
                    "node": node,
                    "command": result["command"],
                    "ok": result["ok"],
                    "exit_code": result["exit_code"],
                }
            )

    return results


def parse_verdict(raw: str) -> dict[str, Any]:
    """解析 verifier 的 JSON 结论；解析不出来时按"未通过"处理（fail-closed）。"""
    payload = extract_json_object(raw)
    if payload is None:
        return {
            "passed": False,
            "reason": "verifier 没有返回可解析的 JSON 结论",
            "checks": [],
            "recommended_next_instruction": _clip(raw, 300),
        }

    checks: list[dict[str, Any]] = []
    for item in payload.get("checks") or []:
        data = item if isinstance(item, Mapping) else {"name": str(item)}
        checks.append(
            {
                "name": str(data.get("name") or "check"),
                "passed": bool(data.get("passed")),
                "detail": str(data.get("detail") or "").strip(),
            }
        )

    return {
        "passed": bool(payload.get("passed")),
        "reason": str(payload.get("reason") or "").strip(),
        "checks": checks,
        "recommended_next_instruction": str(
            payload.get("recommended_next_instruction") or ""
        ).strip(),
    }


def _command_detail(result: Mapping[str, Any]) -> str:
    parts = [f"exit_code={result.get('exit_code')}"]
    stderr = str(result.get("stderr") or "").strip()
    stdout = str(result.get("stdout") or "").strip()
    if stderr:
        parts.append(f"stderr: {_clip(stderr, 300)}")
    elif stdout:
        parts.append(f"stdout: {_clip(stdout, 300)}")
    else:
        parts.append("no output")
    return "；".join(parts)


def _format_failure(
    verdict: Mapping[str, Any],
    failed_commands: Sequence[Mapping[str, Any]],
) -> str:
    lines: list[str] = []

    reason = str(verdict.get("reason") or "").strip()
    if reason:
        lines.append(f"原因：{reason}")

    for check in verdict.get("checks") or []:
        if isinstance(check, Mapping) and not check.get("passed"):
            detail = str(check.get("detail") or "").strip()
            lines.append(
                f"- 未通过检查：{check.get('name')}"
                + (f"（{_clip(detail, 200)}）" if detail else "")
            )

    for result in failed_commands:
        lines.append(
            f"- 验收命令失败（exit_code={result.get('exit_code')}）：{result.get('command')}"
        )
        stderr = str(result.get("stderr") or "").strip()
        if stderr:
            lines.append(f"  stderr: {_clip(stderr, 300)}")

    instruction = str(verdict.get("recommended_next_instruction") or "").strip()
    if instruction:
        lines.append(f"建议下一步：{instruction}")

    return "\n".join(lines) or "验证未通过，但未给出具体原因"


def compose_verifier_brief(
    state: Mapping[str, Any],
    command_results: Sequence[Mapping[str, Any]],
) -> str:
    """给 verifier 的输入：计划 + 验收标准 + 验证命令结果 + 最近 actor 输出。"""
    sections: list[str] = []

    task = str(state.get("task") or "").strip()
    if task:
        sections.append(f"## Task\n{task}")

    plan_summary = str(state.get("plan_summary") or "").strip()
    if plan_summary:
        sections.append(f"## Plan summary\n{plan_summary}")

    todos = state.get("todos") or []
    if todos:
        lines = [
            f"- [{todo.get('status', 'pending')}] {todo.get('id')}: {todo.get('content')}"
            for todo in todos
        ]
        sections.append("## Todos\n" + "\n".join(lines))

    criteria = state.get("acceptance_criteria") or []
    if criteria:
        sections.append(
            "## Acceptance criteria\n" + "\n".join(f"- {item}" for item in criteria)
        )

    commands = state.get("verification_commands") or []
    if commands:
        sections.append(
            "## Verification commands\n" + "\n".join(f"- {item}" for item in commands)
        )

    if command_results:
        lines = []
        for result in command_results:
            lines.append(
                f"- `{result.get('command')}` → exit_code={result.get('exit_code')}, "
                f"ok={bool(result.get('ok'))}"
            )
            output = str(result.get("stderr") or result.get("stdout") or "").strip()
            if output:
                lines.append(f"  output: {_clip(output, 600)}")
        sections.append(
            "## Verification command results (already executed by the runtime)\n"
            + "\n".join(lines)
        )
    else:
        sections.append(
            "## Verification command results\n(no verification commands were configured)"
        )

    summary = str(state.get("last_actor_summary") or "").strip()
    if summary:
        sections.append(f"## Latest actor summary\n{summary}")

    return "\n\n".join(sections)


def verifier_node(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """先跑验收命令，再让只读 agent 判断任务是否真的完成。

    判定规则（fail-closed）：**LLM 说通过 且 所有验收命令都成功**，才算 passed。
    验收命令是客观证据，允许它否决模型的乐观结论。

    Returns:
        `passed` / `attempts`(+1) / `verification_results` / `verification_checks`
        / `next_instruction` / `messages`，失败时还有 `last_error`。
    """
    runtime = require_runtime(state)
    model = resolve_model(state, config)
    emit = resolve_emit(config)
    _emit_node_start(emit, "verifier")

    command_results = run_verification_commands(
        runtime,
        state.get("verification_commands") or [],
        emit=emit,
    )

    collected: list[BaseMessage] = []
    raw = ""
    for event in stream_react_events(
        compose_verifier_brief(state, command_results),
        runtime=runtime,
        max_loops=MAX_VERIFIER_LOOPS,
        model=model,
        tools=build_read_only_tools(runtime),
        system_prompt=VERIFIER_PROMPT,
        message_sink=collected,
    ):
        if emit is not None:
            _forward_react_event(emit, "verifier", event)
        if event.get("type") == "final_answer":
            raw = str(event.get("content") or "")

    verdict = parse_verdict(raw)
    failed_commands = [result for result in command_results if not result["ok"]]
    passed = bool(verdict["passed"]) and not failed_commands

    checks = [
        *verdict["checks"],
        *[
            {
                "name": f"command: {result['command']}",
                "passed": bool(result["ok"]),
                "detail": _command_detail(result),
            }
            for result in command_results
        ],
    ]

    attempts = int(state.get("attempts") or 0) + 1
    max_attempts = int(state.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    exhausted = attempts >= max_attempts

    todos = [dict(todo) for todo in (state.get("todos") or [])]
    updates: dict[str, Any] = {
        "passed": passed,
        "attempts": attempts,
        "verification_results": command_results,
        "verification_checks": checks,
        "next_instruction": verdict["recommended_next_instruction"],
        "messages": collected,
        "todos": todos,
    }

    if passed:
        for todo in todos:
            todo["status"] = "completed"
        updates["last_error"] = ""
        updates["context_next_node"] = FINAL
    else:
        updates["last_error"] = _format_failure(verdict, failed_commands)
        # 失败就回炉重新规划（context_monitor_route 在没通过时会把流程送去这里）。
        # 注意：试满 max_attempts 时必须写 FINAL —— `verifier_route` 里有这个兜底，
        # 但 `context_monitor_route` 没有（它只认 passed/压缩/context_next_node），
        # 所以像 build_complex_workflow 那种"verifier → context_monitor"的拓扑，
        # 靠这里的 FINAL 才能收敛，否则会 planner ↔ verifier 无限打转。
        updates["context_next_node"] = FINAL if exhausted else "planner"

    return updates


def verifier_route(state: MeoGraphState) -> str:
    """验证后的路由：通过、或没机会了就收尾，否则回到 planner 重新规划。"""
    if state.get("passed"):
        return FINAL

    attempts = int(state.get("attempts") or 0)
    max_attempts = int(state.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    if attempts >= max_attempts:
        return FINAL

    return "planner"


# --------------------------------------------------------------------------- #
# final
# --------------------------------------------------------------------------- #
def compose_final_report(state: Mapping[str, Any]) -> str:
    """把 passed / failed 的状态格式化成 final_answer 文本（不依赖模型）。

    只用 ASCII 标记（[OK] / [FAIL]），保证在 GBK 控制台也能直接 print。
    """
    passed = bool(state.get("passed"))
    task = str(state.get("task") or "").strip()
    attempts = int(state.get("attempts") or 0)
    max_attempts = int(state.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)

    lines: list[str] = []
    if task:
        lines.append(f"任务：{task}")
    lines.append(
        f"结果：{'已完成并通过验证' if passed else '未通过验证'}"
        f"（尝试 {attempts}/{max_attempts} 轮）"
    )

    summary = str(state.get("last_actor_summary") or "").strip()
    if summary:
        lines.append(f"\n执行摘要：\n{summary}")

    todos = state.get("todos") or []
    if todos:
        lines.append("\n待办状态：")
        lines.extend(
            f"- [{todo.get('status', 'pending')}] {todo.get('id')}: {todo.get('content')}"
            for todo in todos
        )

    checks = state.get("verification_checks") or []
    if checks:
        lines.append("\n验证结论：")
        lines.extend(
            f"- {'[OK]' if check.get('passed') else '[FAIL]'} {check.get('name')}"
            + (
                f"：{_clip(str(check.get('detail') or ''), 200)}"
                if check.get("detail")
                else ""
            )
            for check in checks
        )

    results = state.get("verification_results") or []
    if results:
        lines.append("\n验收命令：")
        lines.extend(
            f"- exit_code={result.get('exit_code')} "
            f"{'OK' if result.get('ok') else 'FAILED'}: {result.get('command')}"
            for result in results
        )

    if not passed:
        last_error = str(state.get("last_error") or "").strip()
        if last_error:
            lines.append(f"\n未通过原因：\n{last_error}")
        instruction = str(state.get("next_instruction") or "").strip()
        if instruction:
            lines.append(f"\n下一步建议：{instruction}")

    return "\n".join(lines)


def final_node(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """收尾节点：把状态整理成 `final_answer`。

    - 默认是**确定性**格式化（`compose_final_report`），所以离线、没有 API Key
      也能正常收尾。
    - 如果能解析到模型（`config["configurable"]["model"]` → `runtime.metadata["model"]`
      → `create_model()`），会先用 `FINAL_PROMPT` 让模型写一段面向用户的总结；
      任何异常都回退到确定性文本——收尾失败不该让整张图崩掉。
    - `config["configurable"]["summarize_final"] = False` 可强制只用确定性文本。

    Returns:
        `final_answer` 与 `passed`（原样透传，便于外层判断退出码）。
    """
    report = compose_final_report(state)
    passed = bool(state.get("passed"))

    emit = resolve_emit(config)
    _emit_node_start(emit, "final")

    configurable = (config or {}).get("configurable") or {}
    if configurable.get("summarize_final", True):
        try:
            model = resolve_model(state, config)
            response = model.invoke(
                [SystemMessage(content=FINAL_PROMPT), HumanMessage(content=report)]
            )
            text = message_text(getattr(response, "content", "")).strip()
            if text:
                report = text
        except Exception:  # noqa: BLE001 - 总结是尽力而为，失败就退回确定性文本
            pass

    return {"final_answer": report, "passed": passed}


# --------------------------------------------------------------------------- #
# 上下文监控
# --------------------------------------------------------------------------- #
def estimate_context_tokens(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
    *,
    memory_payload: BaseMessage | None = None,
) -> int:
    """估算"当前消息 + 记忆快照"的 token 数。

    优先用模型自带的分词器（`get_num_tokens_from_messages`，ChatOpenAI 走 tiktoken）；
    任何异常（没配 Key、模型不支持、分词器下载失败……）都退回 `len(text) // 4`，
    因为这个值只用来决定"要不要压缩"，不值得让它把图跑挂。
    """
    messages: list[BaseMessage] = list(state.get("messages") or [])
    if memory_payload is not None:
        messages = [*messages, memory_payload]

    try:
        model = resolve_model(state, config)
        return int(model.get_num_tokens_from_messages(messages))
    except Exception:  # noqa: BLE001 - 见 docstring
        text = "\n".join(message_text(getattr(item, "content", "")) for item in messages)
        return len(text) // 4


def context_monitor_node(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """监控上下文规模，决定"继续走"还是"先压缩"。

    1. 用 `estimate_context_tokens()` 估算 `messages + 记忆快照` 的 token 数；
    2. 与 `context_token_limit`（默认 400000）比较，得出 `context_should_compress`；
    3. `context_next_node` 由上游节点设置（verifier 失败后是 `"planner"`），
       拿不到就用默认 `"verifier"`。

    Returns:
        `context_token_count` / `context_should_compress` / `context_next_node`，
        另外顺带写 `memory_snapshot`（RULES_LAYER 要求"记忆由运行时组装"，
        这里就是组装点，图状态里因此总能拿到最近一份快照）。

    注意：本节点已接进两种拓扑：`build_workflow()`（verifier 失败重试的分支）与
    `build_complex_workflow()`（planner 之后、verifier 之后都会经过）。
    """
    emit = resolve_emit(config)
    _emit_node_start(emit, "context_monitor")

    memory = build_layered_memory(state, node="context_monitor")
    memory_payload = SystemMessage(
        content="[layered memory]\n" + format_layered_memory_for_prompt(memory)
    )

    token_count = estimate_context_tokens(state, config, memory_payload=memory_payload)
    limit = int(state.get("context_token_limit") or DEFAULT_CONTEXT_TOKEN_LIMIT)
    should_compress = token_count > limit

    update: dict[str, Any] = {
        "context_token_count": token_count,
        "context_token_limit": limit,
        "context_should_compress": should_compress,
        "context_next_node": str(state.get("context_next_node") or DEFAULT_NEXT_NODE),
        "memory_snapshot": memory,
    }

    if emit is not None:
        emit(
            {
                "type": "context_usage",
                "node": "context_monitor",
                "token_count": token_count,
                "token_limit": limit,
                "should_compress": should_compress,
                "next_node": update["context_next_node"],
            }
        )

    return update


def context_monitor_route(state: MeoGraphState) -> str:
    """上下文监控后的路由：已通过就收尾，该压缩就压缩，否则回到上游指定的节点。"""
    if state.get("passed"):
        return FINAL
    if state.get("context_should_compress"):
        return CONTEXT_COMPRESSOR_NODE
    return str(state.get("context_next_node") or DEFAULT_NEXT_NODE)


# --------------------------------------------------------------------------- #
# 上下文压缩
# --------------------------------------------------------------------------- #
#: 压缩摘要各字段的字符上限
COMPRESSION_FIELD_LIMITS: dict[str, int] = {
    "summary": 2200,
    "active_goal": 600,
    "completed_work": 1200,
    "open_todos": 1200,
    "important_files": 800,
    "tool_findings": 1600,
    "sources": 1200,
    "next_steps": 1000,
    "risks": 800,
}
#: 压缩后写回 `research_notes` 的上限
COMPRESSED_NOTES_LIMIT = 1600
#: 压缩后保留的 sources 条数
MAX_COMPRESSED_SOURCES = 20
#: 状态里最多保留多少条压缩事件（记忆层只带最近 3 条）
MAX_COMPRESSION_EVENTS = 20
#: 送进压缩提示词的转录预算
TRANSCRIPT_PER_MESSAGE_CHARS = 1500
TRANSCRIPT_TOTAL_CHARS = 24000

#: 摘要里各段的展示顺序与标题
_DIGEST_SECTIONS: tuple[tuple[str, str], ...] = (
    ("active_goal", "Active goal"),
    ("completed_work", "Completed work"),
    ("open_todos", "Open todos"),
    ("important_files", "Important files"),
    ("tool_findings", "Tool findings"),
    ("sources", "Sources"),
    ("next_steps", "Next steps"),
    ("risks", "Risks"),
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text_block(value: Any) -> str:
    """把字符串/列表/字典统一渲染成多行文本（todos、files、sources 都能吃）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return "\n".join(f"- {key}: {item}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        lines: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                head = " ".join(
                    str(part)
                    for part in (
                        item.get("title") or item.get("name") or item.get("id") or "",
                        item.get("url") or item.get("link") or "",
                    )
                    if part
                )
                detail = str(
                    item.get("detail") or item.get("note") or item.get("content") or ""
                ).strip()
                lines.append(f"- {head or item}" + (f"：{detail}" if detail else ""))
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)
    return str(value)


def _render_transcript(
    messages: Sequence[BaseMessage],
    *,
    per_message: int = TRANSCRIPT_PER_MESSAGE_CHARS,
    total: int = TRANSCRIPT_TOTAL_CHARS,
) -> str:
    """把消息历史渲染成有界的纯文本，喂给压缩提示词。"""
    lines = [
        f"[{type(message).__name__}] "
        f"{short_text(message_text(getattr(message, 'content', '')), per_message)}".rstrip()
        for message in messages
    ]
    return short_text("\n".join(lines), total)


def _render_compression_digest(digest: Mapping[str, Any]) -> str:
    """把压缩 JSON 渲染成可读的续跑摘要（9 个字段一个不丢）。"""
    parts: list[str] = []
    summary = short_text(str(digest.get("summary") or ""), COMPRESSION_FIELD_LIMITS["summary"])
    if summary:
        parts.append(summary)

    for key, title in _DIGEST_SECTIONS:
        text = short_text(
            _as_text_block(digest.get(key)), COMPRESSION_FIELD_LIMITS.get(key, 800)
        )
        if text:
            parts.append(f"## {title}\n{text}")

    return "\n\n".join(parts)


def _coerce_sources(value: Any) -> list[dict[str, str]]:
    """把压缩 JSON 里的 sources 规范成 `[{title, url}]`。"""
    items = value if isinstance(value, (list, tuple)) else [value] if value else []
    sources: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, Mapping):
            title = str(item.get("title") or item.get("name") or "").strip()
            url = str(item.get("url") or item.get("link") or "").strip()
        else:
            title, url = short_text(str(item).strip(), 200), ""
        if not title and not url:
            continue
        sources.append({"title": short_text(title, 200), "url": short_text(url, 300)})
        if len(sources) >= MAX_COMPRESSED_SOURCES:
            break
    return sources


def _fallback_digest(state: MeoGraphState) -> dict[str, Any]:
    """LLM 不可用时的确定性兜底：信息比 LLM 少，但绝不会失败。"""
    todos = [todo for todo in (state.get("todos") or []) if isinstance(todo, Mapping)]
    completed = [
        f"{todo.get('id')} {todo.get('content', '')}"
        for todo in todos
        if str(todo.get("status")) == "completed"
    ]
    open_todos = [
        f"{todo.get('id')} [{todo.get('status', 'pending')}] {todo.get('content', '')}"
        for todo in todos
        if str(todo.get("status") or "pending") != "completed"
    ]

    summary_parts: list[str] = []
    task = str(state.get("task") or "").strip()
    if task:
        summary_parts.append(f"任务：{task}")
    plan_summary = str(state.get("plan_summary") or "").strip()
    if plan_summary:
        summary_parts.append(f"计划：{plan_summary}")
    summary_parts.append(
        f"尝试：{_safe_int(state.get('attempts'))}/{_safe_int(state.get('max_attempts'), DEFAULT_MAX_ATTEMPTS)}"
    )
    last_error = str(state.get("last_error") or "").strip()
    if last_error:
        summary_parts.append(f"最近失败：{last_error}")

    return {
        "summary": "\n".join(summary_parts),
        "active_goal": plan_summary or task,
        "completed_work": "\n".join(completed),
        "open_todos": "\n".join(open_todos),
        "important_files": "",
        "tool_findings": str(state.get("research_notes") or ""),
        "sources": list(state.get("sources") or []),
        "next_steps": str(state.get("next_instruction") or ""),
        "risks": last_error,
    }


def _digest_has_content(digest: Mapping[str, Any]) -> bool:
    """LLM 可能返回"合法 JSON 但字段全不对"（比如把 verifier 的结论丢过来）。

    那种情况按"压缩失败"处理，交给确定性兜底，否则会产出一份空摘要。
    """
    if str(digest.get("summary") or "").strip():
        return True
    return any(
        str(_as_text_block(digest.get(key)) or "").strip() for key, _ in _DIGEST_SECTIONS
    )


def _compress_digest(
    state: MeoGraphState,
    config: RunnableConfig | None,
    messages: Sequence[BaseMessage],
    memory: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """让 LLM 压缩转录；返回 `(digest, 是否走了 LLM)`。"""
    payload = (
        "## Layered memory\n"
        + format_layered_memory_for_prompt(memory)
        + "\n\n## Transcript to compress\n"
        + _render_transcript(messages)
    )
    try:
        model = resolve_model(state, config)
        response = model.invoke(
            [
                SystemMessage(content=CONTEXT_COMPRESSION_PROMPT),
                HumanMessage(content=payload),
            ]
        )
        digest = extract_json_object(message_text(getattr(response, "content", "")))
        if isinstance(digest, Mapping) and _digest_has_content(digest):
            return dict(digest), True
    except Exception:  # noqa: BLE001 - 压缩不能把图跑挂，失败就用确定性兜底
        pass
    return _fallback_digest(state), False


def context_compressor_node(
    state: MeoGraphState,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """把长上下文压成"可续跑摘要"，并替换消息历史。

    1. 让 LLM 按 `CONTEXT_COMPRESSION_PROMPT` 产出 9 个字段的 JSON
       （失败/没模型时用确定性兜底摘要，绝不抛异常）；
    2. 用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 清空旧消息，再放一条 `AIMessage(摘要)`
       作为新的上下文起点（`add_messages` reducer 支持这个语义）；
    3. 摘要落盘 `HISTORY_SUMMARY.md`，并往 `compression_events` 追加一条记录；
    4. 各字段按上限截断后写回状态（`research_notes` / `sources` / `agent_handoffs` …）。

    Returns:
        `messages`（RemoveMessage + AIMessage）、`context_summary`、`history_summary`、
        `context_token_count`（压缩后的新值）、`context_should_compress=False`、
        `context_next_node`、`research_notes`、`sources`、`agent_handoffs`、
        `compression_events`、`memory_snapshot`。
        返回值里的键都在 `MeoGraphState` 里声明过（否则 LangGraph 会拒绝这次更新）。
    """
    runtime = require_runtime(state)
    emit = resolve_emit(config)
    _emit_node_start(emit, "context_compressor")

    messages = list(state.get("messages") or [])
    tokens_before = _safe_int(state.get("context_token_count"))
    memory = build_layered_memory(state, node="context_compressor")
    digest, used_llm = _compress_digest(state, config, messages, memory)

    summary = short_text(
        str(digest.get("summary") or "").strip(), COMPRESSION_FIELD_LIMITS["summary"]
    )
    digest_text = _render_compression_digest(digest)
    compressed_message = AIMessage(content=summary or "（上下文已压缩）")
    new_messages: list[BaseMessage] = [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        compressed_message,
    ]

    # 压缩后的 token 数：只算新的那一条消息 + 记忆快照
    memory_message = SystemMessage(
        content="[layered memory]\n" + format_layered_memory_for_prompt(memory)
    )
    tokens_after = estimate_context_tokens(
        {**state, "messages": [compressed_message]},  # type: ignore[arg-type]
        config,
        memory_payload=memory_message,
    )

    history_path = None
    if digest_text or summary:
        history_path = append_history_summary(
            runtime, digest_text or summary, heading="Compression"
        )

    event: dict[str, Any] = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "node": "context_compressor",
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "dropped_messages": len(messages),
        "summary": summary,
        "used_llm": used_llm,
        "history_path": history_path,
    }
    events = [
        *list(state.get("compression_events") or []),
        event,
    ][-MAX_COMPRESSION_EVENTS:]

    research_notes_parts: list[str] = []
    existing_notes = str(state.get("research_notes") or "").strip()
    if existing_notes:
        research_notes_parts.append(existing_notes)
    findings = _as_text_block(digest.get("tool_findings"))
    if findings:
        research_notes_parts.append(f"## Tool findings\n{findings}")
    research_notes = short_text("\n\n".join(research_notes_parts), COMPRESSED_NOTES_LIMIT)

    update: dict[str, Any] = {
        "messages": new_messages,
        "context_summary": digest_text or summary,
        "history_summary": summary,
        "context_token_count": tokens_after,
        "context_should_compress": False,
        "context_next_node": str(state.get("context_next_node") or DEFAULT_NEXT_NODE),
        "research_notes": research_notes,
        "sources": _coerce_sources(digest.get("sources")),
        "agent_handoffs": trim_handoffs(state.get("agent_handoffs")),
        "compression_events": events,
        "memory_snapshot": memory,
    }

    if emit is not None:
        emit(
            {
                "type": "context_compressed",
                "node": "context_compressor",
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "dropped_messages": len(messages),
                "used_llm": used_llm,
                "history_path": history_path,
            }
        )

    return update
