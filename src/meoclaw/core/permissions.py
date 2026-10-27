"""Tool permission policies shared by every agent execution path."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

PermissionPolicy = Literal["human", "ai", "full"]
ApprovalHandler = Callable[[str, str], bool]
DecisionSink = Callable[[dict[str, Any]], None]

FILE_CHANGE_TOOLS = frozenset(
    {"FileWriteTool", "FileEditTool", "NotepadAppendTool"}
)
ALWAYS_HUMAN_TOOLS = frozenset({"ClipboardRead"})
DIRECT_TOOLS = frozenset(
    {
        "FileReadTool",
        "GrepTool",
        "NotepadReadTool",
        "WebSearchTool",
        "WebFetchTool",
        "TodoWriteTool",
        "TodoUpdateTool",
        "TakeScreenshot",
        "ClipboardWrite",
        "OpenUrl",
        "CallSearchAgentTool",
        "CallCodeAgentTool",
    }
)
SAFE_OPEN_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".csv",
    }
)

REVIEWER_PROMPT = """You are MeoClaw's isolated tool-call reviewer.
Return ONLY a JSON object:
{"decision":"allow|deny|ask","reason":"short explanation"}

Allow ordinary code edits, builds, tests, lint, formatting, and focused shell
commands. Deny commands that access credentials, escape the workspace, control
Docker, or disable safeguards. Ask when the effect is destructive, unusually
broad, long-running, hard to reverse, or uncertain.

The runtime separately enforces path boundaries, Docker isolation, credential
protection, and verification. You only decide whether this proposed call may
run without a human."""


def normalize_policy(value: Any) -> PermissionPolicy:
    text = str(value or "").strip().lower()
    return text if text in {"human", "ai", "full"} else "human"


def _json_error(tool: str, tool_call_id: str, reason: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(
            {"ok": False, "tool": tool, "error": reason},
            ensure_ascii=False,
        ),
        name=tool or "permission",
        tool_call_id=tool_call_id,
        status="error",
    )


def _request_parts(request: Any) -> tuple[str, dict[str, Any], str]:
    call = getattr(request, "tool_call", None)
    if not isinstance(call, Mapping):
        return "", {}, ""
    name = str(call.get("name") or "")
    args = call.get("args")
    return (
        name,
        dict(args) if isinstance(args, Mapping) else {},
        str(call.get("id") or ""),
    )


def _is_safe_local_file(args: Mapping[str, Any]) -> bool:
    suffix = Path(str(args.get("file_path") or "")).suffix.lower()
    return suffix in SAFE_OPEN_SUFFIXES


def _approval_detail(name: str, args: Mapping[str, Any]) -> str:
    if name == "BashTool":
        command = str(args.get("command") or "").strip()
        return f"执行 shell：{command[:500]}"
    if name in FILE_CHANGE_TOOLS:
        path = str(args.get("file_path") or "").strip()
        content = str(args.get("content") or args.get("new_text") or "")
        preview = content[:500]
        if len(content) > len(preview):
            preview += "…"
        return (
            f"修改文件 {path or '(未提供路径)'}，内容 {len(content)} 字符"
            + (f"\n预览：{preview}" if preview else "")
        )
    if name == "ClipboardRead":
        return "读取系统剪贴板内容"
    if name == "OpenWorkspaceFile":
        return f"打开工作区文件：{args.get('file_path') or '(未提供路径)'}"
    return f"执行工具 {name}"


def deterministic_precheck(name: str, args: Mapping[str, Any]) -> str:
    """Return deny/ask/allow before consulting the AI reviewer."""
    if name == "BashTool":
        command = str(args.get("command") or "").strip().lower()
        dangerous = (
            "docker.sock",
            "/var/run/docker",
            ".ssh",
            "id_rsa",
            "id_ed25519",
            ".aws",
            ".azure",
            ".gcloud",
            ".netrc",
            "credentials.json",
            "secrets.json",
        )
        if any(marker in command for marker in dangerous):
            return "deny"
        if "rm " in command and ("/" in command or "~" in command):
            return "ask"
    return "allow"


class AISecurityReviewer:
    """Ask an isolated model invocation to classify one sensitive call."""

    def __init__(self, model: BaseChatModel) -> None:
        self.model = model

    def review(
        self,
        name: str,
        args: Mapping[str, Any],
    ) -> tuple[str, str]:
        payload = json.dumps(
            {"tool": name, "args": args},
            ensure_ascii=False,
            default=str,
        )
        if len(payload) > 16_000:
            payload = payload[:15_900] + "..."
        try:
            response = self.model.invoke(
                [
                    SystemMessage(content=REVIEWER_PROMPT),
                    HumanMessage(content=payload),
                ]
            )
            content = getattr(response, "content", "")
            text = content if isinstance(content, str) else json.dumps(content)
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                return "ask", "AI Reviewer 未返回可解析结论"
            result = json.loads(text[start : end + 1])
            decision = str(result.get("decision") or "").strip().lower()
            reason = str(result.get("reason") or "").strip()
            if decision not in {"allow", "deny", "ask"}:
                return "ask", reason or "AI Reviewer 结论不明确"
            return decision, reason or "AI Reviewer 未提供理由"
        except Exception as exc:  # noqa: BLE001 - invalid review must fail closed
            return "ask", f"AI Reviewer 调用失败：{type(exc).__name__}"


class ToolPermissionMiddleware(AgentMiddleware):
    """Apply one Permission Policy before each tool call."""

    def __init__(
        self,
        *,
        policy: str,
        reviewer: AISecurityReviewer | None = None,
        approval_handler: ApprovalHandler | None = None,
        decision_sink: DecisionSink | None = None,
    ) -> None:
        super().__init__()
        self.policy = normalize_policy(policy)
        self.reviewer = reviewer
        self.approval_handler = approval_handler
        self.decision_sink = decision_sink
        self._denied_tools: set[str] = set()

    def _emit(self, name: str, decision: str, reason: str) -> None:
        if self.decision_sink is None:
            return
        self.decision_sink(
            {
                "type": "policy_decision",
                "tool": name,
                "decision": decision,
                "reason": reason,
            }
        )

    def _ask_human(
        self,
        name: str,
        args: Mapping[str, Any],
        reason: str,
        *,
        escalated: bool = False,
    ) -> bool:
        if self.approval_handler is None:
            self._emit(name, "deny", "没有可用的人工审批通道")
            self._denied_tools.add(name)
            return False
        if escalated:
            self._emit(name, "ask", reason)
        approved = self.approval_handler(name, _approval_detail(name, args))
        self._emit(name, "allow" if approved else "deny", reason)
        if not approved:
            self._denied_tools.add(name)
        return approved

    def wrap_tool_call(self, request: Any, handler: Any) -> ToolMessage:
        name, args, tool_call_id = _request_parts(request)
        if self.policy == "full" or name in DIRECT_TOOLS:
            return handler(request)
        if name in self._denied_tools:
            return _json_error(name, tool_call_id, "该工具已在本任务中被拒绝")

        if name == "OpenWorkspaceFile":
            if _is_safe_local_file(args):
                return handler(request)
            if self._ask_human(name, args, "可执行文件或未知文件类型"):
                return handler(request)
            return _json_error(name, tool_call_id, "用户拒绝打开该文件")

        if name in ALWAYS_HUMAN_TOOLS:
            if self._ask_human(name, args, "可能读取敏感本地数据"):
                return handler(request)
            return _json_error(name, tool_call_id, "用户拒绝读取剪贴板")

        sensitive = name == "BashTool" or name in FILE_CHANGE_TOOLS
        if not sensitive:
            if self.policy == "human":
                if self._ask_human(name, args, "未知工具"):
                    return handler(request)
                return _json_error(name, tool_call_id, "用户拒绝执行未知工具")
            return handler(request)

        if self.policy == "human":
            if self._ask_human(name, args, "敏感工具调用"):
                return handler(request)
            return _json_error(name, tool_call_id, "用户拒绝了该操作")

        precheck = deterministic_precheck(name, args)
        if precheck == "deny":
            self._emit(name, "deny", "命中确定性安全规则")
            self._denied_tools.add(name)
            return _json_error(name, tool_call_id, "安全规则拒绝了该操作")
        if precheck == "ask":
            if self._ask_human(
                name,
                args,
                "命中确定性升级规则",
                escalated=True,
            ):
                return handler(request)
            return _json_error(name, tool_call_id, "用户拒绝了该操作")

        if self.reviewer is None:
            if self._ask_human(
                name,
                args,
                "AI Reviewer 不可用",
                escalated=True,
            ):
                return handler(request)
            return _json_error(name, tool_call_id, "AI Reviewer 不可用")

        decision, reason = self.reviewer.review(name, args)
        if decision == "allow":
            self._emit(name, "allow", reason)
            return handler(request)
        if decision == "deny":
            self._emit(name, "deny", reason)
            self._denied_tools.add(name)
            return _json_error(name, tool_call_id, reason)
        if self._ask_human(name, args, reason, escalated=True):
            return handler(request)
        return _json_error(name, tool_call_id, "用户拒绝了该操作")
