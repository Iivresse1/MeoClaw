"""GrepTool：在工作区内做正则搜索。

不依赖外部 ripgrep 可执行文件：直接用 `re` + 文件遍历实现，行为可预测。
搜索起点通过 `resolve_tool_path()` 做安检，遍历出的每个候选文件还会再安检一次，
因此 `..` 或指向工作区外的符号链接都不会被读取。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from meoclaw.core.paths import WorkspaceError
from meoclaw.core.state import RuntimeState
from meoclaw.tools.file_tools import resolve_tool_path

__all__ = [
    "MAX_LINE_WIDTH",
    "SKIP_DIRS",
    "GrepInput",
    "build_grep_tool",
    "grep",
]

#: 单行命中内容的显示上限
MAX_LINE_WIDTH = 400
#: 遍历时直接跳过的目录（体积大或与源码无关）
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        "dist",
        "build",
    }
)
#: 判定二进制文件时检查的文件头长度
BINARY_SNIFF_BYTES = 8192


class GrepInput(BaseModel):
    """GrepTool 入参。"""

    pattern: str = Field(..., description="Python 正则表达式，例如 'def main|class Foo'")
    path: str = Field(".", description="相对 workspace 的搜索起点，可以是目录或单个文件")
    glob: str | None = Field(
        None,
        description="文件名过滤，例如 '*.py' 或 '**/*.py'；留空表示不限制",
    )
    head_limit: int | None = Field(
        None,
        ge=1,
        le=5000,
        description="最多返回的命中条数；留空则使用 RuntimeState.grep_head_limit（默认 100）",
    )
    ignore_case: bool = Field(False, description="是否忽略大小写")


def _positive_int(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ToolException(f"{name} 必须是整数，收到 {value!r}") from exc
    if number < 1:
        raise ToolException(f"{name} 必须大于 0，收到 {number}")
    return number


def _clip(line: str, width: int = MAX_LINE_WIDTH) -> str:
    if len(line) <= width:
        return line
    return f"{line[:width]}…（该行共 {len(line)} 字符，已截断）"


def _in_skip_dir(path: Path, root: Path) -> bool:
    """路径中是否含有应跳过的目录段。"""
    try:
        parts = path.relative_to(root).parts
    except ValueError:  # pragma: no cover - path 必然在 root 之下
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def _iter_files(root: Path, pattern: str | None) -> list[Path]:
    """列出待搜索文件（已排序、已剪枝）。

    注意：这里只做"词法"遍历，返回值仍可能因为 glob 里的 `..` 或符号链接
    指向工作区之外，所以调用方必须逐个再走一次 `state.resolve()` 安检。
    """
    if root.is_file():
        return [root]

    if pattern:
        if ".." in Path(pattern).parts:
            raise ToolException(f"glob 不允许包含 '..'（会跳出工作区）: {pattern!r}")
        try:
            candidates = sorted(root.rglob(pattern))
        except (ValueError, NotImplementedError, OSError) as exc:
            raise ToolException(f"glob 无效 {pattern!r}: {exc}") from exc
        return [item for item in candidates if item.is_file() and not _in_skip_dir(item, root)]

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        if _in_skip_dir(base, root):
            dirnames[:] = []
            continue
        files.extend(base / name for name in sorted(filenames))
    return files


def grep(
    state: RuntimeState,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    head_limit: int | None = None,
    ignore_case: bool = False,
) -> str:
    """在 workspace 内用正则搜索文件内容。

    Args:
        state: 运行时状态，提供 workspace 与默认命中上限。
        pattern: Python 正则表达式。
        path: 搜索起点，相对 workspace 的目录或文件。
        glob: 文件名过滤（如 `*.py`），None 表示遍历所有文本文件。
        head_limit: 最多返回的命中条数，None 时取 `state.grep_head_limit`。
        ignore_case: 是否忽略大小写。

    Returns:
        头部统计 + 每行 `相对路径:行号:内容`。
    """
    if not pattern:
        raise ToolException("pattern 不能为空")

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolException(f"正则表达式无效 {pattern!r}: {exc}") from exc

    root = resolve_tool_path(state, path if path else ".", must_exist=True)
    limit = _positive_int(head_limit, "head_limit", state.grep_head_limit)

    matches: list[str] = []
    scanned = 0
    binary_skipped = 0
    escaped = 0
    truncated = False

    for candidate in _iter_files(root, glob):
        try:
            # 二次安检：解析符号链接后仍必须落在 workspace 内
            file = state.resolve(candidate)
        except WorkspaceError:
            escaped += 1
            continue

        try:
            if file.stat().st_size > state.max_read_bytes:
                continue
            data = file.read_bytes()
        except OSError:
            # 单个文件读不了（权限、竞态删除等）不应该让整次搜索失败
            continue

        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            binary_skipped += 1
            continue

        scanned += 1
        display = state.display(file)
        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{display}:{line_number}:{_clip(line)}")
                if len(matches) >= limit:
                    truncated = True
                    break
        if truncated:
            break

    if not matches:
        return f"在 {state.display(root)} 内没有匹配 {pattern!r} 的内容（已扫描 {scanned} 个文件）"

    header = f"匹配 {len(matches)} 处（已扫描 {scanned} 个文件）"
    if truncated:
        header += f"，已达到 head_limit={limit}，结果被截断"
    if binary_skipped:
        header += f"，跳过 {binary_skipped} 个二进制文件"
    if escaped:
        header += f"，跳过 {escaped} 个越界或符号链接指向外部的文件"

    return "\n".join([header, *matches])


def build_grep_tool(state: RuntimeState) -> StructuredTool:
    """把 GrepTool 绑定到 `state`。"""

    def grep_tool(
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        head_limit: int | None = None,
        ignore_case: bool = False,
    ) -> str:
        return grep(
            state,
            pattern,
            path=path,
            glob=glob,
            head_limit=head_limit,
            ignore_case=ignore_case,
        )

    return StructuredTool.from_function(
        func=grep_tool,
        name="GrepTool",
        description=(
            "在工作区内用正则表达式搜索文件内容，返回 `文件:行号:内容`。"
            "适合定位符号定义、报错信息、TODO 等；比逐个 FileReadTool 快得多。"
        ),
        args_schema=GrepInput,
        # 业务失败抛 ToolException，由调用方转成文本（见 core/agent.py 的 execute_tool）
        handle_validation_error=True,
    )
