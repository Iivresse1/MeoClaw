"""Permission-policy middleware tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from meoclaw.core.permissions import (
    AISecurityReviewer,
    ToolPermissionMiddleware,
)


def _request(name: str, args: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "args": args, "id": f"call-{name}"}
    )


def _handler(request: Any) -> ToolMessage:
    call = request.tool_call
    return ToolMessage(
        content=json.dumps({"ok": True}),
        name=call["name"],
        tool_call_id=call["id"],
    )


class ReviewerModel:
    def __init__(self, payload: dict[str, str] | Exception) -> None:
        self.payload = payload
        self.calls = 0

    def invoke(self, messages: list[Any], **_: Any) -> AIMessage:
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return AIMessage(content=json.dumps(self.payload))


def test_human_file_write_requires_approval() -> None:
    calls: list[tuple[str, str]] = []
    middleware = ToolPermissionMiddleware(
        policy="human",
        approval_handler=lambda tool, detail: calls.append((tool, detail)) or True,
    )

    result = middleware.wrap_tool_call(
        _request("FileWriteTool", {"file_path": "a.py", "content": "print(1)"}),
        _handler,
    )

    assert result.status != "error"
    assert calls[0][0] == "FileWriteTool"
    assert "a.py" in calls[0][1]


def test_human_denial_returns_tool_error_without_execution() -> None:
    executed = False
    approvals = 0

    def handler(_: Any) -> ToolMessage:
        nonlocal executed
        executed = True
        raise AssertionError("must not execute")

    def deny(*_: Any) -> bool:
        nonlocal approvals
        approvals += 1
        return False

    middleware = ToolPermissionMiddleware(policy="human", approval_handler=deny)

    result = middleware.wrap_tool_call(
        _request("BashTool", {"command": "python -m pytest"}),
        handler,
    )

    assert executed is False
    assert result.status == "error"
    assert "拒绝" in str(result.content)
    second = middleware.wrap_tool_call(
        _request("BashTool", {"command": "python -m pytest"}),
        handler,
    )
    assert second.status == "error"
    assert approvals == 1


def test_low_risk_system_tools_execute_directly() -> None:
    approvals: list[str] = []
    middleware = ToolPermissionMiddleware(
        policy="human",
        approval_handler=lambda tool, _: approvals.append(tool) or False,
    )

    for name, args in (
        ("TakeScreenshot", {}),
        ("ClipboardWrite", {"text": "hello"}),
        ("OpenUrl", {"url": "https://example.com"}),
    ):
        result = middleware.wrap_tool_call(_request(name, args), _handler)
        assert result.status != "error"
    assert approvals == []


def test_clipboard_read_always_requires_human_in_human_policy() -> None:
    approvals: list[str] = []
    middleware = ToolPermissionMiddleware(
        policy="human",
        approval_handler=lambda tool, _: approvals.append(tool) or True,
    )

    result = middleware.wrap_tool_call(_request("ClipboardRead", {}), _handler)

    assert result.status != "error"
    assert approvals == ["ClipboardRead"]


def test_open_workspace_file_uses_extension_whitelist() -> None:
    approvals: list[str] = []
    middleware = ToolPermissionMiddleware(
        policy="human",
        approval_handler=lambda tool, _: approvals.append(tool) or True,
    )

    safe = middleware.wrap_tool_call(
        _request("OpenWorkspaceFile", {"file_path": "report.pdf"}),
        _handler,
    )
    unsafe = middleware.wrap_tool_call(
        _request("OpenWorkspaceFile", {"file_path": "payload.exe"}),
        _handler,
    )

    assert safe.status != "error"
    assert unsafe.status != "error"
    assert approvals == ["OpenWorkspaceFile"]


def test_ai_reviewer_allows_ordinary_command() -> None:
    decisions: list[dict[str, Any]] = []
    reviewer_model = ReviewerModel(
        {"decision": "allow", "reason": "普通测试命令"}
    )
    middleware = ToolPermissionMiddleware(
        policy="ai",
        reviewer=AISecurityReviewer(reviewer_model),  # type: ignore[arg-type]
        decision_sink=decisions.append,
    )

    result = middleware.wrap_tool_call(
        _request("BashTool", {"command": "python -m pytest -q"}),
        _handler,
    )

    assert result.status != "error"
    assert decisions[-1]["decision"] == "allow"
    assert decisions[-1]["tool"] == "BashTool"


def test_ai_reviewer_escalates_invalid_result_to_human() -> None:
    approvals: list[str] = []
    decisions: list[dict[str, Any]] = []
    reviewer_model = ReviewerModel(RuntimeError("offline"))
    middleware = ToolPermissionMiddleware(
        policy="ai",
        reviewer=AISecurityReviewer(reviewer_model),  # type: ignore[arg-type]
        approval_handler=lambda tool, _: approvals.append(tool) or True,
        decision_sink=decisions.append,
    )

    result = middleware.wrap_tool_call(
        _request("FileEditTool", {"file_path": "a.py", "new_text": "x"}),
        _handler,
    )

    assert result.status != "error"
    assert approvals == ["FileEditTool"]
    assert [item["decision"] for item in decisions] == ["ask", "allow"]


def test_deterministic_dangerous_shell_is_denied_before_ai_review() -> None:
    reviewer_model = ReviewerModel(
        {"decision": "allow", "reason": "should not be called"}
    )
    middleware = ToolPermissionMiddleware(
        policy="ai",
        reviewer=AISecurityReviewer(reviewer_model),  # type: ignore[arg-type]
    )

    result = middleware.wrap_tool_call(
        _request("BashTool", {"command": "cat /var/run/docker.sock"}),
        _handler,
    )

    assert result.status == "error"
    assert reviewer_model.calls == 0
