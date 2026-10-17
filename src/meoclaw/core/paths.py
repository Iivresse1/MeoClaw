"""工作区路径工具。

所有文件类工具在真正读写之前，都必须先经过 `resolve_in_workspace()`：
它会把路径规范化（展开 `~`、解析相对路径、跟随符号链接、折叠 `..`），
然后确认结果仍然位于 workspace 内部，越界就抛 `WorkspaceError`。

这一层是"工具不出工作区"的唯一执行点，不要在别处重复实现。
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "WorkspaceError",
    "display_path",
    "is_within",
    "resolve_in_workspace",
    "resolve_workspace",
]


class WorkspaceError(RuntimeError):
    """路径越界或工作区不可用时抛出。"""


def _as_path(value: str | Path) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def _is_blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def resolve_workspace(workspace: str | Path) -> Path:
    """把 workspace 解析为绝对目录路径，不存在时自动创建。

    Args:
        workspace: 工作区路径，可以是相对路径、`~` 展开路径。

    Returns:
        已解析（resolve 过）的绝对目录路径。

    Raises:
        WorkspaceError: 路径为空、已存在但不是目录、或无法创建。
    """
    if _is_blank(workspace):
        raise WorkspaceError("workspace 不能为空")

    raw = _as_path(workspace).expanduser()
    try:
        # strict=False：目标可能尚不存在，交给下面的 mkdir 创建。
        resolved = raw.resolve()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"无法解析 workspace {raw}: {exc}") from exc

    if resolved.exists() and not resolved.is_dir():
        raise WorkspaceError(f"workspace 必须是目录，但 {resolved} 是一个文件")

    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"无法创建工作区目录 {resolved}: {exc}") from exc

    if not resolved.is_dir():  # pragma: no cover - mkdir 之后仍然不是目录
        raise WorkspaceError(f"工作区不可用: {resolved}")

    # 目录可能是符号链接，创建后再解析一次拿到真实路径。
    return resolved.resolve()


def is_within(workspace: str | Path, target: str | Path) -> bool:
    """判断 target 是否位于 workspace 内（含 workspace 自身）。

    两侧都会先 resolve，因此符号链接指向外部、`..` 上跳都会被识别出来。
    解析失败（极端环境问题）时按"不在内部"处理（fail-closed）。
    """
    try:
        root = _as_path(workspace).resolve()
        candidate = _as_path(target).resolve()
    except (OSError, RuntimeError):
        # RuntimeError：Python 3.12 及更早版本对符号链接环会抛 RuntimeError
        return False
    return candidate == root or root in candidate.parents


def resolve_in_workspace(
    workspace: str | Path,
    target: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """在 workspace 内解析 target，越界即报错。

    Args:
        workspace: 工作区根目录（会被 resolve）。
        target: 目标路径。相对路径按 workspace 拼接；绝对路径也允许，
            但解析后必须仍落在 workspace 内。
        must_exist: 为 True 时要求目标已存在。

    Returns:
        解析后的绝对路径。

    Raises:
        WorkspaceError: 路径为空、越界，或 must_exist 但不存在。
    """
    root = _as_path(workspace).resolve()

    if _is_blank(target):
        raise WorkspaceError("file_path 不能为空")

    raw = _as_path(target).expanduser()
    candidate = raw if raw.is_absolute() else root / raw

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"无法解析路径 {raw}: {exc}") from exc

    if not is_within(root, resolved):
        raise WorkspaceError(f"路径越界：{raw} 解析为 {resolved}，不在工作区 {root} 内")

    if must_exist and not resolved.exists():
        raise WorkspaceError(f"路径不存在: {display_path(resolved, root)}")

    return resolved


def display_path(path: str | Path, workspace: str | Path) -> str:
    """尽量用相对 workspace 的形式展示路径，越界时回退为绝对路径。"""
    root = _as_path(workspace).resolve()
    resolved = _as_path(path).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return str(resolved)
    return "." if str(relative) == "." else relative.as_posix()
