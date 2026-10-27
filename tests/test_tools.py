"""工具层测试：读写/替换/搜索/执行，以及 workspace 边界。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import StructuredTool, ToolException

import meoclaw.core.sandbox as sandbox_module
import meoclaw.tools.bash_tool as bash_module
from meoclaw.core.paths import WorkspaceError, resolve_in_workspace
from meoclaw.core.state import RuntimeState
from meoclaw.tools.bash_tool import execute_command, run_bash
from meoclaw.tools.file_tools import edit_file, read_file, write_file
from meoclaw.tools.grep_tool import grep
from meoclaw.tools.registry import TOOL_NAMES, build_tool_map, build_tools


@pytest.fixture()
def state(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path / "ws")


# --------------------------------------------------------------------------- #
# FileReadTool / FileWriteTool / FileEditTool
# --------------------------------------------------------------------------- #
def test_write_creates_file_and_parents(state: RuntimeState) -> None:
    message = write_file(state, "pkg/mod.py", "one\ntwo\nthree\n")
    assert "创建" in message
    assert (state.workspace / "pkg" / "mod.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_write_overwrites_existing(state: RuntimeState) -> None:
    write_file(state, "a.txt", "old")
    message = write_file(state, "a.txt", "new")
    assert "覆写" in message
    assert (state.workspace / "a.txt").read_text(encoding="utf-8") == "new"


def test_write_refuses_to_clobber_directory(state: RuntimeState) -> None:
    (state.workspace / "adir").mkdir()
    with pytest.raises(ToolException):
        write_file(state, "adir", "x")


def test_read_returns_numbered_lines(state: RuntimeState) -> None:
    write_file(state, "a.txt", "one\ntwo\nthree\n")
    content = read_file(state, "a.txt")
    assert "共 3 行" in content
    assert "1\tone" in content
    assert "3\tthree" in content


def test_read_honours_offset_and_limit(state: RuntimeState) -> None:
    write_file(state, "a.txt", "".join(f"line{i}\n" for i in range(1, 11)))
    content = read_file(state, "a.txt", offset=2, limit=3)
    assert "第 3-5 行" in content
    assert "还有 5 行" in content
    assert "line3" in content
    assert "line6" not in content


def test_read_rejects_escape(state: RuntimeState) -> None:
    (state.workspace.parent / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(ToolException) as excinfo:
        read_file(state, "../outside.txt")
    assert "越界" in str(excinfo.value)
    assert not (state.workspace / "outside.txt").exists()


def test_read_missing_file(state: RuntimeState) -> None:
    with pytest.raises(ToolException):
        read_file(state, "nope.txt")


def test_read_directory_is_rejected(state: RuntimeState) -> None:
    (state.workspace / "adir").mkdir()
    with pytest.raises(ToolException):
        read_file(state, "adir")


def test_read_rejects_oversized_file(state: RuntimeState) -> None:
    write_file(state, "big.txt", "0123456789")
    state.max_read_bytes = 5
    with pytest.raises(ToolException):
        read_file(state, "big.txt")


def test_read_rejects_binary_file(state: RuntimeState) -> None:
    (state.workspace / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ToolException):
        read_file(state, "bin.dat")


def test_edit_replaces_unique_fragment(state: RuntimeState) -> None:
    write_file(state, "a.txt", "alpha\nbeta\ngamma\n")
    message = edit_file(state, "a.txt", "beta", "BETA")
    assert "第 2 行" in message
    assert (state.workspace / "a.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_edit_rejects_multiple_matches(state: RuntimeState) -> None:
    write_file(state, "a.txt", "dup\ndup\n")
    with pytest.raises(ToolException) as excinfo:
        edit_file(state, "a.txt", "dup", "x")
    assert "2 处" in str(excinfo.value)


def test_edit_rejects_missing_fragment(state: RuntimeState) -> None:
    write_file(state, "a.txt", "hello\n")
    with pytest.raises(ToolException):
        edit_file(state, "a.txt", "nope", "x")


def test_edit_rejects_empty_old_text(state: RuntimeState) -> None:
    write_file(state, "a.txt", "hello\n")
    with pytest.raises(ToolException):
        edit_file(state, "a.txt", "", "x")


def test_edit_keeps_crlf_line_endings(state: RuntimeState) -> None:
    target = state.workspace / "crlf.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    edit_file(state, "crlf.txt", "two", "TWO")
    assert target.read_bytes() == b"one\r\nTWO\r\n"


def test_edit_multiline_crlf_line_endings(state: RuntimeState) -> None:
    target = state.workspace / "crlf2.txt"
    target.write_bytes(b"a\r\nb\r\nc\r\n")
    edit_file(state, "crlf2.txt", "a\nb", "A\nB")
    assert target.read_bytes() == b"A\r\nB\r\nc\r\n"


def test_edit_rejects_escape(state: RuntimeState) -> None:
    with pytest.raises(ToolException):
        edit_file(state, "../outside.txt", "a", "b")


def test_edit_rejects_non_utf8_file(state: RuntimeState) -> None:
    """非 UTF-8 文件必须拒绝编辑，而不是用 U+FFFD 悄悄覆盖原内容。"""
    target = state.workspace / "gbk.txt"
    original = "中文\nhello\n".encode("gbk")
    target.write_bytes(original)
    with pytest.raises(ToolException):
        edit_file(state, "gbk.txt", "hello", "hi")
    assert target.read_bytes() == original


def test_edit_rejects_ambiguous_mixed_line_endings(state: RuntimeState) -> None:
    """同一片段同时以 LF 和 CRLF 存在（位置不同）时，唯一性无法确定，必须报错。"""
    target = state.workspace / "mixed.txt"
    target.write_bytes(b"a\nb\r\nx\na\r\nb")
    with pytest.raises(ToolException) as excinfo:
        edit_file(state, "mixed.txt", "a\nb", "X")
    assert "LF" in str(excinfo.value)
    assert target.read_bytes() == b"a\nb\r\nx\na\r\nb"


def test_edit_crlf_fragment_with_leading_newline(state: RuntimeState) -> None:
    """old_text 以换行开头时，LF/CRLF 候选指向同一处，不应误报"匹配到多处"。"""
    target = state.workspace / "leading.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    edit_file(state, "leading.txt", "\ntwo", "TWO")
    # 命中的是文件中真实存在的 "\r\ntwo"，其余字节原样保留
    assert target.read_bytes() == b"oneTWO\r\n"


# --------------------------------------------------------------------------- #
# GrepTool
# --------------------------------------------------------------------------- #
def test_grep_finds_matches_with_glob(state: RuntimeState) -> None:
    write_file(state, "src/a.py", "import os\n\ndef main():\n    return 1\n")
    write_file(state, "src/b.py", "def main():\n    return 2\n")
    write_file(state, "src/notes.md", "def main\n")
    result = grep(state, r"def main", glob="*.py")
    assert "匹配 2 处" in result
    assert "src/a.py:3:" in result
    assert "src/b.py:1:" in result
    assert "notes.md" not in result


def test_grep_ignore_case_and_head_limit(state: RuntimeState) -> None:
    write_file(state, "a.txt", "Foo\nfoo\nFOO\n")
    assert "匹配 3 处" in grep(state, "foo", ignore_case=True)
    limited = grep(state, "foo", ignore_case=True, head_limit=1)
    assert "匹配 1 处" in limited
    assert "head_limit=1" in limited


def test_grep_reports_no_match(state: RuntimeState) -> None:
    write_file(state, "a.txt", "hello\n")
    assert "没有匹配" in grep(state, "zzz")


def test_grep_rejects_invalid_regex(state: RuntimeState) -> None:
    with pytest.raises(ToolException):
        grep(state, "(")


def test_grep_rejects_escape(state: RuntimeState) -> None:
    with pytest.raises(ToolException):
        grep(state, "x", path="../")


def test_grep_skips_binary_and_oversized_files(state: RuntimeState) -> None:
    write_file(state, "a.txt", "needle\n")
    (state.workspace / "bin.dat").write_bytes(b"\x00needle")
    write_file(state, "big.txt", "needle" + "x" * 500)
    state.max_read_bytes = 100
    result = grep(state, "needle")
    assert "匹配 1 处" in result
    assert "a.txt:1:" in result


def test_grep_single_file_path(state: RuntimeState) -> None:
    write_file(state, "a.txt", "needle\n")
    write_file(state, "b.txt", "needle\n")
    result = grep(state, "needle", path="a.txt")
    assert "a.txt:1:" in result
    assert "b.txt" not in result


def test_grep_rejects_glob_with_parent_escape(state: RuntimeState) -> None:
    (state.workspace.parent / "outside.py").write_text("needle\n", encoding="utf-8")
    write_file(state, "inside.py", "needle\n")
    with pytest.raises(ToolException):
        grep(state, "needle", glob="../*.py")


def test_grep_skips_symlink_pointing_outside(state: RuntimeState) -> None:
    outside = state.workspace.parent / "secret.txt"
    outside.write_text("needle\n", encoding="utf-8")
    link = state.workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    result = grep(state, "needle")
    assert "没有匹配" in result
    assert "secret" not in result


# --------------------------------------------------------------------------- #
# BashTool
# --------------------------------------------------------------------------- #
def test_bash_runs_inside_workspace(state: RuntimeState) -> None:
    """用"命令产出的文件落在哪"证明 cwd 是 workspace，避免路径大小写比较带来的假失败。"""
    command = f'"{sys.executable}" -c "open(\'cwd_marker.txt\',\'w\').write(\'ok\')"'
    result = run_bash(state, command)
    assert "退出码: 0" in result
    assert (state.workspace / "cwd_marker.txt").read_text(encoding="utf-8") == "ok"


def test_bash_reports_nonzero_exit_code(state: RuntimeState) -> None:
    command = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
    assert "退出码: 3" in run_bash(state, command)


def test_bash_captures_stderr(state: RuntimeState) -> None:
    command = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'boom\')"'
    result = run_bash(state, command)
    assert "--- stderr ---" in result
    assert "boom" in result


def test_bash_timeout_kills_process(state: RuntimeState) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    result = run_bash(state, command, timeout_seconds=1)
    assert "强制终止" in result


def test_bash_rejects_empty_command(state: RuntimeState) -> None:
    with pytest.raises(ToolException):
        run_bash(state, "   ")


def test_bash_docker_mode_runs_through_sandbox_command(
    state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state.metadata["sandbox"] = "docker"
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda _: "docker")
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
            captured["timeout"] = timeout
            return b"inside-container\n", b""

        def poll(self) -> int:
            return self.returncode

    def fake_popen(command: Any, **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(bash_module.subprocess, "Popen", fake_popen)

    outcome = execute_command(state, "python -m pytest -q")

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:3] == ["docker", "run", "-i"]
    assert "--rm" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("-w") + 1] == "/workspace"
    assert "--read-only" in command
    assert command[-3:] == ["/bin/bash", "-lc", "python -m pytest -q"]
    assert captured["kwargs"]["shell"] is False
    assert "cwd" not in captured["kwargs"]
    assert outcome["ok"] is True
    assert outcome["stdout"] == "inside-container"


def test_bash_docker_mode_fails_closed_without_docker(
    state: RuntimeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state.metadata["sandbox"] = "docker"
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda _: None)

    with pytest.raises(ToolException, match="Docker 未安装"):
        execute_command(state, "python -m pytest -q")


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_build_tools_order_and_types(state: RuntimeState) -> None:
    tools = build_tools(state)
    assert [tool.name for tool in tools] == list(TOOL_NAMES)
    assert all(isinstance(tool, StructuredTool) for tool in tools)
    assert set(build_tool_map(state)) == set(TOOL_NAMES)


def test_tool_schemas_are_usable(state: RuntimeState) -> None:
    for tool in build_tools(state):
        schema = tool.args_schema.model_json_schema()
        assert schema["type"] == "object"
        assert schema["properties"]


def test_tools_end_to_end_through_langchain(state: RuntimeState) -> None:
    tools = build_tool_map(state)
    assert "已创建" in tools["FileWriteTool"].invoke({"file_path": "x.txt", "content": "hi\n"})
    assert "hi" in tools["FileReadTool"].invoke({"file_path": "x.txt"})
    assert "已更新" in tools["FileEditTool"].invoke(
        {"file_path": "x.txt", "old_text": "hi", "new_text": "HI"}
    )
    assert "匹配 1 处" in tools["GrepTool"].invoke({"pattern": "HI"})
    assert "退出码: 0" in tools["BashTool"].invoke(
        {"command": f'"{sys.executable}" -c "print(1)"'}
    )


def test_tool_failure_raises_tool_exception(state: RuntimeState) -> None:
    """工具层用 ToolException 表达可预期失败。

    由调用方负责转成模型可见的文本：`core.agent.execute_tool` 和
    LangGraph 的 ToolNode 都会捕获 ToolException 并返回其 message。
    这样"失败"才不会被误当成"成功返回"。
    """
    tools = build_tool_map(state)
    tools["FileWriteTool"].invoke({"file_path": "a.txt", "content": "dup\ndup\n"})
    with pytest.raises(ToolException):
        tools["FileEditTool"].invoke(
            {"file_path": "a.txt", "old_text": "dup", "new_text": "x"}
        )


def test_workspace_escape_raises_at_core_layer(state: RuntimeState) -> None:
    """工具层把越界翻译成 ToolException，core 层仍然抛 WorkspaceError。"""
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(state.workspace, "../escape.txt")


# --------------------------------------------------------------------------- #
# WebSearchTool
# --------------------------------------------------------------------------- #
def test_web_search_rejects_blank_query() -> None:
    from meoclaw.tools.web_search_tool import web_search

    result = web_search("   ")
    assert result["ok"] is False
    assert result["error"] == "query 不能为空"


def test_web_search_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 TAVILY_API_KEY 时返回结构化错误，不抛异常。"""
    from meoclaw.tools import web_access

    monkeypatch.setattr(web_access, "load_env", lambda: None)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    from meoclaw.tools.web_search_tool import web_search

    result = web_search("LangGraph")

    assert result["ok"] is False
    assert "TAVILY_API_KEY" in result["error"]
    assert result["query"] == "LangGraph"


def test_web_search_parses_tavily_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 Key 且 Tavily 可用时，结果被规范成 {title, url, content, score}。"""
    import types

    from meoclaw.tools import web_search_tool

    class FakeTavilyClient:
        def __init__(self, api_key: str) -> None:
            assert api_key == "test-key"

        def search(self, query: str, search_depth: str, max_results: int, include_answer: bool):
            assert query == "LangGraph"
            assert search_depth == "basic"
            assert max_results == 5
            return {
                "answer": "LangGraph 是一个状态机框架。",
                "results": [
                    {"title": "官方文档", "url": "https://langchain-ai.github.io/langgraph/", "content": "x" * 2000, "score": 0.95},
                ],
            }

    from meoclaw.tools import web_access

    monkeypatch.setattr(web_access, "load_env", lambda: None)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setitem(
        sys.modules, "tavily", types.SimpleNamespace(TavilyClient=FakeTavilyClient)
    )

    result = web_search_tool.web_search("LangGraph")

    assert result["ok"] is True
    assert result["answer"] == "LangGraph 是一个状态机框架。"
    assert result["results"] == [
        {
            "title": "官方文档",
            "url": "https://langchain-ai.github.io/langgraph/",
            "content": "x" * 1200,  # snippet 被截断到 1200 字符
            "score": 0.95,
        }
    ]


def test_web_search_tool_schema_and_name() -> None:
    from meoclaw.tools.web_search_tool import (
        WEB_SEARCH_TOOL_NAME,
        build_web_search_tool,
    )

    tool = build_web_search_tool()
    assert tool.name == WEB_SEARCH_TOOL_NAME
    schema = tool.args_schema.model_json_schema()
    assert set(schema["properties"]) == {"query", "max_results", "include_answer"}


def test_build_read_only_tools_contains_web_search(state: RuntimeState) -> None:
    from meoclaw.tools.registry import READ_ONLY_TOOL_NAMES, build_read_only_tools

    tools = build_read_only_tools(state)
    assert [tool.name for tool in tools] == list(READ_ONLY_TOOL_NAMES)
    assert [tool.name for tool in tools[-2:]] == ["WebSearchTool", "WebFetchTool"]
