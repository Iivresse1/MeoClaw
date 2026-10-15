"""底层 ReAct 循环：一次"模型 → 工具 → 模型"的执行，逐事件 yield。

这是**节点内部**使用的能力：`graph/nodes.py` 的 actor / verifier 都用它跑工具往返。
CLI 不再直接用它 —— CLI 消费的是 `core/agent.py` 里驱动整张图的统一事件流。

用法：

    for event in stream_react_events("给 utils.py 加个 add 函数", workspace="./workspace"):
        print(event["type"], event)

事件类型（`event["type"]`）：

    ai_message    模型本轮输出，含 content 与 tool_calls
    tool_call     即将执行的工具调用（name / args / id）
    tool_result   工具执行结果（result 原文 + content JSON 串 + ok 标志）
    final_answer  收尾：最后一条有文本的 AI 消息 + 停止原因

工具失败（ToolException：路径越界、正则非法、匹配到多处等）由 `execute_tool` 收敛成
`ok=False` 的结果文本回给模型，循环不会中断。`stopped_reason == "max_loops"` 时，
`final_answer` 里的文本可能是循环早期那轮的说明——此时应当视为"任务未完成"。

注意：本模块依赖 langchain，因此**刻意不在 `core/__init__.py` 里 re-export**，
这样 `import meoclaw` 依旧不会把 langchain 拉进来。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool

from meoclaw.core.state import RuntimeState
from meoclaw.prompts.stage2 import ACTOR_PROMPT
from meoclaw.providers.openai_provider import create_model
from meoclaw.tools.registry import build_tools

__all__ = [
    "ACTOR_PROMPT",
    "REACT_EVENT_TYPES",
    "execute_tool",
    "message_text",
    "serialize_tool_result",
    "stream_react_events",
]

# ACTOR_PROMPT 统一维护在 meoclaw.prompts.stage2，这里 re-export 方便引用。

#: ReAct 循环会产出的事件类型（便于消费方做穷举分支）
REACT_EVENT_TYPES: tuple[str, ...] = (
    "ai_message",
    "tool_call",
    "tool_result",
    "final_answer",
)


def message_text(content: Any) -> str:
    """把模型返回的 content 统一成纯文本。

    content 可能是 str，也可能是 content blocks 列表
    （`[{"type": "text", "text": "..."}]`）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content)


def serialize_tool_result(result: Any) -> str:
    """把工具返回值编码成 `ToolMessage.content`（统一走 JSON）。"""
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str 基本兜住了
        return json.dumps(str(result), ensure_ascii=False)


def _call_field(call: Any, key: str, default: Any) -> Any:
    """安全读取 tool_call 字段：兼容个别网关返回的非 Mapping 结构。"""
    if isinstance(call, Mapping):
        return call.get(key, default)
    return default


def execute_tool(
    tool_map: Mapping[str, StructuredTool],
    call: Mapping[str, Any],
) -> tuple[bool, Any]:
    """执行一次工具调用。

    Args:
        tool_map: `{工具名: StructuredTool}`。
        call: 模型给出的 tool_call，含 `name` 与 `args`。

    Returns:
        `(ok, result)`。未知工具、结构异常或执行抛错时 `ok=False`，
        `result` 是给模型看的错误文本——单个工具失败不应该中断整个循环。
    """
    if not isinstance(call, Mapping):
        return False, f"非法的 tool_call 结构：{call!r}"

    name = str(call.get("name") or "")
    args = call.get("args") or {}

    tool = tool_map.get(name)
    if tool is None:
        return False, f"未知工具 {name!r}；可用工具：{', '.join(tool_map)}"

    try:
        return True, tool.invoke(args)
    except KeyboardInterrupt:  # pragma: no cover - 交给上层
        raise
    except Exception as exc:
        return False, f"工具执行失败：{type(exc).__name__}: {exc}"


def stream_react_events(
    task: str,
    *,
    workspace: str | Path | None = None,
    max_loops: int = 10,
    model: BaseChatModel | None = None,
    model_name: str | None = None,
    temperature: float = 0.0,
    runtime: RuntimeState | None = None,
    tools: Sequence[StructuredTool] | None = None,
    system_prompt: str = ACTOR_PROMPT,
    message_sink: list[BaseMessage] | None = None,
) -> Iterator[dict[str, Any]]:
    """跑一轮 ReAct 循环，逐事件 yield。

    Args:
        task: 用户任务描述。
        workspace: 工作区目录（不存在会自动创建）；与 `runtime` 二选一。
        max_loops: 最大循环轮数（每轮 = 一次模型调用 + 其工具调用）。
        model: 可注入的模型实例（测试用）；为空时用 `create_model()`。
        model_name: 覆盖模型名，透传给 `create_model()`。
        temperature: 采样温度。
        runtime: 直接复用已有的 `RuntimeState`（图节点会传 `state["runtime"]`）。
        tools: 覆盖工具集；为空时用 `build_tools(runtime)`。
            验证节点用它传只读工具集。
        system_prompt: 系统提示词，默认 `ACTOR_PROMPT`。
        message_sink: 传入 list 时，本轮的 AI 消息与 ToolMessage 会同步追加进去
            （图节点据此产出 `state["messages"]` 的增量）。

    Yields:
        事件字典，`event["type"]` ∈ `REACT_EVENT_TYPES`。

    Raises:
        ValueError: task 为空、max_loops 非法，或 workspace/runtime 都没给。
        MissingAPIKeyError: 没有配置 API Key（未注入 model 时）。
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 不能为空")
    if not isinstance(max_loops, int) or isinstance(max_loops, bool) or max_loops < 1:
        raise ValueError(f"max_loops 必须是大于 0 的整数，收到 {max_loops!r}")

    # 1) 运行时状态（workspace 自动创建，工具可见范围以它为准）
    if runtime is None:
        if workspace is None:
            raise ValueError("必须提供 workspace 或 runtime 之一")
        runtime = RuntimeState(workspace=workspace)

    # 2) 工具与模型
    tool_list: list[StructuredTool] = list(tools) if tools is not None else build_tools(runtime)
    tool_map: dict[str, StructuredTool] = {tool.name: tool for tool in tool_list}
    chat_model = model if model is not None else create_model(
        model=model_name, temperature=temperature
    )
    agent = chat_model.bind_tools(tool_list)

    # 3) 初始消息
    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=task),
    ]

    last_ai_text = ""
    loops_run = 0
    stopped_reason = "max_loops"

    for loop_index in range(1, max_loops + 1):
        loops_run = loop_index

        response = agent.invoke(messages)
        messages.append(response)
        if message_sink is not None:
            message_sink.append(response)

        text = message_text(getattr(response, "content", ""))
        if text.strip():
            last_ai_text = text
        tool_calls = list(getattr(response, "tool_calls", None) or [])

        yield {
            "type": "ai_message",
            "content": text,
            "tool_calls": tool_calls,
            "loop": loop_index,
        }

        if not tool_calls:
            stopped_reason = "no_tool_calls"
            break

        for call_index, call in enumerate(tool_calls, start=1):
            name = str(_call_field(call, "name", "") or "")
            args = _call_field(call, "args", None) or {}
            raw_id = _call_field(call, "id", None)
            if isinstance(raw_id, str) and raw_id:
                call_id = raw_id
            else:
                call_id = f"call_{loop_index}_{call_index}"
                if isinstance(call, dict):
                    # 回填合成 id：AIMessage 的 tool_calls 与后续 ToolMessage 必须严格配对，
                    # 否则下一轮请求会被服务端拒绝（找不到对应的 tool_call_id）。
                    call["id"] = call_id

            yield {
                "type": "tool_call",
                "name": name,
                "args": args,
                "id": call_id,
                "loop": loop_index,
            }

            ok, result = execute_tool(tool_map, call)
            payload = serialize_tool_result(result)
            tool_message = ToolMessage(content=payload, tool_call_id=call_id)
            messages.append(tool_message)
            if message_sink is not None:
                message_sink.append(tool_message)

            yield {
                "type": "tool_result",
                "name": name,
                "result": result,
                "content": payload,
                "ok": ok,
                "loop": loop_index,
            }

    yield {
        "type": "final_answer",
        "content": last_ai_text,
        "loops": loops_run,
        "stopped_reason": stopped_reason,
    }
