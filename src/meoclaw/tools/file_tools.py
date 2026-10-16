"""文件工具：读取、写入、精确替换。

约定：
- 三个核心函数都接收 `state` 作为第一个参数，并在动作前先调用
  `resolve_tool_path()` 做路径安全检查，任何越界都会被拒绝。
- 可预期的失败（路径不存在/越界、匹配到多处、内容为空等）抛 `ToolException`；
  由调用方转成模型可见的文本——`core.agent.execute_tool` 与 LangGraph 的 `ToolNode`
  都会捕获 `ToolException` 并返回其 message。
- 写入统一使用 LF，不做换行转换；编辑会保持文件原有的 CRLF/LF 风格。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from meoclaw.core.paths import WorkspaceError
from meoclaw.core.state import RuntimeState

__all__ = [
    "MAX_LINE_WIDTH",
    "FileEditInput",
    "FileReadInput",
    "FileWriteInput",
    "build_file_tools",
    "edit_file",
    "read_file",
    "resolve_tool_path",
    "write_file",
]

#: 单行内容在输出中的显示上限，避免超长行撑爆上下文
MAX_LINE_WIDTH = 2000
#: 判定二进制文件时检查的文件头长度
BINARY_SNIFF_BYTES = 8192


# --------------------------------------------------------------------------- #
# 参数 schema：显式声明，保证 bind_tools() 生成清晰的 function schema
# --------------------------------------------------------------------------- #
class FileReadInput(BaseModel):
    """FileReadTool 入参。"""

    file_path: str = Field(..., description="相对 workspace 的文件路径，例如 'src/app.py'")
    offset: int = Field(0, ge=0, description="起始行号，0 表示从第 1 行开始")
    limit: int = Field(2000, ge=1, le=20000, description="最多读取的行数，默认 2000")


class FileWriteInput(BaseModel):
    """FileWriteTool 入参。"""

    file_path: str = Field(..., description="相对 workspace 的文件路径，父目录会自动创建")
    content: str = Field(..., description="完整文件内容（整体覆写，不是追加）")


class FileEditInput(BaseModel):
    """FileEditTool 入参。"""

    file_path: str = Field(..., description="相对 workspace 的文件路径，文件必须已存在")
    old_text: str = Field(..., description="要被替换的原文，必须在文件中恰好出现一次")
    new_text: str = Field("", description="替换后的新文本，允许为空字符串（表示删除）")


# --------------------------------------------------------------------------- #
# 内部工具函数
# --------------------------------------------------------------------------- #
def _positive_int(value: object, name: str, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ToolException(f"{name} 不能为空")
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ToolException(f"{name} 必须是整数，收到 {value!r}") from exc
    if number < 1:
        raise ToolException(f"{name} 必须大于 0，收到 {number}")
    return number


def _non_negative_int(value: object, name: str, default: int = 0) -> int:
    if value is None:
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ToolException(f"{name} 必须是整数，收到 {value!r}") from exc
    if number < 0:
        raise ToolException(f"{name} 不能为负数，收到 {number}")
    return number


def _clip(line: str, width: int = MAX_LINE_WIDTH) -> str:
    if len(line) <= width:
        return line
    return f"{line[:width]}…（该行共 {len(line)} 字符，已截断）"


def _read_text_bytes(state: RuntimeState, path: Path) -> bytes:
    """读取文件原始字节，附带大小与二进制检查。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ToolException(f"无法读取 {state.display(path)}: {exc}") from exc
    if size > state.max_read_bytes:
        raise ToolException(
            f"{state.display(path)} 为 {size} 字节，超过上限 {state.max_read_bytes} 字节；"
            "请缩小读取范围或调大 RuntimeState.max_read_bytes"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolException(f"无法读取 {state.display(path)}: {exc}") from exc
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        raise ToolException(f"{state.display(path)} 看起来是二进制文件，无法按文本处理")
    return data


# --------------------------------------------------------------------------- #
# 核心实现
# --------------------------------------------------------------------------- #
def resolve_tool_path(
    state: RuntimeState,
    target: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """工具层统一的路径安检入口。

    把 core 层的 `WorkspaceError` 翻译成 `ToolException`：这样"路径不存在"和
    "路径越界"都会以工具结果的形式回到模型（`core.agent.execute_tool` 捕获后转成文本，
    LangGraph 的 `ToolNode` 也会捕获 `ToolException`），一次写错路径不会打断 Agent 循环；
    越界依然被硬性拒绝。

    注意：core/paths.py 本身保持 langchain 无关，直接调用它仍然抛 WorkspaceError。
    """
    try:
        return state.resolve(target, must_exist=must_exist)
    except WorkspaceError as exc:
        raise ToolException(str(exc)) from exc


def read_file(
    state: RuntimeState,
    file_path: str,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """读取工作区内文件，返回带行号的文本。

    Args:
        state: 运行时状态，提供 workspace 与读取上限。
        file_path: 相对（或绝对但仍在 workspace 内）的文件路径。
        offset: 起始行号，0 表示第 1 行。
        limit: 最多读取的行数。

    Returns:
        形如 "path 第 1-3 行 / 共 10 行" 的头部 + 带行号内容。
    """
    path = resolve_tool_path(state, file_path, must_exist=True)
    if path.is_dir():
        raise ToolException(f"{state.display(path)} 是目录；搜索目录内容请使用 GrepTool")

    offset = _non_negative_int(offset, "offset")
    limit = _positive_int(limit, "limit")

    data = _read_text_bytes(state, path)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    display = state.display(path)

    if total == 0:
        return f"{display} 是空文件（0 行）"
    if offset >= total:
        raise ToolException(f"offset={offset} 超出范围：{display} 只有 {total} 行")

    window = lines[offset : offset + limit]
    end = offset + len(window)
    width = len(str(end))
    body = "\n".join(
        f"{offset + index + 1:>{width}}\t{_clip(line)}" for index, line in enumerate(window)
    )
    header = f"{display} 第 {offset + 1}-{end} 行 / 共 {total} 行"
    if end < total:
        header += f"（还有 {total - end} 行，可用 offset={end} 继续读取）"
    return f"{header}\n{body}"


def write_file(state: RuntimeState, file_path: str, content: str) -> str:
    """创建或整体覆写工作区内的文件，父目录自动创建。"""
    path = resolve_tool_path(state, file_path)
    if path.exists() and path.is_dir():
        raise ToolException(f"{state.display(path)} 是目录，不能写入文件内容")
    if content is None:
        raise ToolException("content 不能为 None")
    if not isinstance(content, str):
        content = str(content)

    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" 关闭换行转换：写入内容与给定字符串逐字节一致（统一 LF）。
        path.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        raise ToolException(f"写入 {state.display(path)} 失败: {exc}") from exc

    action = "覆写" if existed else "创建"
    return f"已{action} {state.display(path)}（{len(content)} 字符，{len(content.splitlines())} 行）"


def edit_file(
    state: RuntimeState,
    file_path: str,
    old_text: str,
    new_text: str,
) -> str:
    """把文件中的 `old_text` 替换为 `new_text`。

    `old_text` 必须在文件中恰好出现一次：出现 0 次或多次都会抛 ToolException。
    """
    path = resolve_tool_path(state, file_path, must_exist=True)
    if path.is_dir():
        raise ToolException(f"{state.display(path)} 是目录，不能编辑")
    if old_text is None or old_text == "":
        raise ToolException("old_text 不能为空；请提供需要被替换的原始片段")
    if new_text is None:
        raise ToolException("new_text 不能为 None")

    data = _read_text_bytes(state, path)
    display = state.display(path)

    # 编辑必须严格按 UTF-8 解码：用 errors="replace" 会把非法字节变成 U+FFFD，
    # 写回时等于悄悄损坏原文件（例如 GBK 编码的中文）。宁可拒绝，也不要破坏。
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolException(
            f"{display} 不是有效的 UTF-8 文本，拒绝编辑以免破坏原文件；"
            "如需处理请先用 BashTool 转换编码"
        ) from exc

    # 文件可能是 CRLF；先按原文匹配，再试 CRLF 变体，从而保持原有换行风格。
    # 目标是"逻辑位置唯一"：只有当同一片段出现在两个真正不同的位置时才报错
    # （例如文件里既有 LF 版本又有 CRLF 版本）。同一位置的重叠匹配（如 old_text
    # 以 \n 开头时，"\ntwo" 与 "\r\ntwo" 指向同一处）取更长的那个即可。
    candidates = [old_text]
    if "\r\n" in raw and "\r\n" not in old_text:
        crlf_variant = old_text.replace("\n", "\r\n")
        if crlf_variant != old_text:
            candidates.append(crlf_variant)

    spans: list[tuple[int, str]] = []
    for candidate in candidates:
        count = raw.count(candidate)
        if count == 0:
            continue
        if count > 1:
            raise ToolException(
                f"old_text 在 {display} 中匹配到 {count} 处，无法确定替换目标；"
                "请带上更多上下文，让片段唯一"
            )
        spans.append((raw.index(candidate), candidate))

    if not spans:
        raise ToolException(
            f"old_text 未在 {display} 中找到；请先用 FileReadTool 确认原文（注意空格、缩进与换行）"
        )

    spans.sort(key=lambda item: (item[0], -len(item[1])))
    merged: list[tuple[int, str]] = []
    for offset, candidate in spans:
        if merged and offset < merged[-1][0] + len(merged[-1][1]):
            continue  # 与上一处重叠：视为同一个位置
        merged.append((offset, candidate))

    if len(merged) > 1:
        raise ToolException(
            f"old_text 在 {display} 中同时匹配到 LF 与 CRLF 两种写法（位置不同），"
            "无法确定唯一目标；请缩短片段，或先统一文件换行符"
        )

    index, matched = merged[0]
    line_number = raw.count("\n", 0, index) + 1

    replacement = new_text
    if matched != old_text:
        # 命中的是 CRLF 变体，写回时沿用 CRLF。
        replacement = new_text.replace("\r\n", "\n").replace("\n", "\r\n")

    updated = raw[:index] + replacement + raw[index + len(matched) :]
    if updated == raw:
        return f"{display} 第 {line_number} 行的内容与替换结果一致，未做改动"

    try:
        path.write_text(updated, encoding="utf-8", newline="")
    except OSError as exc:
        raise ToolException(f"写回 {display} 失败: {exc}") from exc

    return (
        f"已更新 {display}：替换第 {line_number} 行的片段"
        f"（{len(matched)} 字符 → {len(replacement)} 字符）"
    )


# --------------------------------------------------------------------------- #
# StructuredTool 工厂
# --------------------------------------------------------------------------- #
def build_file_tools(state: RuntimeState) -> list[StructuredTool]:
    """把三个文件工具绑定到 `state`，返回 StructuredTool 列表。"""

    def file_read(file_path: str, offset: int = 0, limit: int = 2000) -> str:
        return read_file(state, file_path, offset=offset, limit=limit)

    def file_write(file_path: str, content: str) -> str:
        return write_file(state, file_path, content)

    def file_edit(file_path: str, old_text: str, new_text: str = "") -> str:
        return edit_file(state, file_path, old_text=old_text, new_text=new_text)

    # 只保留参数校验错误兜底：业务失败抛 ToolException，由调用方转成工具结果文本
    # （core.agent.execute_tool / LangGraph ToolNode 都会这么处理）。
    # 如果这里也设 handle_tool_error=True，失败会被当成"成功返回"，
    # 调用方就无法通过 ok 标志区分成功与失败了。
    common: dict[str, object] = {"handle_validation_error": True}

    return [
        StructuredTool.from_function(
            func=file_read,
            name="FileReadTool",
            description=(
                "读取工作区内某个文本文件的内容，返回带行号的文本。"
                "offset 是起始行号（0 表示第 1 行），limit 是最多读取的行数。"
                "修改文件前先用它确认原文。"
            ),
            args_schema=FileReadInput,
            **common,
        ),
        StructuredTool.from_function(
            func=file_write,
            name="FileWriteTool",
            description=(
                "在工作区内创建或整体覆写一个文件，父目录会自动创建。"
                "content 是完整文件内容；只改局部时请优先使用 FileEditTool。"
            ),
            args_schema=FileWriteInput,
            **common,
        ),
        StructuredTool.from_function(
            func=file_edit,
            name="FileEditTool",
            description=(
                "在工作区内的文件中把 old_text 精确替换为 new_text。"
                "old_text 必须在文件中恰好出现一次：出现 0 次或多次都会失败，"
                "所以请带上足够的上下文保证唯一。"
            ),
            args_schema=FileEditInput,
            **common,
        ),
    ]
