"""图节点与专家 Agent 共用的上下文解析：RuntimeState / 模型 / 事件出口。

放在 core 层是为了避免环：`graph.nodes` 和 `agents.*` 都依赖这里，
而这里不依赖它们任何一个（将来 planner 调用专家 Agent 也不会成环）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig

from meoclaw.core.state import RuntimeState
from meoclaw.providers.openai_provider import create_model

__all__ = ["Emit", "require_runtime", "resolve_emit", "resolve_model"]

#: 事件发射器：接收节点/Agent 产出的事件字典
Emit = Callable[[dict[str, Any]], None]


def require_runtime(state: Mapping[str, Any] | RuntimeState) -> RuntimeState:
    """取出 `state["runtime"]`（也接受直接传 RuntimeState），缺失时给出可操作的报错。"""
    if isinstance(state, RuntimeState):
        return state
    runtime = state.get("runtime")
    if not isinstance(runtime, RuntimeState):
        raise ValueError(
            "state['runtime'] 缺失或类型不对：请先用 RuntimeState(workspace=...) 初始化图状态"
        )
    return runtime


def resolve_model(
    state: Mapping[str, Any] | RuntimeState,
    config: RunnableConfig | None = None,
) -> BaseChatModel:
    """按 config → runtime.metadata → create_model() 的顺序取模型。"""
    configurable = (config or {}).get("configurable") or {}
    model = configurable.get("model")
    if model is None:
        runtime = state if isinstance(state, RuntimeState) else state.get("runtime")
        metadata = getattr(runtime, "metadata", None) or {}
        model = metadata.get("model")
    if model is not None:
        return model
    return create_model()


def _stream_writer() -> Emit | None:
    """取 langgraph 的 custom stream writer；不在图运行上下文里时返回 None。"""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # 直接调用节点函数（不在图里）时会走到这里
        return None
    return writer if callable(writer) else None


def resolve_emit(config: RunnableConfig | None = None) -> Emit | None:
    """取出事件出口（两条通道都会用上）。

    1. langgraph 的 custom stream writer —— 让 `stream_mode="custom"` 收得到；
    2. `config["configurable"]["emit"]` 回调 —— 便于测试或自定义消费。

    都不存在时返回 None。
    """
    emitters: list[Emit] = []

    writer = _stream_writer()
    if writer is not None:
        emitters.append(writer)

    configurable = (config or {}).get("configurable") or {}
    callback = configurable.get("emit")
    if callable(callback) and callback is not writer:
        emitters.append(callback)

    if not emitters:
        return None
    if len(emitters) == 1:
        return emitters[0]

    def emit(event: dict[str, Any]) -> None:
        for item in emitters:
            item(event)

    return emit
