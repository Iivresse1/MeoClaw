"""工具注册：把 `RuntimeState` 绑定成一组 StructuredTool。

对外的唯一入口是 `build_tools(state)`，返回值可直接交给
`model.bind_tools(tools)`；顺序固定，便于 prompt 缓存与测试断言。
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from meoclaw.core.state import RuntimeState
from meoclaw.tools.bash_tool import build_bash_tool, run_bash
from meoclaw.tools.file_tools import build_file_tools, edit_file, read_file, write_file
from meoclaw.tools.grep_tool import build_grep_tool, grep
from meoclaw.tools.notepad_tool import build_notepad_tools
from meoclaw.tools.web_search_tool import build_web_search_tool

__all__ = [
    "READ_ONLY_TOOL_NAMES",
    "TOOL_NAMES",
    "build_read_only_tools",
    "build_tool_map",
    "build_tools",
    "edit_file",
    "grep",
    "read_file",
    "run_bash",
    "write_file",
]

#: 注册顺序即工具列表顺序
TOOL_NAMES: tuple[str, ...] = (
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GrepTool",
    "BashTool",
)

#: 只读工具（验证节点用：能查证，但改不了任何东西）。
#: WebSearchTool 不触达工作区，所以对验证者是安全的（联网复核事实/来源）。
READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    "FileReadTool",
    "GrepTool",
    "NotepadReadTool",
    "WebSearchTool",
)


def build_tools(
    state: RuntimeState,
    *,
    include_bash: bool = True,
) -> list[StructuredTool]:
    """构建绑定到 `state.workspace` 的全部工具。

    Args:
        state: 运行时状态；其 workspace 决定所有工具的可见范围。

    Returns:
        与 `TOOL_NAMES` 顺序一致的 StructuredTool 列表。
    """
    tools: list[StructuredTool] = [*build_file_tools(state), build_grep_tool(state)]
    if include_bash:
        tools.append(build_bash_tool(state))

    names = tuple(tool.name for tool in tools)
    expected = TOOL_NAMES if include_bash else TOOL_NAMES[:-1]
    if names != expected:
        raise RuntimeError(f"工具注册结果不符合预期：期望 {expected}，实际 {names}")
    return tools


def build_tool_map(state: RuntimeState) -> dict[str, StructuredTool]:
    """返回 `{工具名: 工具}` 映射，便于按 model 的 tool_call 派发。"""
    return {tool.name: tool for tool in build_tools(state)}


def build_read_only_tools(state: RuntimeState) -> list[StructuredTool]:
    """只读工具集：`FileReadTool` + `GrepTool` + `NotepadReadTool` + `WebSearchTool`（验证节点专用）。

    刻意不含 FileWriteTool / FileEditTool / BashTool / NotepadAppendTool ——
    验证者只能查证，不能改动工作区，也不能借 shell 绕过只读约束。
    （`VERIFIER_PROMPT` 里点名了 NotepadReadTool 与 WebSearchTool。）
    """
    candidates = [
        *build_file_tools(state),
        build_grep_tool(state),
        *build_notepad_tools(state),
        build_web_search_tool(),
    ]
    by_name = {tool.name: tool for tool in candidates}
    read_only = [by_name[name] for name in READ_ONLY_TOOL_NAMES if name in by_name]

    names = tuple(tool.name for tool in read_only)
    if names != READ_ONLY_TOOL_NAMES:
        raise RuntimeError(
            f"只读工具集不符合预期：期望 {READ_ONLY_TOOL_NAMES}，实际 {names}"
        )
    return read_only
