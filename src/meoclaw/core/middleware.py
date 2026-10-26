"""LangChain middleware used by MeoClaw agent loops.

This module deliberately contains cross-cutting agent policy only. Domain tools
and verification rules remain in ``meoclaw.tools`` and ``meoclaw.graph``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage

from meoclaw.tools.web_search_tool import WEB_SEARCH_TOOL_NAME

__all__ = [
    "JsonToolResultMiddleware",
    "build_agent_middleware",
    "tool_error_to_message",
]


class JsonToolResultMiddleware(AgentMiddleware):
    """Serialize successful tool results before they are sent back to the model."""

    def wrap_tool_call(self, request: Any, handler: Any) -> ToolMessage:
        result = handler(request)
        if (
            isinstance(result, ToolMessage)
            and str(getattr(result, "status", "") or "") != "error"
        ):
            content = result.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            else:
                try:
                    json.loads(content)
                except json.JSONDecodeError:
                    content = json.dumps(content, ensure_ascii=False)
            result = result.model_copy(update={"content": content})
        return result


def tool_error_to_message(exc: Exception, request: Any) -> str:
    """Return selected tool failures to the model without leaking full internals."""
    name = ""
    if isinstance(getattr(request, "tool_call", None), Mapping):
        name = str(request.tool_call.get("name") or "")
    return json.dumps(
        {
            "ok": False,
            "tool": name,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500] or "工具执行失败，请检查参数或改用其他方法",
        },
        ensure_ascii=False,
    )


def build_agent_middleware(
    model: BaseChatModel,
    *,
    max_model_calls: int,
    max_tool_calls: int = 32,
    summary_token_limit: int | None = None,
    approval_tools: Sequence[str] | None = None,
) -> list[AgentMiddleware]:
    """Build the common middleware stack for one create_agent graph.

    Tool retries are intentionally scoped to web search.  Retrying file writes or
    shell commands can repeat non-idempotent side effects.
    """
    middleware: list[AgentMiddleware] = [
        JsonToolResultMiddleware(),
        ToolErrorMiddleware(on_error=tool_error_to_message),
        ToolRetryMiddleware(
            max_retries=2,
            tools=[WEB_SEARCH_TOOL_NAME],
            on_failure="error",
            backoff_factor=2.0,
            initial_delay=0.25,
            max_delay=2.0,
            jitter=True,
        ),
        ModelCallLimitMiddleware(
            run_limit=max(1, int(max_model_calls)),
            exit_behavior="end",
        ),
        ToolCallLimitMiddleware(
            run_limit=max(1, int(max_tool_calls)),
            exit_behavior="end",
        ),
    ]
    if summary_token_limit is not None and int(summary_token_limit) > 0:
        middleware.append(
            SummarizationMiddleware(
                model=model,
                trigger=("tokens", int(summary_token_limit)),
                keep=("messages", 16),
            )
        )
    if approval_tools:
        middleware.append(
            HumanInTheLoopMiddleware(
                interrupt_on={name: True for name in approval_tools},
                interrupt_mode="per_call",
                description_prefix="请确认是否允许执行工具",
            )
        )
    return middleware
