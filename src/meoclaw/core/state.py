"""运行时状态。

`RuntimeState.workspace` 是所有工具唯一允许触达的目录；
另外几个字段是各类工具的默认预算（读取上限、输出上限、超时、搜索条数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meoclaw.core.paths import (
    display_path,
    resolve_in_workspace,
    resolve_workspace,
)

__all__ = [
    "DEFAULT_BASH_TIMEOUT_SECONDS",
    "DEFAULT_GREP_HEAD_LIMIT",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_MAX_READ_BYTES",
    "RuntimeState",
]

#: 单个文件最大读取字节数（默认 1 MiB）
DEFAULT_MAX_READ_BYTES = 1_000_000
#: 单个工具返回给模型的最大字符数
DEFAULT_MAX_OUTPUT_CHARS = 20_000
#: BashTool 默认超时（秒）
DEFAULT_BASH_TIMEOUT_SECONDS = 120
#: GrepTool 默认最大命中条数
DEFAULT_GREP_HEAD_LIMIT = 100


def _positive_int(value: object, name: str) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数，收到 {value!r}") from exc
    if number <= 0:
        raise ValueError(f"{name} 必须大于 0，收到 {number}")
    return number


@dataclass
class RuntimeState:
    """一次运行期间共享的状态对象。

    Attributes:
        workspace: 工作区根目录，构造时自动创建并解析为绝对路径。
        max_read_bytes: 单文件读取上限，超过则拒绝读取。
        max_output_chars: 工具输出截断阈值。
        bash_timeout_seconds: BashTool 默认超时。
        grep_head_limit: GrepTool 默认最大命中数。
        metadata: 预留的扩展字典，工具可自行读写。
    """

    workspace: Path
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    bash_timeout_seconds: int = DEFAULT_BASH_TIMEOUT_SECONDS
    grep_head_limit: int = DEFAULT_GREP_HEAD_LIMIT
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 允许传入 str / Path，统一规范化并确保目录存在。
        self.workspace = resolve_workspace(self.workspace)

        self.max_read_bytes = _positive_int(self.max_read_bytes, "max_read_bytes")
        self.max_output_chars = _positive_int(self.max_output_chars, "max_output_chars")
        self.bash_timeout_seconds = _positive_int(
            self.bash_timeout_seconds, "bash_timeout_seconds"
        )
        self.grep_head_limit = _positive_int(self.grep_head_limit, "grep_head_limit")

        if self.metadata is None:
            self.metadata = {}

    @property
    def root(self) -> Path:
        """workspace 的别名，语义上更贴近根目录。"""
        return self.workspace

    def resolve(self, file_path: str | Path, *, must_exist: bool = False) -> Path:
        """在 workspace 内解析路径，越界抛 WorkspaceError。"""
        return resolve_in_workspace(self.workspace, file_path, must_exist=must_exist)

    def display(self, path: str | Path) -> str:
        """把绝对路径显示为相对 workspace 的形式。"""
        return display_path(path, self.workspace)

    def describe(self) -> dict[str, object]:
        """返回可打印的状态摘要。"""
        return {
            "workspace": str(self.workspace),
            "max_read_bytes": self.max_read_bytes,
            "max_output_chars": self.max_output_chars,
            "bash_timeout_seconds": self.bash_timeout_seconds,
            "grep_head_limit": self.grep_head_limit,
        }
