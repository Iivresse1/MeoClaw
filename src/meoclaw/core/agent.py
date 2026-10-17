"""CLI 侧的统一事件流：驱动整张 LangGraph 工作流，把图事件翻译成统一格式。

    for event in stream_agent_events("给 utils.py 加个 add 函数", workspace="./workspace"):
        print(event["type"], event)

实现上不再自己跑 ReAct 循环，而是：

    workflow.stream(inputs, stream_mode=["updates", "custom"])

- `updates`（节点跑完后返回的 state 增量）→ 翻译成 `plan` / `actor_summary` /
  `verification` / `final_answer`；
- `custom`（节点运行中由 `get_stream_writer()` 或 config 里的 `emit` 回调实时吐出）
  → 原样冒泡，并带上 `node` 字段。

事件类型（`event["type"]`）：

    node_start            某个节点开始（node）
    ai_message            模型输出（actor / verifier）
    tool_call             即将执行的工具调用（node / name / args）
    tool_result           工具结果（node / name / result / ok）
    verification_command  验收命令执行结果（node / command / ok / exit_code）
    context_usage         上下文用量（node / token_count / token_limit / should_compress / next_node）
    context_compressed    上下文已压缩（node / tokens_before / tokens_after / dropped_messages / used_llm）
    react_done            节点内部 ReAct 循环收尾（node）—— 注意不是最终的 final_answer
    plan                  planner 的计划（plan_summary / todos / acceptance_criteria / verification_commands）
    actor_summary         actor 的收尾总结（content / todos）
    verification          verifier 的判定（passed / attempts / checks / results / last_error / next_instruction）
    final_answer          final 节点写好的最终回答（content / passed）
    workflow_end          整张图结束（passed / attempts / final_answer / todos / last_error / next_instruction）

底层的 ReAct 循环在 `core/react.py`，由 actor / verifier 节点内部使用。

注意：本模块依赖 langchain/langgraph，因此**刻意不在 `core/__init__.py` 里 re-export**，
这样 `import meoclaw` 依旧不会把 langchain 拉进来。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from meoclaw.core.react import (
    ACTOR_PROMPT,
    execute_tool,
    message_text,
    serialize_tool_result,
)
from meoclaw.core.state import RuntimeState
from meoclaw.graph.nodes import DEFAULT_MAX_ATTEMPTS
from meoclaw.graph.workflow import build_workflow, initial_state
from meoclaw.providers.openai_provider import create_model

__all__ = [
    "ACTOR_PROMPT",
    "UNIFIED_EVENT_TYPES",
    "execute_tool",
    "message_text",
    "serialize_tool_result",
    "stream_agent_events",
]

#: 统一事件流里可能出现的事件类型（便于消费方做穷举分支）
UNIFIED_EVENT_TYPES: tuple[str, ...] = (
    "node_start",
    "ai_message",
    "tool_call",
    "tool_result",
    "verification_command",
    "context_usage",
    "context_compressed",
    "react_done",
    "plan",
    "actor_summary",
    "verification",
    "final_answer",
    "workflow_end",
    "custom",
)


def _iter_updates(payload: Any) -> Iterator[tuple[str, Any]]:
    """`updates` 流的负载形如 `{"planner": {...}}`（同一帧可能有多个节点）。"""
    if isinstance(payload, Mapping):
        for node, update in payload.items():
            yield str(node), update


def _custom_event(payload: Any) -> dict[str, Any]:
    """把 `custom` 流里的负载统一成事件字典。"""
    if isinstance(payload, Mapping):
        event = dict(payload)
        event.setdefault("type", "custom")
        event.setdefault("node", "")
        return event
    return {"type": "custom", "node": "", "payload": payload}


def stream_agent_events(
    task: str,
    *,
    workspace: str | Path | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    model: BaseChatModel | None = None,
    model_name: str | None = None,
    temperature: float = 0.0,
    runtime: RuntimeState | None = None,
) -> Iterator[dict[str, Any]]:
    """跑一遍完整工作流（planner → actor → verifier → final），逐事件 yield。

    Args:
        task: 用户任务描述。
        workspace: 工作区目录（不存在会自动创建）；与 `runtime` 二选一。
        max_attempts: 最大尝试轮数（verifier 每跑一次算一轮），默认 3。
        model: 可注入的模型实例（测试用）；为空时用 `create_model()`。
        model_name: 覆盖模型名，透传给 `create_model()`。
        temperature: 采样温度。
        runtime: 复用已有的 `RuntimeState`。

    Yields:
        统一格式的事件字典，`event["type"]` ∈ `UNIFIED_EVENT_TYPES`。
        最后一个事件一定是 `workflow_end`。

    Raises:
        ValueError: task 为空或 max_attempts 非法。
        MissingAPIKeyError: 没有配置 API Key（未注入 model 时）。
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 不能为空")
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        raise ValueError(f"max_attempts 必须是大于 0 的整数，收到 {max_attempts!r}")

    chat_model = model if model is not None else create_model(
        model=model_name, temperature=temperature
    )

    inputs = initial_state(
        task,
        workspace=workspace,
        max_attempts=max_attempts,
        runtime=runtime,
        model=chat_model,
    )
    workflow = build_workflow()

    passed = False
    attempts = 0
    final_answer = ""
    todos: list[Any] = []
    last_error = ""
    next_instruction = ""

    for chunk in workflow.stream(inputs, stream_mode=["updates", "custom"]):
        # 指定多个 stream_mode 时 langgraph 产出 (mode, payload) 元组
        if isinstance(chunk, tuple) and len(chunk) == 2:
            mode, payload = chunk
        else:  # pragma: no cover - 单模式时的兜底
            mode, payload = "updates", chunk

        if mode == "custom":
            yield _custom_event(payload)
            continue

        if mode != "updates":
            continue

        for node, update in _iter_updates(payload):
            if not isinstance(update, Mapping):
                continue

            if node == "planner":
                yield {
                    "type": "plan",
                    "node": node,
                    "plan_summary": str(update.get("plan_summary") or ""),
                    "todos": update.get("todos") or [],
                    "acceptance_criteria": update.get("acceptance_criteria") or [],
                    "verification_commands": update.get("verification_commands") or [],
                }

            elif node == "actor":
                todos = list(update.get("todos") or todos)
                yield {
                    "type": "actor_summary",
                    "node": node,
                    "content": str(update.get("last_actor_summary") or ""),
                    "todos": todos,
                }

            elif node == "verifier":
                passed = bool(update.get("passed"))
                attempts = int(update.get("attempts") or attempts)
                todos = list(update.get("todos") or todos)
                last_error = str(update.get("last_error") or "")
                next_instruction = str(update.get("next_instruction") or "")
                yield {
                    "type": "verification",
                    "node": node,
                    "passed": passed,
                    "attempts": attempts,
                    "checks": update.get("verification_checks") or [],
                    "results": update.get("verification_results") or [],
                    "last_error": last_error,
                    "next_instruction": next_instruction,
                }

            elif node == "final":
                final_answer = str(update.get("final_answer") or "")
                passed = bool(update.get("passed", passed))
                yield {
                    "type": "final_answer",
                    "node": node,
                    "content": final_answer,
                    "passed": passed,
                }

            else:  # pragma: no cover - 图上没有别的节点
                yield {"type": "custom", "node": node, "payload": dict(update)}

    yield {
        "type": "workflow_end",
        "node": "workflow",
        "passed": passed,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "final_answer": final_answer,
        "todos": todos,
        "last_error": last_error,
        "next_instruction": next_instruction,
    }
