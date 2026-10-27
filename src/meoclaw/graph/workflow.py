"""把四个节点组装成一张可执行的 LangGraph。

    START → planner → actor → verifier → ┬─ passed 或 attempts 用尽 → final → END
                     ↑                   │
                     └──── planner ←─────┘   未通过且还有机会

用法：

    workflow = build_workflow()
    result = workflow.invoke(
        initial_state("给 utils.py 加 add(a, b) 并写测试", workspace="./workspace"),
        {"configurable": {"emit": print}},      # 可选：实时事件回调
    )
    print(result["final_answer"])
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from meoclaw.core.context_builder import tag_message_context
from meoclaw.core.state import RuntimeState
from meoclaw.graph.nodes import (
    DEFAULT_MAX_ATTEMPTS,
    FINAL,
    actor_node,
    final_node,
    planner_node,
    responder_node,
    supervisor_node,
    verifier_node,
    verifier_route,
)
from meoclaw.graph.state import MeoGraphState

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from langgraph.graph.state import CompiledStateGraph

__all__ = ["build_complex_workflow", "build_workflow", "initial_state"]


def _start_route(state: MeoGraphState) -> str:
    return "responder" if state.get("workflow_kind") == "answer" else "planner"


def build_workflow(checkpointer: Any = None) -> CompiledStateGraph:
    """组装并编译 planner → actor → verifier → final/planner 的图。

    Returns:
        已编译的 LangGraph（`invoke` / `stream` 都能用）。
        节点名与路由函数的返回值一一对应。

    `verifier_route` 保留 `attempts >= max_attempts → final` 的兜底。
    """
    graph = StateGraph(MeoGraphState)  #StateGraph 是主要的图类，以用户定义的 State 对象作为类型参数。

    #----构建图时，先定义状态，再添加节点和边，最后编译----#

    graph.add_node("planner", planner_node)
    graph.add_node("responder", responder_node)
    graph.add_node("actor", actor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("final", final_node)

    graph.add_conditional_edges(
        START,
        _start_route,
        {"responder": "responder", "planner": "planner"},
    )
    graph.add_edge("responder", END)
    graph.add_edge("planner", "actor")
    graph.add_edge("actor", "verifier")
    graph.add_conditional_edges(
        "verifier",
        verifier_route,
        {FINAL: "final", "planner": "planner"},
    )
    graph.add_edge("final", END)

    return graph.compile(checkpointer=checkpointer)


def build_complex_workflow(checkpointer: Any = None) -> CompiledStateGraph:
    """完整的 supervisor 拓扑：planner 直接面对 verifier，**没有 actor 节点**。

        START → planner(supervisor) → verifier → final/planner

    与 `build_workflow()` 的区别：

    - 没有 actor 节点 —— planner 以 supervisor 形态运行（`supervisor_node`），
      通过 `CallSearchAgentTool` / `CallCodeAgentTool` 把研究与实现委派给专家 Agent；
    - `verifier_route` 负责通过、重试和试满后的收敛。
    """
    graph = StateGraph(MeoGraphState)

    graph.add_node("planner", supervisor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "verifier")
    graph.add_conditional_edges(
        "verifier",
        verifier_route,
        {FINAL: "final", "planner": "planner"},
    )
    graph.add_edge("final", END)

    return graph.compile(checkpointer=checkpointer)


def initial_state(
    task: str,
    *,
    workspace: str | Path | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    runtime: RuntimeState | None = None,
    model: Any = None,
    task_id: str | None = None,
    session_id: str | None = None,
    sandbox_mode: str | None = None,
    workflow_kind: str = "task",
) -> dict[str, Any]:
    """构造调用图所需的初始状态（顺带创建/校验 workspace）。

    Args:
        task: 用户任务描述。
        workspace: 工作区目录；与 `runtime` 二选一。
        max_attempts: 最大尝试轮数（verifier 每跑一次算一轮）。
        runtime: 复用已有的 `RuntimeState`。
        model: 可选注入模型（等价于写进 `runtime.metadata["model"]`），方便测试与复用。

    Returns:
        可直接喂给 `workflow.invoke()` 的初始状态字典。

    Raises:
        ValueError: task 为空，或 workspace / runtime 都没给。
    """
    task = (task or "").strip()
    if not task:
        raise ValueError("task 不能为空")

    if runtime is None:
        if workspace is None:
            raise ValueError("必须提供 workspace 或 runtime 之一")
        runtime = RuntimeState(workspace=workspace)

    if model is not None:
        runtime.metadata["model"] = model
    task_id = str(task_id or uuid4().hex)
    session_id = str(session_id or runtime.metadata.get("session_id") or task_id)
    runtime.metadata["task_id"] = task_id
    runtime.metadata["session_id"] = session_id
    runtime.metadata["thread_id"] = session_id
    if sandbox_mode:
        runtime.metadata["sandbox"] = sandbox_mode
    user_message = tag_message_context(
        HumanMessage(content=task),
        role="planner",
        task_id=task_id,
        visible_to=["planner", "actor", "verifier", "final"],
    )

    return {
        "task": task,
        "workflow_kind": workflow_kind if workflow_kind in {"answer", "task"} else "task",
        "runtime": runtime,
        "messages": [user_message],
        "max_attempts": max(1, int(max_attempts)),
        "attempts": 0,
        "todos": [],
        "verification_results": [],
        "verification_checks": [],
        "passed": False,
        "last_error": "",
        "next_instruction": "",
    }
