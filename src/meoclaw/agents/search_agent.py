"""searchAgent：把 planner 的调研指令转成研究摘要 + 结构化来源的检索专家。

用法（supervisor 的 CallSearchAgentTool 调用）：

    result = run_search_agent(state, "查一下 LangGraph 的 add_messages 语义", writer=emit)
    result["ok"], result["summary"], result["queries"], result["sources"]

内部复用底层 ReAct 循环（`core.react.stream_react_events`），所以 tool_call_id 配对、
工具失败降级成 `ok=False` 这些行为与 codeAgent 完全一致。与 codeAgent 的区别：

- 系统提示词是 `SEARCH_AGENT_PROMPT`，唯一的外部能力是 `WebSearchTool`；
- 不绑定任何会改工作区的工具（写文件/编辑/命令都不给它）；
- 收尾时从检索事件里汇总 `queries` 与去重后的 `sources`（只保留 title/url）。

注意：本模块**不 import `graph.nodes`**，而是共用 `core.context` 里的上下文工具，
这样 supervisor 节点直接调用本模块也不会形成 import 环。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from meoclaw.core.context import Emit, require_runtime, resolve_emit, resolve_model
from meoclaw.core.react import stream_react_events
from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import build_layered_memory, format_layered_memory_for_prompt
from meoclaw.prompts.stage3 import SEARCH_AGENT_PROMPT
from meoclaw.tools.web_search_tool import WEB_SEARCH_TOOL_NAME, build_web_search_tool

__all__ = [
    "DEFAULT_MAX_LOOPS",
    "SEARCH_AGENT_NAME",
    "build_search_agent_tools",
    "compose_search_message",
    "run_search_agent",
]

#: Agent 名字（事件里用它标记来源）
SEARCH_AGENT_NAME = "searchAgent"
#: 一轮最多几次"模型 → 工具"往返（检索不需要太多轮）
DEFAULT_MAX_LOOPS = 4
#: 返回的来源条数上限（已经按 URL 去重后再截断）
MAX_SOURCES = 12


def build_search_agent_tools() -> list:
    """searchAgent 的工具集：只有 WebSearchTool（检索专家只检索）。"""
    return [build_web_search_tool()]


def compose_search_message(
    state: Mapping[str, Any] | RuntimeState,
    instruction: str,
    *,
    memory: Mapping[str, Any] | None = None,
) -> str:
    """HumanMessage = 任务 + planner 指令 + 既有研究笔记 + memory 快照。"""
    view: Mapping[str, Any] = state if isinstance(state, Mapping) else {"runtime": state}
    parts: list[str] = []

    task = str(view.get("task") or "").strip()
    if task:
        parts.append(f"## Task\n{task}")

    instruction = (instruction or "").strip()
    if instruction:
        parts.append(f"## Research instruction from planner\n{instruction}")

    notes = str(view.get("research_notes") or "").strip()
    if notes:
        parts.append(f"## Existing research notes (do not repeat them)\n{notes}")

    snapshot = memory if memory is not None else build_layered_memory(view, node=SEARCH_AGENT_NAME)
    if snapshot:
        parts.append(f"## Memory snapshot\n{format_layered_memory_for_prompt(snapshot)}")

    parts.append("Search as needed, then finish with a concise research summary and source URLs.")
    return "\n\n".join(parts)


def run_search_agent(
    state: Mapping[str, Any] | RuntimeState,
    instruction: str,
    *,
    writer: Emit | None = None,
    max_loops: int = DEFAULT_MAX_LOOPS,
    model: BaseChatModel | None = None,
) -> dict[str, Any]:
    """跑一轮 searchAgent，返回本轮结果。

    Args:
        state: 图状态（含 `runtime` / `task` / `research_notes` …），也可以直接给 `RuntimeState`。
        instruction: planner 交给检索专家的具体指令。
        writer: 事件出口；为 None 时用 langgraph 的 custom stream writer（在图里跑时）。
        max_loops: 最多几次"模型 → 工具"往返。
        model: 可注入模型（测试用）；为空时按 config/metadata/create_model 顺序解析。

    Returns:
        `{ok, summary, queries, sources, messages, tool_events, agent, stopped_reason, loops}`
        - `ok`：本轮是否正常收尾（没有撞到 `max_loops`）；
        - `summary`：收尾研究摘要（没有摘要时回落到各次检索的 answer 拼接）；
        - `queries`：实际发出的检索词列表；
        - `sources`：按 URL 去重后的 `[{title, url}]`（截断到 MAX_SOURCES 条）。

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

    memory_snapshot = build_layered_memory(view, node=SEARCH_AGENT_NAME)

    messages: list[BaseMessage] = []
    tool_events: list[dict[str, Any]] = []
    queries: list[str] = []
    sources: list[dict[str, str]] = []
    answers: list[str] = []
    summary = ""
    stopped_reason = ""
    loops = 0

    for event in stream_react_events(
        compose_search_message(view, instruction, memory=memory_snapshot),
        runtime=runtime,
        max_loops=max_loops,
        model=chat_model,
        tools=build_search_agent_tools(),
        system_prompt=SEARCH_AGENT_PROMPT,
        message_sink=messages,
    ):
        tagged = {**event, "agent": SEARCH_AGENT_NAME}
        tool_events.append(tagged)
        if emit is not None:
            emit(tagged)

        kind = event.get("type")
        if kind == "tool_call" and event.get("name") == WEB_SEARCH_TOOL_NAME:
            query = str((event.get("args") or {}).get("query") or "").strip()
            if query:
                queries.append(query)
        elif kind == "tool_result" and event.get("name") == WEB_SEARCH_TOOL_NAME:
            result = event.get("result")
            if isinstance(result, Mapping):
                if result.get("ok") is False:
                    continue  # 缺 Key / 网络失败：已由结果文本带回模型，不进 sources
                if result.get("answer"):
                    answers.append(str(result["answer"]))
                for item in result.get("results") or []:
                    if not isinstance(item, Mapping):
                        continue
                    url = str(item.get("url") or "").strip()
                    if not url or any(source["url"] == url for source in sources):
                        continue
                    sources.append(
                        {
                            "title": str(item.get("title") or url),
                            "url": url,
                        }
                    )
        elif kind == "final_answer":
            summary = str(event.get("content") or "")
            stopped_reason = str(event.get("stopped_reason") or "")
            loops = int(event.get("loops") or 0)

    if not summary and answers:
        summary = "\n\n".join(answers)
    if stopped_reason == "max_loops" and summary:
        summary = f"{summary}\n\n[searchAgent 达到最大轮数 {max_loops}，检索可能未完成]"

    return {
        "ok": stopped_reason == "no_tool_calls",
        "summary": summary,
        "queries": queries,
        "sources": sources[:MAX_SOURCES],
        "messages": messages,
        "tool_events": tool_events,
        "agent": SEARCH_AGENT_NAME,
        "stopped_reason": stopped_reason,
        "loops": loops,
        "memory_snapshot": memory_snapshot,
    }
