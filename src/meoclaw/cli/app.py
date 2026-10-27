"""typer CLI 入口。

用法：

    meoclaw "<task>" --workspace <path> [--max-attempts 3]
    python -m meoclaw "<task>" -w ./workspace

只注册一个命令，因此 Typer 会把它折叠成根命令 —— 调用时不需要写子命令名。
执行阶段消费 `core.agent.stream_agent_events()` 的统一事件流（底层是
`build_workflow().stream(..., stream_mode=["updates", "custom"])`），用 rich 实时打印，
按节点分组显示：📋 Planner / 🔧 Actor / ✅❌ Verifier / 📝 Final。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import typer
from langchain_core.tools import StructuredTool
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from meoclaw import __version__
from meoclaw.core.agent import stream_agent_events
from meoclaw.core.paths import WorkspaceError
from meoclaw.core.state import RuntimeState
from meoclaw.providers.openai_provider import MissingAPIKeyError
from meoclaw.tools.registry import build_tools

#: 未指定 --workspace 时的默认工作区（相对当前目录，自动创建）
DEFAULT_WORKSPACE = Path("workspace")
#: 单次工具输出在终端里的显示上限
OUTPUT_PREVIEW_CHARS = 1200
#: 默认最大尝试轮数（与 RuntimeState / graph 的默认值保持一致）
DEFAULT_MAX_ATTEMPTS = 3

#: 节点 → 图标 / 标题
NODE_EMOJI = {
    "planner": "📋",
    "actor": "🔧",
    "verifier": "✅",
    "final": "📝",
}
NODE_TITLES = {
    "planner": "Planner",
    "actor": "Actor",
    "verifier": "Verifier",
    "final": "Final",
}

app = typer.Typer(
    add_completion=False,
    help="MeoClaw —— 工作区受限的编码智能体（typer + langgraph）。",
)


# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #
def _preview(text: str, limit: int = OUTPUT_PREVIEW_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（共 {len(text)} 字符，此处只显示前 {limit} 字符）"


def _can_encode(console: Console, text: str) -> bool:
    """目标输出流能否编码这段文本（GBK 控制台/重定向到文件时会用到）。"""
    stream = getattr(console, "file", None) or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _console_capabilities(console: Console) -> tuple[bool, box.Box]:
    """按输出编码选择 emoji 与边框样式。

    GBK 之类的编码既装不下 emoji 也装不下 rich 的圆角边框，
    直接打印会抛 UnicodeEncodeError —— 这里先探测再降级成 ASCII。
    """
    emoji_ok = _can_encode(console, "📋🔧✅❌📝")
    box_style = box.ROUNDED if _can_encode(console, "╭─╮│╰╯") else box.ASCII
    return emoji_ok, box_style


def _node_label(emoji_ok: bool, node: str) -> str:
    """节点标题：`📋 Planner`，编码不支持时退化为 `[Planner]`。"""
    title = NODE_TITLES.get(node, node or "Workflow")
    emoji = NODE_EMOJI.get(node)
    if emoji and emoji_ok:
        return f"{emoji} {title}"
    return f"[{title}]"


def _verifier_title(emoji_ok: bool, passed: bool, attempts: int, max_attempts: int) -> str:
    if emoji_ok:
        mark = "✅" if passed else "❌"
    else:
        mark = "[OK]" if passed else "[FAIL]"
    return f"{mark} Verifier（第 {attempts}/{max_attempts} 轮）"


def _print_state(state: RuntimeState) -> None:
    typer.secho("运行状态", bold=True)
    for key, value in state.describe().items():
        typer.echo(f"  {key}: {value}")


def _print_tools(tools: Sequence[StructuredTool]) -> None:
    typer.secho(f"可用工具（{len(tools)} 个）", bold=True)
    for tool in tools:
        typer.echo(f"  - {tool.name}: {tool.description}")
        schema = tool.args_schema
        if not isinstance(schema, type):
            continue
        try:
            fields = schema.model_json_schema()
        except Exception:  # noqa: BLE001, S112 - optional schema introspection
            continue
        required = set(fields.get("required", []))
        for field_name, field in fields.get("properties", {}).items():
            mark = "必填" if field_name in required else "可选"
            typer.echo(f"      · {field_name}（{mark}）：{field.get('description', '')}")


# --------------------------------------------------------------------------- #
# 事件流打印
# --------------------------------------------------------------------------- #
def _stream_events(
    console: Console,
    events: Iterator[dict[str, Any]],
    max_attempts: int,
    *,
    emoji_ok: bool,
    box_style: box.Box,
) -> int:
    """消费统一事件流并按节点实时打印，返回退出码（0 通过 / 1 未通过）。

    注意：所有来自模型/工具/文件的内容都用 rich.text.Text 组装，
    避免内容里的 "[Errno 2]"、"[/x]" 之类被当成 rich 标记解析而报 MarkupError。
    """
    final_answer = ""
    passed = False
    attempts = 0
    last_error = ""

    with console.status("[bold blue]MeoClaw 正在工作…[/bold blue]") as status:
        for event in events:
            kind = event.get("type")
            node = str(event.get("node") or "")

            if kind == "node_start":
                label = _node_label(emoji_ok, node)
                console.print(Text(f"{label} …", style="bold"))
                status.update(f"[bold blue]{label} 运行中…[/bold blue]")

            elif kind == "ai_message":
                # actor 的中间说明直接展示；verifier 的输出是 JSON 结论，
                # 下面的 ✅/❌ 面板已经整理过了，不再原样刷屏。
                if node == "actor":
                    text = str(event.get("content") or "").strip()
                    if text:
                        console.print(Text(_preview(text, 600), style="dim"))
                status.update("[bold blue]MeoClaw 正在工作…[/bold blue]")

            elif kind == "tool_call":
                name = str(event.get("name") or "?")
                args = event.get("args") or {}
                line = Text("  → ")
                line.append(name, style="bold cyan")
                line.append(f" {json.dumps(args, ensure_ascii=False, default=str)}", style="dim")
                console.print(line)
                status.update("[bold blue]执行工具…[/bold blue]")

            elif kind == "tool_result":
                name = str(event.get("name") or "?")
                ok = bool(event.get("ok", True))
                body = event.get("result")
                text = (
                    body
                    if isinstance(body, str)
                    else json.dumps(body, ensure_ascii=False, default=str)
                )
                console.print(
                    Panel(
                        Text(_preview(text)),
                        title=Text(f"{name} {'结果' if ok else '失败'}"),
                        title_align="left",
                        border_style="green" if ok else "red",
                        box=box_style,
                    )
                )
                status.update("[bold blue]MeoClaw 正在工作…[/bold blue]")

            elif kind == "verification_command":
                ok = bool(event.get("ok"))
                line = Text("  ")
                line.append("[OK] " if ok else "[FAIL] ", style="green" if ok else "red")
                line.append(
                    f"exit_code={event.get('exit_code')} {event.get('command')}", style="dim"
                )
                console.print(line)

            elif kind == "plan":
                body: list[str] = [str(event.get("plan_summary") or "").strip()]
                for todo in event.get("todos") or []:
                    body.append(f"- {todo.get('id')}: {todo.get('content')}")
                criteria = event.get("acceptance_criteria") or []
                if criteria:
                    body.append("验收标准：" + "；".join(str(item) for item in criteria))
                commands = event.get("verification_commands") or []
                if commands:
                    body.append("验收命令：" + "；".join(str(item) for item in commands))
                console.print(
                    Panel(
                        Text("\n".join(part for part in body if part)),
                        title=Text(f"{_node_label(emoji_ok, 'planner')} 计划"),
                        title_align="left",
                        border_style="cyan",
                        box=box_style,
                    )
                )
                status.update("[bold blue]MeoClaw 正在工作…[/bold blue]")

            elif kind == "actor_summary":
                # 内容与上面的 ai_message 重复，这里只更新状态栏
                status.update("[bold blue]MeoClaw 正在工作…[/bold blue]")

            elif kind == "verification":
                passed = bool(event.get("passed"))
                attempts = int(event.get("attempts") or attempts)
                last_error = str(event.get("last_error") or "")
                lines: list[str] = []
                for check in event.get("checks") or []:
                    mark = "[OK]" if check.get("passed") else "[FAIL]"
                    detail = str(check.get("detail") or "").strip()
                    lines.append(
                        f"{mark} {check.get('name')}"
                        + (f"：{_preview(detail, 200)}" if detail else "")
                    )
                console.print(
                    Panel(
                        Text("\n".join(lines) or "（没有给出检查项）"),
                        title=Text(_verifier_title(emoji_ok, passed, attempts, max_attempts)),
                        title_align="left",
                        border_style="green" if passed else "red",
                        box=box_style,
                    )
                )
                status.update("[bold blue]MeoClaw 正在工作…[/bold blue]")

            elif kind == "final_answer":
                final_answer = str(event.get("content") or "")
                status.update("[bold blue]MeoClaw 正在收尾…[/bold blue]")

            elif kind == "workflow_end":
                passed = bool(event.get("passed", passed))
                attempts = int(event.get("attempts") or attempts)
                final_answer = str(event.get("final_answer") or final_answer)
                last_error = str(event.get("last_error") or last_error)

    console.print()
    console.print(
        Panel(
            Text(final_answer.strip() or "(没有生成最终回答)"),
            title=Text(f"{_node_label(emoji_ok, 'final')} 最终回答"),
            title_align="left",
            border_style="blue" if passed else "red",
            box=box_style,
        )
    )

    if passed:
        console.print(Text(f"✅ 通过验证（尝试 {attempts} 轮）" if emoji_ok else f"[OK] 通过验证（尝试 {attempts} 轮）", style="green"))
        return 0

    console.print(
        Text(
            f"❌ 未通过验证（尝试 {attempts}/{max_attempts} 轮）"
            if emoji_ok
            else f"[FAIL] 未通过验证（尝试 {attempts}/{max_attempts} 轮）",
            style="yellow",
        )
    )
    if last_error:
        console.print(Text(_preview(last_error, 800), style="yellow"))
    return 1


def _run_agent(
    task: str,
    workspace: Path,
    model_name: str | None,
    temperature: float,
    max_attempts: int,
) -> int:
    """跑一遍完整工作流，把退出码返回给 CLI。"""
    console = Console()
    emoji_ok, box_style = _console_capabilities(console)

    header = Text(f"MeoClaw v{__version__}", style="bold")
    header.append(" · 工作区 ")
    header.append(str(workspace), style="cyan")
    header.append(f" · 最多 {max_attempts} 轮", style="dim")
    console.print(header)

    try:
        # 生成器是惰性的：建模型 / 跑图都发生在 _stream_events 里，
        # 所以下面的 except 能覆盖到建模型阶段的错误。
        events = stream_agent_events(
            task,
            workspace=workspace,
            max_attempts=max_attempts,
            model_name=model_name,
            temperature=temperature,
            sandbox_mode=os.environ.get("MEOCLAW_SANDBOX", "docker"),
            permission_policy=os.environ.get("MEOCLAW_PERMISSION", "full"),
            auto_route=True,
        )
        return _stream_events(
            console, events, max_attempts, emoji_ok=emoji_ok, box_style=box_style
        )
    except MissingAPIKeyError as exc:
        console.print(Text(str(exc), style="red"))
        return 3
    except KeyboardInterrupt:  # pragma: no cover - 用户 Ctrl+C
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports model/network failures
        # 例如 OPENAI_BASE_URL 非法、网络失败、模型返回异常结构等
        console.print(Text(f"运行失败：{type(exc).__name__}: {exc}", style="red"))
        return 4


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
@app.command()
def run(
    ctx: typer.Context,
    task: str | None = typer.Argument(None, help="交给智能体的任务描述"),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="工作区目录，不存在时自动创建（默认 ./workspace）",
    ),
    model: str | None = typer.Option(None, "--model", "-m", help="模型名，默认读取 OPENAI_MODEL"),
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="采样温度"),
    max_attempts: int = typer.Option(
        DEFAULT_MAX_ATTEMPTS,
        "--max-attempts",
        min=1,
        max=20,
        help="最多尝试轮数：验证不通过就回到 planner 重来，用尽后收尾",
    ),
    list_tools: bool = typer.Option(False, "--list-tools", help="只列出工具与参数后退出，不调用模型"),
    show_state: bool = typer.Option(False, "--show-state", help="额外打印工作区与各类预算"),
) -> None:
    """在 workspace 内执行一个编码任务。"""
    if (task is None or not task.strip()) and not list_tools:
        typer.secho(
            '缺少任务描述，用法：meoclaw "<task>" --workspace <path>',
            fg=typer.colors.RED,
            err=True,
        )
        typer.echo(ctx.get_help())
        raise typer.Exit(code=2)

    # 用 str 接收再转 Path：否则 `--workspace ""` 会被 click 转成 Path(".")，
    # 静默把当前目录当成工作区。
    if workspace is not None and not workspace.strip():
        typer.secho("--workspace 不能为空", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    workspace_path = DEFAULT_WORKSPACE if workspace is None else Path(workspace)

    try:
        state = RuntimeState(workspace=workspace_path)
        tools = build_tools(state)
    except WorkspaceError as exc:
        typer.secho(f"工作区不可用：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if list_tools:
        _print_state(state)
        _print_tools(tools)
        raise typer.Exit(code=0)

    if show_state:
        _print_state(state)

    code = _run_agent(
        task=task or "",
        workspace=state.workspace,
        model_name=model,
        temperature=temperature,
        max_attempts=max_attempts,
    )
    if code != 0:
        raise typer.Exit(code=code)


def main() -> None:
    """控制台入口（project.scripts 指向这里）。"""
    try:
        app(prog_name="meoclaw")
    except KeyboardInterrupt:  # pragma: no cover - 用户 Ctrl+C
        typer.secho("\n已中断。", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(130) from None


if __name__ == "__main__":  # pragma: no cover
    main()
