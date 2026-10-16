"""CLI 层测试：只覆盖不需要 API Key / 不发网络请求的路径。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from meoclaw.cli.app import app
from meoclaw.providers import openai_provider

runner = CliRunner()


def _output(result: object) -> str:
    """兼容 click 8.0/8.1（stderr 单独存放或访问即报错）与 8.2+（默认混流）。"""
    text = getattr(result, "output", "") or ""
    try:
        stderr = result.stderr  # click < 8.2 在混流模式下访问会抛异常
    except Exception:
        stderr = ""
    return text + (stderr or "")


def test_list_tools_works_without_api_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--list-tools", "-w", str(tmp_path / "ws")])

    assert result.exit_code == 0
    output = _output(result)
    assert "FileReadTool" in output
    assert "BashTool" in output
    # 工作区会被自动创建
    assert (tmp_path / "ws").is_dir()


def test_bare_invocation_shows_usage() -> None:
    """裸调用要么走本地校验（退出码 2），要么由 click 打印帮助（退出码 0）。"""
    result = runner.invoke(app, [])

    assert result.exit_code in (0, 2)
    output = _output(result)
    assert any(marker in output for marker in ("缺少任务描述", "Usage", "用法"))


def test_blank_task_is_rejected(tmp_path: Path) -> None:
    result = runner.invoke(app, ["   ", "-w", str(tmp_path / "ws")])
    assert result.exit_code == 2


def test_blank_workspace_is_rejected() -> None:
    result = runner.invoke(app, ["任务", "--workspace", ""])
    assert result.exit_code == 2
    assert "不能为空" in _output(result)


def test_missing_api_key_returns_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_provider, "_candidate_env_files", lambda: [])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    result = runner.invoke(app, ["随便做点什么", "-w", str(tmp_path / "ws")])

    assert result.exit_code == 3
    assert "API Key" in _output(result)
