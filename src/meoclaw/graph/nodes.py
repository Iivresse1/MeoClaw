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
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, ToolException

from meoclaw.agents.code_agent import CODE_AGENT_NAME, run_code_agent
from meoclaw.agents.search_agent import SEARCH_AGENT_NAME, run_search_agent
from meoclaw.core.context import Emit, require_runtime, resolve_emit, resolve_model
from meoclaw.core.context_builder import ContextBuilder, tag_message_context
from meoclaw.core.react import (
    message_text,
    serialize_tool_result,
    stream_react_events,
)
from meoclaw.core.sandbox import build_sandbox_shell_middleware, sandbox_mode
from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import append_history_summary, summarize_task_for_history
from meoclaw.graph.state import MeoGraphState
from meoclaw.prompts import (
    ACTOR_PROMPT,
    FINAL_PROMPT,
    PLANNER_PROMPT,
    SUPERVISOR_PROMPT,
    VERIFIER_PROMPT,
)
from meoclaw.tools.bash_tool import execute_command
from meoclaw.tools.registry import build_read_only_tools, build_tools
from meoclaw.tools.todo_tool import (
    TodoWriteInput,
    TodoWriteTool,
    TodoWriteToolName,
    apply_todo_updates,
    build_todo_update_tool,
    drain_todo_updates,
    normalize_plan,
    persist_todos,
)

_CONTEXT_BUILDER = ContextBuilder()

__all__ = [
    "CALL_CODE_AGENT_TOOL",
    "CALL_SEARCH_AGENT_TOOL",
    "DEFAULT_MAX_ATTEMPTS",
    "FINAL",
    "MAX_ACTOR_LOOPS",
    "MAX_SUPERVISOR_LOOPS",
    "MAX_VERIFIER_LOOPS",
    "PlannerError",
    "actor_node",
    "build_todo_update_tool",
    "compose_final_report",
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
    runtime = require_runtime(state)
    model = resolve_model(state, config)
    emit = resolve_emit(config)
    _emit_node_start(emit, "planner")

    task = str(state.get("task") or "").strip()
    if not task:
        raise PlannerError("state['task'] 为空，无法制定计划")

    task_id = str((runtime.metadata or {}).get("task_id") or "")
    bundle = _CONTEXT_BUILDER.build(
        "planner", state, PLANNER_PROMPT, task_id=task_id
    )

    planner = model.bind_tools([TodoWriteTool])
    response = planner.invoke(
        [SystemMessage(content=bundle.system_prompt), *bundle.messages]
    )
    plan = extract_plan(response)

    # 兜底：模型没给出可用待办时，至少把任务本身当成一条待办，保证 actor 有活干
    todos = plan["todos"] or [
        {"id": "t1", "content": task, "status": "pending", "note": ""}
    ]

    plan_text = json.dumps(plan, ensure_ascii=False, indent=2)
    planner_message = tag_message_context(
        AIMessage(content=plan_text),
        role="planner",
        task_id=task_id,
        visible_to=["planner", "actor", "verifier", "final"],
    )
    return {
        "messages": [planner_message],
        "plan_summary": plan["plan_summary"] or task,
        "todos": todos,
        "acceptance_criteria": plan["acceptance_criteria"],
        "verification_commands": plan["verification_commands"],
        "max_attempts": int(state.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
        "passed": False,
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

    def call_todo_write(
        plan_summary: str,
        todos: Sequence[Mapping[str, Any]],
        acceptance_criteria: Sequence[str] | None = None,
        verification_commands: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        plan = normalize_plan(
            {
                "plan_summary": plan_summary,
                "todos": todos,
                "acceptance_criteria": acceptance_criteria,
                "verification_commands": verification_commands,
            }
        )
        if not plan["todos"]:
            raise ValueError("todos 不能为空")
        working["plan_summary"] = plan["plan_summary"]
        working["todos"] = plan["todos"]
        working["acceptance_criteria"] = plan["acceptance_criteria"]
        working["verification_commands"] = plan["verification_commands"]
        persist_todos(
            require_runtime(working),
            plan["todos"],
            plan_summary=plan["plan_summary"],
        )
        if emit is not None:
            emit({"type": "plan_snapshot", "node": "planner", **plan})
        return {"ok": True, **plan}

    return [
        StructuredTool.from_function(
            func=call_todo_write,
            name=TodoWriteToolName,
            description=(
                "发布或修订执行计划，包含 plan_summary、todos、"
                "acceptance_criteria 与 verification_commands。"
            ),
            args_schema=TodoWriteInput,
            handle_validation_error=True,
        ),
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
    tools = _build_supervisor_tools(working, emit)
    task_id = str((runtime.metadata or {}).get("task_id") or "")
    bundle = _CONTEXT_BUILDER.build(
        "planner",
        working,
        SUPERVISOR_PROMPT,
        task_id=task_id,
        instruction=task,
    )
    collected: list[BaseMessage] = []
    summary = ""
    stopped_reason = ""
    for event in stream_react_events(
        task,
        runtime=runtime,
        max_loops=MAX_SUPERVISOR_LOOPS,
        model=model,
        tools=tools,
        system_prompt=bundle.system_prompt,
        input_messages=bundle.messages,
        role="planner",
        task_id=task_id,
        message_sink=collected,
    ):
        if emit is not None:
            _forward_react_event(emit, "planner", event)
        if event.get("type") == "final_answer":
            summary = str(event.get("content") or "")
            stopped_reason = str(event.get("stopped_reason") or "")
    if stopped_reason == "max_loops":
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
    }


# --------------------------------------------------------------------------- #
# actor
# --------------------------------------------------------------------------- #
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

    task_id = str((runtime.metadata or {}).get("task_id") or "")
    bundle = _CONTEXT_BUILDER.build("actor", state, ACTOR_PROMPT, task_id=task_id)
    kernel = sandbox_mode(runtime)
    use_docker = kernel == "docker"
    tools = [
        *build_tools(runtime, include_bash=not use_docker),
        build_todo_update_tool(runtime),
    ]
    extra_middleware = [build_sandbox_shell_middleware(runtime)] if use_docker else []
    collected: list[BaseMessage] = []
    summary = ""
    stopped_reason = ""

    for event in stream_react_events(
        task,
        runtime=runtime,
        max_loops=MAX_ACTOR_LOOPS,
        model=model,
        tools=tools,
        system_prompt=bundle.system_prompt,
        input_messages=bundle.messages,
        role="actor",
        task_id=task_id,
        extra_middleware=extra_middleware,
        message_sink=collected,
        stream_tokens=True,
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
    task_id = str((runtime.metadata or {}).get("task_id") or "")
    verifier_state = {**state, "verification_results": command_results}
    bundle = _CONTEXT_BUILDER.build(
        "verifier", verifier_state, VERIFIER_PROMPT, task_id=task_id
    )

    collected: list[BaseMessage] = []
    raw = ""
    for event in stream_react_events(
        str(state.get("task") or "验证当前任务"),
        runtime=runtime,
        max_loops=MAX_VERIFIER_LOOPS,
        model=model,
        tools=build_read_only_tools(runtime),
        system_prompt=bundle.system_prompt,
        input_messages=bundle.messages,
        role="verifier",
        task_id=task_id,
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
    else:
        updates["last_error"] = _format_failure(verdict, failed_commands)

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
    - 节点退出前由运行时把任务结算摘要追加到 `HISTORY_SUMMARY.md`；
      下一任务/重启后的 ContextBuilder 会读取它，形成长期记忆闭环。

    Returns:
        `final_answer` 与 `passed`（原样透传，便于外层判断退出码）。
    """
    report = compose_final_report(state)
    passed = bool(state.get("passed"))
    runtime = require_runtime(state)

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
        except Exception:  # noqa: BLE001, S110 - 总结失败时使用确定性文本
            pass

    task_id = str((runtime.metadata or {}).get("task_id") or "")
    append_history_summary(
        runtime,
        summarize_task_for_history(state),
        heading=f"Task {task_id[:12]}" if task_id else "Task result",
        entry_id=task_id,
    )
    final_message = tag_message_context(
        AIMessage(content=report),
        role="final",
        task_id=task_id,
        visible_to=["planner", "actor", "verifier", "final"],
    )
    return {"final_answer": report, "passed": passed, "messages": [final_message]}
