"""WebSearchTool：Tavily 网络检索，返回结构化来源。

设计要点：
- 与其他工具一样返回**结构化结果**（dict）：`{ok, query, answer, results}`，
  由 `core/react.serialize_tool_result` 编码成 JSON 文本回给模型；
  searchAgent（`agents/search_agent.py`）则直接从事件里的 `result` 字段
  提取 answer 与 sources。
- 缺 `TAVILY_API_KEY`、tavily-python 未安装、网络失败都**不抛异常**，
  而是返回 `ok=False` 的错误结果——检索是尽力而为，失败不该打断 Agent 循环。
- `tavily` 在函数内部懒加载：没装这个包也能导入本模块（测试与离线环境），
  只有真正调用工具时才会触发 ImportError 分支。

安全边界：网络检索不触达工作区，也不需要绑定 `RuntimeState`。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from meoclaw.tools.web_access import resolve_tavily_api_key

__all__ = [
    "MAX_RESULTS_LIMIT",
    "WEB_SEARCH_TOOL_NAME",
    "WebSearchInput",
    "build_web_search_tool",
    "web_search",
]

#: 工具名（事件与断言里都会用到）
WEB_SEARCH_TOOL_NAME = "WebSearchTool"
#: 单次返回结果条数上限
MAX_RESULTS_LIMIT = 10
#: 单条结果内容截断（字符）
SNIPPET_CHARS = 1200

ENV_TAVILY_KEY = ("TAVILY_API_KEY",)


class WebSearchInput(BaseModel):
    """WebSearchTool 入参。"""

    query: str = Field(..., description="检索词，尽量具体，例如 'LangGraph add_messages reducer 语义'")
    max_results: int = Field(
        5,
        ge=1,
        le=MAX_RESULTS_LIMIT,
        description="最多返回的结果条数（1-10，默认 5）",
    )
    include_answer: bool = Field(
        True,
        description="是否让 Tavily 额外生成一段汇总回答（answer 字段）",
    )


def web_search(
    query: str,
    max_results: int = 5,
    include_answer: bool = True,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """用 Tavily 检索，返回 `{ok, query, answer, results}` 结构化结果。

    Args:
        query: 检索词。
        max_results: 最多返回条数（会被夹到 1-10）。
        include_answer: 是否附带 Tavily 的汇总回答。

    Returns:
        `ok=True` 时含 `answer`（可能为空）与 `results`（`[{title, url, content, score}]`）；
        任何失败都是 `ok=False` + `error` 说明，绝不抛异常。
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "query": "", "error": "query 不能为空"}

    resolved_key = resolve_tavily_api_key(api_key)
    if not resolved_key:
        return {
            "ok": False,
            "query": query,
            "error": "missing required setting: TAVILY_API_KEY（在 .env 里配置后才能联网检索）",
        }

    try:
        from tavily import TavilyClient  # 懒加载：未安装时走 ImportError 分支
    except ImportError as exc:
        return {
            "ok": False,
            "query": query,
            "error": f"tavily-python is not installed: {exc}",
        }

    try:
        limit = max(1, min(int(max_results), MAX_RESULTS_LIMIT))
    except (TypeError, ValueError):
        limit = 5

    try:
        client = TavilyClient(api_key=resolved_key)
        response = client.search(
            query=query,
            search_depth="basic",
            max_results=limit,
            include_answer=bool(include_answer),
        )
    except Exception as exc:  # noqa: BLE001 - 网络失败是可预期失败
        return {"ok": False, "query": query, "error": f"{type(exc).__name__}: {exc}"}

    results: list[dict[str, Any]] = []
    for item in response.get("results") or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")[:SNIPPET_CHARS]
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": content,
                "score": item.get("score"),
            }
        )

    return {
        "ok": True,
        "query": query,
        "answer": str(response.get("answer") or ""),
        "results": results,
    }


def build_web_search_tool(api_key: str | None = None) -> StructuredTool:
    """构造 WebSearchTool（无状态，不需要绑定 RuntimeState）。"""

    def web_search_tool(
        query: str, max_results: int = 5, include_answer: bool = True
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "max_results": max_results,
            "include_answer": include_answer,
        }
        if api_key:
            kwargs["api_key"] = api_key
        result = web_search(query, **kwargs)
        if result.get("ok") is False and _is_retryable_search_error(
            str(result.get("error") or "")
        ):
            # ToolRetryMiddleware can only retry exceptions. Missing credentials
            # and invalid input remain structured, non-retryable results.
            raise ToolException(f"temporary web search failure: {result['error']}")
        return result

    return StructuredTool.from_function(
        func=web_search_tool,
        name=WEB_SEARCH_TOOL_NAME,
        description=(
            "用 Tavily 检索网络资料，返回汇总回答（answer）与来源列表（results，含 title/url/content/score）。"
            "适合查找最新事实、官方文档或第三方资料；检索结果应注明来源 URL。"
        ),
        args_schema=WebSearchInput,
        # 检索失败不抛异常而是 ok=False 的结构化结果，无需 handle_tool_error
        handle_validation_error=True,
    )


def _is_retryable_search_error(error: str) -> bool:
    text = error.lower()
    markers = (
        "timeout",
        "timed out",
        "connection",
        "connect",
        "429",
        "500",
        "502",
        "503",
        "504",
        "temporarily unavailable",
    )
    return any(marker in text for marker in markers)
