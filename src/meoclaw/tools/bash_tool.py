"""BashTool：在 workspace 目录下执行 shell 命令，带超时与输出截断。

安全边界说明（重要）：
- `cwd` 固定为 workspace，路径类工具的"越界即拒绝"由 core/paths.py 保证；
- 但 shell 命令本身无法被 Python 真正沙箱化 —— `cd ..`、绝对路径读写依然可能发生。
  需要强隔离时，请把工作区放进容器 / 低权限用户里运行。
"""

from __future__ import annotations

import locale
import os
import signal
import subprocess
import time
from typing import Any, TypedDict

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from meoclaw.core.state import RuntimeState

__all__ = [
    "BashInput",
    "CommandOutcome",
    "build_bash_tool",
    "execute_command",
    "format_command_report",
    "run_bash",
]


class CommandOutcome(TypedDict):
    """一次命令执行的结构化结果（供验证节点直接消费）。"""

    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    timeout_seconds: int
    elapsed_seconds: float

#: 超时后等待进程真正退出的宽限时间（秒）
KILL_GRACE_SECONDS = 10


class BashInput(BaseModel):
    """BashTool 入参。"""

    command: str = Field(..., description="要执行的 shell 命令，工作目录为 workspace 根目录")
    timeout_seconds: int | None = Field(
        None,
        ge=1,
        le=3600,
        description="超时秒数，留空则使用默认值（120 秒）",
    )


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


def _truncate(text: str, limit: int) -> str:
    """超长时保留头部和尾部：报错、断言失败、堆栈通常在输出末尾。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head_size = int(limit * 0.6)
    tail_size = limit - head_size
    omitted = len(text) - head_size - tail_size
    tail = text[-tail_size:] if tail_size > 0 else ""
    return f"{text[:head_size]}\n…（省略中间 {omitted} 字符，原文共 {len(text)} 字符）…\n{tail}"


def _drain_output(proc: subprocess.Popen) -> tuple[bytes, bytes]:  # type: ignore[type-arg]
    """回收残余输出。

    进程被终止后再 `communicate()` 仍可能被"还握着管道的后台孙进程"卡住，
    所以这里最多再等 `KILL_GRACE_SECONDS` 秒，之后放弃剩余输出而不是无限挂起。
    """
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
        proc.kill()
        return b"", b""
    return stdout_bytes or b"", stderr_bytes or b""


def _decode(data: bytes | None) -> str:
    """把子进程输出解码为文本，兼容 UTF-8 / 本地代码页（如 GBK）。"""
    if not data:
        return ""
    encodings = ["utf-8"]
    try:
        encodings.append(locale.getpreferredencoding(False))
    except Exception:  # pragma: no cover - 极端环境
        pass
    encodings.extend(["gbk", "latin-1"])
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _kill_process_tree(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """超时后尽力结束整棵进程树（shell 的子进程也要一起收掉）。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=KILL_GRACE_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
    try:
        proc.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
        proc.kill()


def execute_command(
    state: RuntimeState,
    command: str,
    timeout_seconds: int | None = None,
) -> CommandOutcome:
    """在 workspace 内执行命令，返回**结构化**结果。

    与 `run_bash()` 共用同一套实现：`run_bash` 只是把这里的结果格式化成文本。
    验证节点（graph/nodes.py）需要 `ok` / `exit_code` / `stdout` / `stderr` 这些字段，
    所以单独暴露出来。

    Args:
        state: 运行时状态，提供 workspace（作为 cwd）与默认超时。
        command: 要执行的命令字符串。
        timeout_seconds: 超时秒数，None 时取 `state.bash_timeout_seconds`。

    Returns:
        `CommandOutcome`。超时不抛异常，而是 `timed_out=True` 且 `ok=False`。
    """
    if not isinstance(command, str) or not command.strip():
        raise ToolException("command 不能为空")
    command = command.strip()
    timeout = _positive_int(timeout_seconds, "timeout_seconds", state.bash_timeout_seconds)

    popen_kwargs: dict[str, Any] = {
        "cwd": str(state.workspace),
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        # 独立进程组，便于超时后 taskkill /T 收掉整棵树
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    started = time.perf_counter()
    try:
        proc: subprocess.Popen[bytes] = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        raise ToolException(f"无法启动命令: {exc}") from exc

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        # 进程已被终止，这里只是回收残余输出（有上限，不会无限等待）
        stdout_bytes, stderr_bytes = _drain_output(proc)

    elapsed = time.perf_counter() - started
    exit_code = proc.returncode

    return {
        "command": command,
        "ok": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "stdout": _decode(stdout_bytes).strip("\n"),
        "stderr": _decode(stderr_bytes).strip("\n"),
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "elapsed_seconds": elapsed,
    }


def format_command_report(state: RuntimeState, outcome: CommandOutcome) -> str:
    """把 `CommandOutcome` 渲染成给模型看的文本报告。"""
    lines = [
        f"命令: {outcome['command']}",
        f"工作目录: {state.display(state.workspace)}",
        f"退出码: {outcome['exit_code']}"
        + (
            f"（超过 {outcome['timeout_seconds']} 秒仍未结束，进程已被强制终止）"
            if outcome["timed_out"]
            else ""
        ),
        f"耗时: {outcome['elapsed_seconds']:.2f}s",
    ]

    sections: list[str] = []
    if outcome["stdout"]:
        sections.extend(["--- stdout ---", outcome["stdout"]])
    if outcome["stderr"]:
        sections.extend(["--- stderr ---", outcome["stderr"]])
    if sections:
        # 截断预算按 stdout+stderr 合并计算，避免两路各占一份上限
        lines.append(_truncate("\n".join(sections), state.max_output_chars))
    else:
        lines.append("(命令没有产生输出)")

    return "\n".join(lines)


def run_bash(
    state: RuntimeState,
    command: str,
    timeout_seconds: int | None = None,
) -> str:
    """在 workspace 内执行 shell 命令，返回文本报告。

    Args:
        state: 运行时状态，提供 workspace（作为 cwd）与默认超时。
        command: 要执行的命令字符串。
        timeout_seconds: 超时秒数，None 时取 `state.bash_timeout_seconds`。

    Returns:
        包含命令、cwd、退出码、耗时与 stdout/stderr 的文本报告。
        超时不会抛异常，而是在报告中标注"进程已被终止"。
    """
    return format_command_report(state, execute_command(state, command, timeout_seconds))


def build_bash_tool(state: RuntimeState) -> StructuredTool:
    """把 BashTool 绑定到 `state`。"""

    def bash_tool(command: str, timeout_seconds: int | None = None) -> str:
        return run_bash(state, command, timeout_seconds=timeout_seconds)

    return StructuredTool.from_function(
        func=bash_tool,
        name="BashTool",
        description=(
            "在工作区根目录下执行 shell 命令并返回退出码与输出。"
            "适合运行测试、构建、git 等；命令超时会被强制终止，"
            "需要更长执行时间时请显式指定 timeout_seconds。"
        ),
        args_schema=BashInput,
        # 业务失败抛 ToolException，由调用方转成文本（见 core/agent.py 的 execute_tool）
        handle_validation_error=True,
    )
