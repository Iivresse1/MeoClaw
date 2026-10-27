"""WebFetchTool: retrieve readable page content through Tavily Extract."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from meoclaw.tools.web_access import resolve_tavily_api_key

__all__ = [
    "WEB_FETCH_TOOL_NAME",
    "WebFetchInput",
    "build_web_fetch_tool",
    "web_fetch",
]

WEB_FETCH_TOOL_NAME = "WebFetchTool"
DEFAULT_MAX_CHARS = 20_000
MAX_CONTENT_CHARS = 60_000


class WebFetchInput(BaseModel):
    """Input for a single web page fetch."""

    url: str = Field(description="要读取的 http(s) 页面地址")
    query: str = Field(
        default="",
        description="可选：希望页面中优先保留的相关内容",
    )
    extract_depth: Literal["basic", "advanced"] = Field(
        default="basic",
        description="Tavily 提取深度；advanced 更完整但更慢",
    )
    max_chars: int = Field(
        default=DEFAULT_MAX_CHARS,
        ge=1,
        le=MAX_CONTENT_CHARS,
        description="返回给模型的最大字符数",
    )


def web_fetch(
    url: str,
    *,
    query: str = "",
    extract_depth: Literal["basic", "advanced"] = "basic",
    max_chars: int = DEFAULT_MAX_CHARS,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch one public web page and return bounded readable content."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "ok": False,
            "url": url,
            "error": "url 必须是完整的 http(s) 地址",
        }

    key = resolve_tavily_api_key(api_key)
    if not key:
        return {
            "ok": False,
            "url": url,
            "error": "missing required setting: TAVILY_API_KEY（请在设置页配置联网检索）",
        }

    try:
        from tavily import TavilyClient
    except ImportError as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"tavily-python is not installed: {exc}",
        }

    try:
        client = TavilyClient(api_key=key)
        response = client.extract(
            urls=[url],
            extract_depth=extract_depth,
            format="markdown",
            query=query.strip() or None,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - external fetch failure is expected
        return {
            "ok": False,
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }

    results = response.get("results") or []
    first = results[0] if results and isinstance(results[0], dict) else {}
    content = str(first.get("raw_content") or first.get("content") or "")
    if not content:
        failed = response.get("failed_results") or []
        detail = failed[0] if failed and isinstance(failed[0], dict) else {}
        return {
            "ok": False,
            "url": url,
            "error": str(detail.get("error") or "网页没有可读取的正文"),
        }

    limit = max(1, min(int(max_chars), MAX_CONTENT_CHARS))
    return {
        "ok": True,
        "url": str(first.get("url") or url),
        "content": content[:limit],
        "truncated": len(content) > limit,
    }


def build_web_fetch_tool(api_key: str | None = None) -> StructuredTool:
    """Build a WebFetchTool bound to an optional pre-resolved API key."""

    def fetch_tool(
        url: str,
        query: str = "",
        extract_depth: Literal["basic", "advanced"] = "basic",
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> dict[str, Any]:
        return web_fetch(
            url,
            query=query,
            extract_depth=extract_depth,
            max_chars=max_chars,
            api_key=api_key,
        )

    return StructuredTool.from_function(
        func=fetch_tool,
        name=WEB_FETCH_TOOL_NAME,
        description=(
            "读取指定 http(s) 网页的正文，返回 Markdown 或纯文本内容。"
            "适合在 WebSearchTool 找到来源后打开原文核对细节。"
        ),
        args_schema=WebFetchInput,
        handle_validation_error=True,
    )
