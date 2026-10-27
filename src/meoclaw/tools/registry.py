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
from meoclaw.tools.web_fetch_tool import build_web_fetch_tool
from meoclaw.tools.web_search_tool import build_web_search_tool

__all__ = [
    "READ_ONLY_TOOL_NAMES",
    "TOOL_NAMES",
    "WEB_TOOL_NAMES",
    "build_read_only_tools",
    "build_tool_map",
    "build_tools",
    "build_web_tools",
    "edit_file",
    "grep",
    "read_file",
    "run_bash",
    "write_file",
]

WEB_TOOL_NAMES: tuple[str, ...] = ("WebSearchTool", "WebFetchTool")

#: 注册顺序即工具列表顺序
TOOL_NAMES: tuple[str, ...] = (
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GrepTool",
    "BashTool",
    *WEB_TOOL_NAMES,
)

#: 只读工具（验证节点用：能查证，但改不了任何东西）。
#: 联网工具不触达工作区，所以对验证者是安全的（复核事实与原文）。
READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    "FileReadTool",
    "GrepTool",
    "NotepadReadTool",
    *WEB_TOOL_NAMES,
)


def build_web_tools(api_key: str | None = None) -> list[StructuredTool]:
    """Build the read-only web tools shared by all execution modes."""
    tools = [
        build_web_search_tool(api_key=api_key),
        build_web_fetch_tool(api_key=api_key),
    ]
    names = tuple(tool.name for tool in tools)
    if names != WEB_TOOL_NAMES:
        raise RuntimeError(
            f"联网工具注册结果不符合预期：期望 {WEB_TOOL_NAMES}，实际 {names}"
        )
    return tools


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
    tools.extend(build_web_tools())

    names = tuple(tool.name for tool in tools)
    local_tools = TOOL_NAMES[: -len(WEB_TOOL_NAMES)]
    expected = (
        (*local_tools, *WEB_TOOL_NAMES)
        if include_bash
        else (*local_tools[:-1], *WEB_TOOL_NAMES)
    )
    if names != expected:
        raise RuntimeError(f"工具注册结果不符合预期：期望 {expected}，实际 {names}")
    return tools


def build_tool_map(state: RuntimeState) -> dict[str, StructuredTool]:
    """返回 `{工具名: 工具}` 映射，便于按 model 的 tool_call 派发。"""
    return {tool.name: tool for tool in build_tools(state)}


def build_read_only_tools(state: RuntimeState) -> list[StructuredTool]:
    """只读工具集：文件读取、Grep、笔记读取、WebSearch、WebFetch。

    刻意不含 FileWriteTool / FileEditTool / BashTool / NotepadAppendTool ——
    验证者只能查证，不能改动工作区，也不能借 shell 绕过只读约束。
    （`VERIFIER_PROMPT` 里点名了联网工具。）
    """
    candidates = [
        *build_file_tools(state),
        build_grep_tool(state),
        *build_notepad_tools(state),
        *build_web_tools(),
    ]
    by_name = {tool.name: tool for tool in candidates}
    read_only = [by_name[name] for name in READ_ONLY_TOOL_NAMES if name in by_name]

    names = tuple(tool.name for tool in read_only)
    if names != READ_ONLY_TOOL_NAMES:
        raise RuntimeError(
            f"只读工具集不符合预期：期望 {READ_ONLY_TOOL_NAMES}，实际 {names}"
        )
    return read_only
