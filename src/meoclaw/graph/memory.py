"""三层 Memory 系统：Rules / Working / History Summary。

设计原则（也正是 `RULES_LAYER` 最后一条）：

> Do not expose memory write tools to agents; layered memory is assembled by the runtime.

所以这里**只读**工作区里的 `NOTEPAD.md`（长期笔记）与 `HISTORY_SUMMARY.md`（压缩历史），
连同图状态一起压成一份**有界**的快照（每个字段都有字符上限），交给需要它的节点拼进提示词。
写入 `HISTORY_SUMMARY.md` 由运行时的压缩步骤调用 `append_history_summary()` 完成。

三层各自的职责：

| 层 | 内容 | 生命周期 |
| --- | --- | --- |
| Rules | 固定规则（工作区范围、路径写法、文件分工、不给 Agent 写记忆的工具） | 永远不变 |
| Working | 当前任务状态：任务/计划/待办/验收标准/最近失败/轮数/检索要点/交接记录 | 每轮刷新 |
| History Summary | 压缩历史：`HISTORY_SUMMARY.md`、`NOTEPAD.md`、`context_summary`、最近 3 次压缩事件 | 跨压缩保留 |
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from meoclaw.core.context import require_runtime
from meoclaw.core.state import RuntimeState
from meoclaw.tools.notepad_tool import NOTEPAD_FILENAME
from meoclaw.tools.todo_tool import TODO_FILENAME

__all__ = [
    "HISTORY_SUMMARY_FILENAME",
    "RULES_LAYER",
    "append_history_summary",
    "build_layered_memory",
    "format_layered_memory_for_prompt",
    "read_history_summary",
    "read_notepad",
    "short_text",
    "trim_handoffs",
]

#: Rules Layer：固定规则，不随任务变化
RULES_LAYER: dict[str, Any] = {
    "scope": "workspace",
    "storage": "internal",
    "rules": [
        "Work inside the current workspace only.",
        "Use paths relative to the workspace; do not prefix paths with workspace/.",
        "Keep durable task context outside the raw messages transcript when possible.",
        "Treat TODO.md as working plan state, NOTEPAD.md as durable notes, and HISTORY_SUMMARY.md as compressed history.",
        "Do not expose memory write tools to agents; layered memory is assembled by the runtime.",
    ],
}

#: 压缩历史文件名（工作区根目录）
HISTORY_SUMMARY_FILENAME = "HISTORY_SUMMARY.md"
#: 读进记忆的单个文档上限（字符）
DOC_READ_CHARS = 8000
#: 各字段进记忆时的字符上限（task / plan_summary / todos 等保持原样，由上游保证有界）
NOTES_LIMIT = 1600
AGENT_SUMMARY_LIMIT = 1000
LAST_ERROR_LIMIT = 1400
HISTORY_LIMIT = 2200
NOTEPAD_LIMIT = 1800
CONTEXT_SUMMARY_LIMIT = 1600
#: 交接记录只保留最近几条；每条字段再压到这个长度
HANDOFF_KEEP = 6
HANDOFF_FIELD_LIMIT = 600
#: 压缩事件只带最近几条
COMPRESSION_EVENT_KEEP = 3


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _short_text(text: Any, limit: int) -> str:
    """超长文本截断，末尾加 "..."（返回值总长不超过 limit）。"""
    value = "" if text is None else str(text)
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return "." * limit
    return f"{value[: limit - 3]}..."


#: 公开别名：其它模块（例如上下文压缩节点）也要按同样的规则截断文本
short_text = _short_text


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact_sources(sources: Any) -> list[dict[str, str]]:
    """来源只保留 title 与 url（丢掉 snippet 之类的大块内容）。"""
    compact: list[dict[str, str]] = []
    for item in sources or []:
        data = item if isinstance(item, Mapping) else {"title": str(item)}
        title = str(data.get("title") or data.get("name") or "").strip()
        url = str(data.get("url") or data.get("link") or "").strip()
        if not title and not url:
            continue
        compact.append({"title": title, "url": url})
    return compact


def _trim_handoffs(
    handoffs: Any,
    *,
    keep: int = HANDOFF_KEEP,
    field_limit: int = HANDOFF_FIELD_LIMIT,
) -> list[dict[str, str]]:
    """只保留最近 `keep` 条交接记录，并把每条字段压短。"""
    items = [item for item in (handoffs or []) if isinstance(item, Mapping)]
    if keep > 0:
        items = items[-keep:]
    return [
        {str(key): _short_text(value, field_limit) for key, value in item.items()}
        for item in items
    ]


#: 公开别名：上下文压缩节点也要按同样的规则裁剪交接记录
trim_handoffs = _trim_handoffs


def _read_document(
    state: RuntimeState, filename: str, limit: int = DOC_READ_CHARS
) -> dict[str, Any]:
    """读工作区里的一个文档，返回 `{path, exists, content}`。

    记忆组装是"尽力而为"：文件不存在、越界、读不了都只是 `exists=False`，
    绝不因为读不到某个文件就让整张图失败。
    """
    document: dict[str, Any] = {"path": filename, "exists": False, "content": ""}
    try:
        path = state.resolve(filename)
        if path.is_file():
            document["exists"] = True
            document["content"] = path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except Exception:  # noqa: BLE001 - 见 docstring
        return document
    return document


# --------------------------------------------------------------------------- #
# History Summary Store
# --------------------------------------------------------------------------- #
def read_notepad(state: RuntimeState) -> dict[str, Any]:
    """读取 `NOTEPAD.md` → `{path, exists, content}`。"""
    return _read_document(state, NOTEPAD_FILENAME)


def read_history_summary(state: RuntimeState) -> dict[str, Any]:
    """读取 `HISTORY_SUMMARY.md` → `{path, exists, content}`。"""
    return _read_document(state, HISTORY_SUMMARY_FILENAME)


def append_history_summary(
    state: RuntimeState,
    content: str,
    *,
    heading: str = "History",
) -> str | None:
    """把一段压缩历史追加到 `HISTORY_SUMMARY.md`（运行时压缩步骤调用）。

    文件超过 `state.max_read_bytes` 时只保留最新的一段（旧历史被裁掉），
    保证它不会无限膨胀。写失败返回 None，不抛异常。
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")

    try:
        path = state.resolve(HISTORY_SUMMARY_FILENAME)
        existing = ""
        if path.is_file():
            existing = path.read_text(encoding="utf-8", errors="replace")
        if not existing.strip():
            existing = "# HISTORY SUMMARY\n"

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        block = f"\n\n## {heading} ({stamp})\n\n{content}\n"

        if len(existing) + len(block) > state.max_read_bytes:
            # 超限时：最新一段完整保留，旧历史按剩余预算保留尾部
            marker = "…（旧历史已裁剪）\n"
            budget = max(0, state.max_read_bytes - len(marker) - len(block))
            kept = existing.rstrip()[-budget:] if budget else ""
            updated = f"{marker}{kept}{block}"
        else:
            updated = f"{existing.rstrip()}\n{block}"

        path.write_text(updated, encoding="utf-8", newline="")
    except Exception:  # noqa: BLE001 - 持久化是尽力而为
        return None
    return state.display(path)


# --------------------------------------------------------------------------- #
# 三层记忆组装
# --------------------------------------------------------------------------- #
def _session_fields(state: RuntimeState) -> tuple[str, int]:
    """会话标识取 `runtime.metadata`（session_id / thread_id / session_turn / turn）。"""
    metadata = getattr(state, "metadata", None) or {}
    session_id = str(metadata.get("session_id") or metadata.get("thread_id") or "")
    session_turn = _as_int(metadata.get("session_turn") or metadata.get("turn"), 0)
    return session_id, session_turn


def build_layered_memory(
    state: Mapping[str, Any] | RuntimeState,
    *,
    node: str = "graph",
) -> dict[str, Any]:
    """组装三层记忆快照。

    Args:
        state: 图状态（含 `runtime`），或直接给 `RuntimeState`。
        node: 当前节点名，写进 `working_memory["node"]`，便于排查"谁看到的记忆"。

    Returns:
        `{"rules": ..., "working_memory": ..., "history_summary_store": ...}`，
        所有长文本都已按上限截断，可以直接塞进提示词。
    """
    runtime = require_runtime(state)
    view: Mapping[str, Any] = state if isinstance(state, Mapping) else {"runtime": runtime}

    notepad = read_notepad(runtime)
    history = read_history_summary(runtime)
    session_id, session_turn = _session_fields(runtime)

    working_memory: dict[str, Any] = {
        "node": node,
        "task": view.get("task", ""),
        "session_id": session_id,
        "session_turn": session_turn,
        "plan_summary": view.get("plan_summary", ""),
        "todos": list(view.get("todos") or []),
        "acceptance_criteria": list(view.get("acceptance_criteria") or []),
        "verification_commands": list(view.get("verification_commands") or []),
        "research_notes": _short_text(view.get("research_notes", ""), NOTES_LIMIT),
        "sources": _compact_sources(view.get("sources")),
        "agent_handoffs": _trim_handoffs(view.get("agent_handoffs")),
        "code_agent_summary": _short_text(
            view.get("code_agent_summary", ""), AGENT_SUMMARY_LIMIT
        ),
        "verifier_summary": _short_text(
            view.get("verifier_summary", ""), AGENT_SUMMARY_LIMIT
        ),
        "last_error": _short_text(view.get("last_error", ""), LAST_ERROR_LIMIT),
        "attempts": _as_int(view.get("attempts"), 0),
        "max_attempts": _as_int(view.get("max_attempts"), 3) or 3,
    }

    # 图状态里的 history_summary 优先（压缩节点刚写的最新值），否则用文件内容
    history_text = view.get("history_summary") or history.get("content", "")
    history_summary_store: dict[str, Any] = {
        "history_path": HISTORY_SUMMARY_FILENAME,
        "history_exists": bool(history.get("exists", False)),
        "history_summary": _short_text(history_text, HISTORY_LIMIT),
        "notepad_path": NOTEPAD_FILENAME,
        "notepad_exists": bool(notepad.get("exists", False)),
        "notepad": _short_text(notepad.get("content", ""), NOTEPAD_LIMIT),
        "context_summary": _short_text(
            view.get("context_summary", ""), CONTEXT_SUMMARY_LIMIT
        ),
        "compression_events": list(view.get("compression_events") or [])[
            -COMPRESSION_EVENT_KEEP:
        ],
        "todo_path": TODO_FILENAME,
    }

    return {
        "rules": dict(RULES_LAYER),
        "working_memory": working_memory,
        "history_summary_store": history_summary_store,
    }


def format_layered_memory_for_prompt(memory: Mapping[str, Any], *, indent: int = 2) -> str:
    """把三层记忆渲染成 JSON 文本，直接拼进提示词。"""
    return json.dumps(memory, ensure_ascii=False, indent=indent, default=str)
