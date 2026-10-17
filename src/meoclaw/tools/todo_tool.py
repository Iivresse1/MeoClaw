"""计划类工具：TodoWriteTool（发布/修订计划）与 TodoUpdateTool（更新待办状态）。

分工：
- `TodoWriteTool` 无状态，planner 用它做**结构化输出**：模型的 tool_call.args
  就是计划本身（plan_summary / todos / acceptance_criteria / verification_commands）。
- `TodoUpdateTool` 需要知道"更新记到哪"，所以按运行期构造
  （`build_todo_update_tool(runtime)`）：把变更追加到
  `runtime.metadata["todo_updates"]`，由 actor 节点在循环结束后合并回图状态。
  这样做的好处是——工具不需要（也无法）直接改 LangGraph 的 state。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from meoclaw.core.state import RuntimeState

__all__ = [
    "TODO_FILENAME",
    "TODO_STATUSES",
    "TODO_UPDATES_KEY",
    "TodoItemInput",
    "TodoUpdateInput",
    "TodoWriteInput",
    "TodoUpdateToolName",
    "TodoWriteTool",
    "apply_todo_updates",
    "build_todo_update_tool",
    "build_todo_write_tool",
    "drain_todo_updates",
    "normalize_plan",
    "normalize_todos",
    "persist_todos",
    "render_todos_markdown",
]

#: 允许的待办状态（与 graph/state.py 的注释保持一致）
TODO_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed", "blocked")
#: TodoUpdateTool 把变更记在 runtime.metadata 的这个键下
TODO_UPDATES_KEY = "todo_updates"
#: 待办持久化到工作区里的这个文件（跨压缩/重启保留计划状态）
TODO_FILENAME = "TODO.md"

TodoStatus = Literal["pending", "in_progress", "completed", "blocked"]

TodoWriteToolName = "TodoWriteTool"
TodoUpdateToolName = "TodoUpdateTool"


class TodoItemInput(BaseModel):
    """planner 给出的单条待办。"""

    id: str = Field("", description="短且稳定的 id，例如 't1'；留空会自动编号")
    content: str = Field(..., description="这条待办要做什么，必须可验证")
    status: TodoStatus = Field("pending", description="初始状态固定用 pending")
    note: str = Field("", description="可选备注")


class TodoWriteInput(BaseModel):
    """TodoWriteTool 入参：整个计划。"""

    plan_summary: str = Field(..., description="计划摘要，一两句话说清整体思路")
    todos: list[TodoItemInput] = Field(..., description="按执行顺序排列的待办列表")
    acceptance_criteria: list[str] = Field(
        default_factory=list, description="判定任务完成的标准"
    )
    verification_commands: list[str] = Field(
        default_factory=list, description="证明完成的 shell 命令（在工作区根目录执行）"
    )


class TodoUpdateInput(BaseModel):
    """TodoUpdateTool 入参：单条待办的状态更新。"""

    todo_id: str = Field(..., description="要更新的待办 id，例如 't2'")
    status: TodoStatus = Field(..., description="新状态")
    note: str = Field("", description="可选备注，例如失败原因")


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #
def _as_mapping(item: Any) -> dict[str, Any]:
    """把 dict / pydantic 模型统一成 dict。"""
    if isinstance(item, BaseModel):
        return item.model_dump()
    if isinstance(item, Mapping):
        return dict(item)
    return {}


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_status(value: Any) -> str | None:
    """把状态归一到 TODO_STATUSES 之一；无法识别时返回 None。"""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "todo": "pending",
        "open": "pending",
        "doing": "in_progress",
        "inprogress": "in_progress",
        "active": "in_progress",
        "done": "completed",
        "complete": "completed",
        "finished": "completed",
        "fail": "blocked",
        "failed": "blocked",
    }
    text = aliases.get(text, text)
    return text if text in TODO_STATUSES else None


def normalize_todos(raw: Any) -> list[dict[str, str]]:
    """把 LLM 给的 todos 规范成 `[{id, content, status, note}]`。

    - 缺 id / id 重复时自动编号（t1、t2……）；
    - 无法识别的状态回落到 pending；
    - 没有 content 的条目直接丢弃。
    """
    if raw is None:
        return []
    items = [raw] if isinstance(raw, (str, Mapping, BaseModel)) else list(raw)

    todos: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for item in items:
        payload = _as_mapping(item)
        if not payload and isinstance(item, str):
            payload = {"content": item}

        content = str(payload.get("content") or "").strip()
        if not content:
            continue

        todo_id = str(payload.get("id") or "").strip()
        if not todo_id or todo_id in used_ids:
            todo_id = f"t{len(todos) + 1}"
            while todo_id in used_ids:
                todo_id = f"{todo_id}_"
        used_ids.add(todo_id)

        todos.append(
            {
                "id": todo_id,
                "content": content,
                "status": normalize_status(payload.get("status")) or "pending",
                "note": str(payload.get("note") or "").strip(),
            }
        )
    return todos


def normalize_plan(payload: Any) -> dict[str, Any]:
    """校验并规范化 LLM 给出的计划，缺字段补默认值。"""
    data = _as_mapping(payload)
    todos = normalize_todos(data.get("todos"))
    plan_summary = str(data.get("plan_summary") or "").strip()
    if not plan_summary and todos:
        plan_summary = "；".join(todo["content"] for todo in todos[:3])
    return {
        "plan_summary": plan_summary,
        "todos": todos,
        "acceptance_criteria": _str_list(data.get("acceptance_criteria")),
        "verification_commands": _str_list(data.get("verification_commands")),
    }


# --------------------------------------------------------------------------- #
# TodoWriteTool
# --------------------------------------------------------------------------- #
def build_todo_write_tool() -> StructuredTool:
    """构造 TodoWriteTool（无状态，可直接复用）。"""

    def todo_write(
        plan_summary: str,
        todos: Sequence[Mapping[str, Any]],
        acceptance_criteria: Sequence[str] | None = None,
        verification_commands: Sequence[str] | None = None,
    ) -> str:
        plan = normalize_plan(
            {
                "plan_summary": plan_summary,
                "todos": todos,
                "acceptance_criteria": acceptance_criteria,
                "verification_commands": verification_commands,
            }
        )
        if not plan["todos"]:
            raise ToolException("todos 不能为空：请给出至少一条可验证的待办")
        # 回显规范化结果，模型后续轮次能看到自己的计划被如何理解
        return json.dumps(plan, ensure_ascii=False)

    return StructuredTool.from_function(
        func=todo_write,
        name=TodoWriteToolName,
        description=(
            "发布或修订执行计划。这是 planner 唯一的输出方式："
            "传入 plan_summary、todos、acceptance_criteria、verification_commands。"
        ),
        args_schema=TodoWriteInput,
        handle_validation_error=True,
    )


#: 现成实例：`model.bind_tools([TodoWriteTool])`
TodoWriteTool = build_todo_write_tool()


# --------------------------------------------------------------------------- #
# TodoUpdateTool
# --------------------------------------------------------------------------- #
def build_todo_update_tool(state: RuntimeState) -> StructuredTool:
    """构造 TodoUpdateTool，变更记录进 `state.metadata["todo_updates"]`。"""

    def todo_update(todo_id: str, status: str, note: str = "") -> str:
        todo_id = str(todo_id or "").strip()
        if not todo_id:
            raise ToolException("todo_id 不能为空")

        normalized = normalize_status(status)
        if normalized is None:
            raise ToolException(
                f"非法状态 {status!r}；允许的取值：{', '.join(TODO_STATUSES)}"
            )

        recorder = state.metadata.setdefault(TODO_UPDATES_KEY, [])
        if not isinstance(recorder, list):  # pragma: no cover - 防御性
            recorder = []
            state.metadata[TODO_UPDATES_KEY] = recorder
        recorder.append({"id": todo_id, "status": normalized, "note": str(note or "").strip()})

        return f"已记录：{todo_id} → {normalized}"

    return StructuredTool.from_function(
        func=todo_update,
        name=TodoUpdateToolName,
        description=(
            "更新某条待办的状态（pending / in_progress / completed / blocked），"
            "可在开工时标记 in_progress，完成后标记 completed，卡住时标记 blocked 并写 note。"
        ),
        args_schema=TodoUpdateInput,
        handle_validation_error=True,
    )


def drain_todo_updates(state: RuntimeState) -> list[dict[str, str]]:
    """取出并清空本轮记录的状态变更（由节点合并回图状态）。"""
    recorder = state.metadata.pop(TODO_UPDATES_KEY, [])
    if not isinstance(recorder, list):
        return []
    return [dict(item) for item in recorder if isinstance(item, Mapping)]


def apply_todo_updates(
    todos: Sequence[Mapping[str, Any]] | None,
    updates: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """把变更应用到待办列表上（未知 id 忽略），返回新列表。"""
    merged = [dict(todo) for todo in (todos or [])]
    index = {str(todo.get("id")): todo for todo in merged}

    for update in updates:
        todo = index.get(str(update.get("id")))
        if todo is None:
            continue
        status = normalize_status(update.get("status"))
        if status:
            todo["status"] = status
        note = str(update.get("note") or "").strip()
        if note:
            todo["note"] = note
    return merged


# --------------------------------------------------------------------------- #
# 持久化（TODO.md）
# --------------------------------------------------------------------------- #
#: 状态 → markdown 复选框标记
_STATUS_MARKS = {"pending": " ", "in_progress": "~", "completed": "x", "blocked": "!"}


def render_todos_markdown(
    todos: Sequence[Mapping[str, Any]],
    *,
    plan_summary: str = "",
) -> str:
    """把待办渲染成 `TODO.md` 的内容。"""
    lines = ["# TODO", ""]
    if plan_summary.strip():
        lines.extend([plan_summary.strip(), ""])

    for todo in todos:
        status = str(todo.get("status") or "pending")
        mark = _STATUS_MARKS.get(status, " ")
        note = str(todo.get("note") or "").strip()
        line = f"- [{mark}] {todo.get('id')}: {todo.get('content')}  ({status})"
        if note:
            line += f" — {note}"
        lines.append(line)

    if len(lines) == 2:  # 只有标题 + 空行
        lines.append("(暂无待办)")
    return "\n".join(lines) + "\n"


def persist_todos(
    state: RuntimeState,
    todos: Sequence[Mapping[str, Any]],
    *,
    plan_summary: str = "",
) -> str | None:
    """把待办写进 `workspace/TODO.md`。

    尽力而为：持久化失败不该打断 Agent（返回 None 表示没写成）。
    """
    try:
        path = state.resolve(TODO_FILENAME)
        path.write_text(
            render_todos_markdown(todos, plan_summary=plan_summary),
            encoding="utf-8",
            newline="",
        )
    except Exception:  # noqa: BLE001 - 见上：持久化是尽力而为
        return None
    return state.display(path)
