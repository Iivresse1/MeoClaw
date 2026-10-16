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

from langgraph.graph import END, START, StateGraph

from meoclaw.core.state import RuntimeState
from meoclaw.graph.nodes import (
    CONTEXT_COMPRESSOR_NODE,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_NEXT_NODE,
    FINAL,
    actor_node,
    context_compressor_node,
    context_monitor_node,
    context_monitor_route,
    final_node,
    planner_node,
    supervisor_node,
    verifier_node,
    verifier_route,
)
from meoclaw.graph.state import MeoGraphState

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from langgraph.graph.state import CompiledStateGraph

__all__ = ["build_complex_workflow", "build_workflow", "context_compressor_route", "initial_state"]


def build_workflow() -> "CompiledStateGraph":
    """组装并编译 planner → actor → verifier →（必要时压缩）→ final/planner 的图。

    Returns:
        已编译的 LangGraph（`invoke` / `stream` 都能用）。
        节点名与路由函数的返回值一一对应。

    路由设计：
    - `verifier_route` 保留 `attempts >= max_attempts → final` 的兜底，负责"还要不要重试"；
    - 只有在"要重试"（`"planner"`）这条分支上才经过 `context_monitor`：
      它判断上下文是否过大 → 需要就先 `context_compressor` 压缩，再回到 planner。
      这样成功路径（→ final）完全不受影响，也避免压缩节点被反复触发。
    """
    graph = StateGraph(MeoGraphState)  #StateGraph 是主要的图类，以用户定义的 State 对象作为类型参数。

    #----构建图时，先定义状态，再添加节点和边，最后编译----#

    graph.add_node("planner", planner_node)
    graph.add_node("actor", actor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("context_monitor", context_monitor_node)
    graph.add_node(CONTEXT_COMPRESSOR_NODE, context_compressor_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "actor")
    graph.add_edge("actor", "verifier")
    graph.add_conditional_edges(
        "verifier",
        verifier_route,
        {FINAL: "final", "planner": "context_monitor"},
    )
    graph.add_conditional_edges(
        "context_monitor",
        context_monitor_route,
        {
            FINAL: "final",
            CONTEXT_COMPRESSOR_NODE: CONTEXT_COMPRESSOR_NODE,
            "planner": "planner",
            "verifier": "verifier",
            "actor": "actor",
        },
    )
    graph.add_edge(CONTEXT_COMPRESSOR_NODE, "planner")
    graph.add_edge("final", END)

    return graph.compile()


def context_compressor_route(state: MeoGraphState) -> str:
    """压缩后去哪里？由上游写好的 `context_next_node` 决定（默认 `"verifier"`）。

    压缩只会在"没通过"的分支上发生（`context_monitor_route` 先判 `passed`），
    所以这里的值通常是 `"planner"`（继续修）或 `"final"`（试满了）。
    """
    return str(state.get("context_next_node") or DEFAULT_NEXT_NODE)


def build_complex_workflow() -> "CompiledStateGraph":
    """完整的 supervisor 拓扑：planner 直接面对 verifier，**没有 actor 节点**。

        START → planner(supervisor) → context_monitor ─┬─ 需要压缩 → context_compressor ─┐
                                                       │                                 │
                                                       └────────────→ verifier ←─────────┘
                                                                        │
                                  verifier → context_monitor ←──────────┘   （验证后也过 monitor）

    与 `build_workflow()` 的区别：

    - 没有 actor 节点 —— planner 以 supervisor 形态运行（`supervisor_node`），
      通过 `CallSearchAgentTool` / `CallCodeAgentTool` 把研究与实现委派给专家 Agent；
    - 路由统一交给 `context_monitor_route` / `context_compressor_route`，
      `verifier_route` 不再参与（所以收敛性靠 `verifier_node` 在试满 max_attempts 时
      把 `context_next_node` 写成 `"final"`，见该函数注释）。
    """
    graph = StateGraph(MeoGraphState)

    graph.add_node("planner", supervisor_node)
    graph.add_node("context_monitor", context_monitor_node)
    graph.add_node(CONTEXT_COMPRESSOR_NODE, context_compressor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "context_monitor")
    graph.add_conditional_edges(
        "context_monitor",
        context_monitor_route,
        {
            CONTEXT_COMPRESSOR_NODE: CONTEXT_COMPRESSOR_NODE,
            "verifier": "verifier",
            "planner": "planner",
            FINAL: "final",
        },
    )
    graph.add_conditional_edges(
        CONTEXT_COMPRESSOR_NODE,
        context_compressor_route,
        {
            "verifier": "verifier",
            "planner": "planner",
            FINAL: "final",
        },
    )
    graph.add_edge("verifier", "context_monitor")  # 验证后也过 monitor
    graph.add_edge("final", END)

    return graph.compile()


def initial_state(
    task: str,
    *,
    workspace: str | Path | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    runtime: RuntimeState | None = None,
    model: Any = None,
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

    return {
        "task": task,
        "runtime": runtime,
        "max_attempts": max(1, int(max_attempts)),
        "attempts": 0,
        "todos": [],
        "verification_results": [],
        "verification_checks": [],
        "passed": False,
        "last_error": "",
        "next_instruction": "",
    }
