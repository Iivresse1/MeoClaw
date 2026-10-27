"""Web tools are exposed through every public execution mode."""

from __future__ import annotations

from pathlib import Path

from meoclaw.core.state import RuntimeState
from meoclaw.tools.registry import build_read_only_tools, build_tools, build_web_tools


def test_web_tool_builder_exposes_search_and_fetch() -> None:
    tools = build_web_tools()

    assert [tool.name for tool in tools] == ["WebSearchTool", "WebFetchTool"]


def test_workspace_tools_include_web_search_and_fetch(tmp_path: Path) -> None:
    state = RuntimeState(workspace=tmp_path / "ws")

    names = [tool.name for tool in build_tools(state)]

    assert "WebSearchTool" in names
    assert "WebFetchTool" in names


def test_verifier_tools_include_web_search_and_fetch(tmp_path: Path) -> None:
    state = RuntimeState(workspace=tmp_path / "ws")

    names = [tool.name for tool in build_read_only_tools(state)]

    assert names[-2:] == ["WebSearchTool", "WebFetchTool"]


def test_search_agent_tools_include_web_search_and_fetch() -> None:
    from meoclaw.agents.search_agent import build_search_agent_tools

    names = [tool.name for tool in build_search_agent_tools()]

    assert names == ["WebSearchTool", "WebFetchTool"]


def test_system_tools_include_web_search_and_fetch(tmp_path: Path) -> None:
    from meoclaw_bridge.adapters.system_tools import build_system_tools

    names = [tool.name for tool in build_system_tools(tmp_path / "ws")]

    assert "WebSearchTool" in names
    assert "WebFetchTool" in names
