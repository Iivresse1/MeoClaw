"""NOTEPAD.md 读写工具：把"值得跨越压缩保留"的结论落到工作区文件里。

设计意图（见后续三层 Memory 阶段）：TODO.md 是工作计划的持久化，
NOTEPAD.md 是**长期笔记** —— 结论、决策、重要文件、阻塞点、下一步。
Agent 上下文被压缩后，可以靠 `NotepadReadTool` 把关键信息捞回来。
"""

from __future__ import annotations

from datetime import datetime

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from meoclaw.core.state import RuntimeState
from meoclaw.tools.file_tools import resolve_tool_path

__all__ = [
    "DEFAULT_READ_CHARS",
    "NOTEPAD_FILENAME",
    "NotepadAppendInput",
    "NotepadReadInput",
    "build_notepad_tools",
    "notepad_append",
    "notepad_read",
]

#: 笔记文件名（工作区根目录）
NOTEPAD_FILENAME = "NOTEPAD.md"
#: 单次读取默认返回的字符数（从尾部取，越近的笔记越重要）
DEFAULT_READ_CHARS = 4000


class NotepadReadInput(BaseModel):
    """NotepadReadTool 入参。"""

    limit: int = Field(
        DEFAULT_READ_CHARS,
        ge=1,
        le=20000,
        description="最多返回多少字符（只保留最近的笔记）",
    )


class NotepadAppendInput(BaseModel):
    """NotepadAppendTool 入参。"""

    heading: str = Field(..., description="小节标题，例如 '决策'、'阻塞点'、'重要文件'")
    content: str = Field(
        ...,
        description="要长期保留的内容：结论、决策、重要文件、阻塞点、下一步计划",
    )


def notepad_read(state: RuntimeState, limit: int = DEFAULT_READ_CHARS) -> str:
    """读取 `NOTEPAD.md`（只保留最近 limit 个字符）。"""
    path = state.resolve(NOTEPAD_FILENAME)
    if not path.exists():
        return f"{NOTEPAD_FILENAME} 还没有内容（用 NotepadAppendTool 追加即会自动创建）"
    if path.is_dir():
        raise ToolException(f"{state.display(path)} 是目录，不是笔记文件")

    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolException(f"读取 {NOTEPAD_FILENAME} 失败: {exc}") from exc

    if not data.strip():
        return f"{NOTEPAD_FILENAME} 目前是空的"
    if len(data) <= limit:
        return data.rstrip("\n")
    return (
        f"…（只显示最近 {limit} 字符，全文共 {len(data)} 字符）\n"
        + data[-limit:].rstrip("\n")
    )


def notepad_append(state: RuntimeState, heading: str, content: str) -> str:
    """把一段内容追加到 `NOTEPAD.md`（带时间戳的小节）。"""
    heading = (heading or "").strip() or "笔记"
    content = (content or "").strip()
    if not content:
        raise ToolException("content 不能为空")

    path = resolve_tool_path(state, NOTEPAD_FILENAME)
    existing = ""
    if path.exists():
        if path.is_dir():
            raise ToolException(f"{state.display(path)} 是目录，不能当笔记文件")
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolException(f"读取 {NOTEPAD_FILENAME} 失败: {exc}") from exc
    if not existing.strip():
        existing = "# NOTEPAD\n"

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = f"{existing.rstrip()}\n\n## {heading}\n_({stamp})_\n\n{content}\n"

    if len(updated) > state.max_read_bytes:
        raise ToolException(
            f"{NOTEPAD_FILENAME} 将增长到 {len(updated)} 字符，超过上限 "
            f"{state.max_read_bytes}；请写得更精简，或先整理旧笔记"
        )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8", newline="")
    except OSError as exc:
        raise ToolException(f"写入 {NOTEPAD_FILENAME} 失败: {exc}") from exc

    return f"已追加到 {NOTEPAD_FILENAME} 的「{heading}」小节（当前 {len(updated)} 字符）"


def build_notepad_tools(state: RuntimeState) -> list[StructuredTool]:
    """构造 `[NotepadReadTool, NotepadAppendTool]`。"""

    def read_tool(limit: int = DEFAULT_READ_CHARS) -> str:
        return notepad_read(state, limit=limit)

    def append_tool(heading: str, content: str) -> str:
        return notepad_append(state, heading=heading, content=content)

    return [
        StructuredTool.from_function(
            func=read_tool,
            name="NotepadReadTool",
            description=(
                "读取工作区里的 NOTEPAD.md（长期笔记）：上下文被压缩后用它找回"
                "之前的结论、决策、重要文件和阻塞点。"
            ),
            args_schema=NotepadReadInput,
            handle_validation_error=True,
        ),
        StructuredTool.from_function(
            func=append_tool,
            name="NotepadAppendTool",
            description=(
                "把值得长期保留的内容追加到 NOTEPAD.md：结论、决策、重要文件、"
                "阻塞点、下一步。写进去的东西在上下文被压缩后依然能读到。"
            ),
            args_schema=NotepadAppendInput,
            handle_validation_error=True,
        ),
    ]
