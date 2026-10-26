"""LangChain-backed ReAct event stream used by MeoClaw graph nodes.

The model-tool loop is delegated to ``langchain.agents.create_agent``.  This
module only adapts its native stream into MeoClaw's event contract and validates
input/output edge cases.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command
from pydantic import Field

from meoclaw.core.context_builder import tag_message_context
from meoclaw.core.middleware import build_agent_middleware
from meoclaw.core.state import RuntimeState
from meoclaw.prompts.stage2 import ACTOR_PROMPT
from meoclaw.providers.openai_provider import create_model
from meoclaw.tools.registry import build_tools

__all__ = [
    "ACTOR_PROMPT",
    "REACT_EVENT_TYPES",
    "message_text",
    "serialize_tool_result",
    "stream_react_events",
]

REACT_EVENT_TYPES: tuple[str, ...] = (
    "ai_token",
    "ai_message",
    "tool_call",
    "tool_result",
    "final_answer",
)


def message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content)


def serialize_tool_result(result: Any) -> str:
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str is enough
        return json.dumps(str(result), ensure_ascii=False)


class _DuckChatModel(BaseChatModel):
    """Adapt the repository's scripted test models to BaseChatModel."""

    delegate: Any = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return "meoclaw-duck"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _DuckChatModel:
        bind = getattr(self.delegate, "bind_tools", None)
        if callable(bind):
            try:
                self.delegate = bind(tools, **kwargs)
            except TypeError:
                # Legacy scripted models in the test-suite accept only tools.
                self.delegate = bind(tools)
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        response = self.delegate.invoke(messages, **kwargs)
        if not isinstance(response, AIMessage):
            response = AIMessage(content=message_text(response))
        return ChatResult(generations=[ChatGeneration(message=response)])


def _as_chat_model(model: Any) -> BaseChatModel:
    if isinstance(model, BaseChatModel):
        return model
    return _DuckChatModel(delegate=model)


def _decode_tool_content(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            if content.startswith("Error:") and " is not a valid tool" in content:
                return f"未知工具：{content}"
            return content
    return content


def _tool_result_ok(message: ToolMessage) -> bool:
    if str(getattr(message, "status", "") or "") == "error":
        return False
    payload = _decode_tool_content(message.content)
    return not (isinstance(payload, Mapping) and payload.get("ok") is False)


def stream_react_events(
    task: str,
    *,
    workspace: str | Path | None = None,
    max_loops: int = 10,
    model: Any = None,
    model_name: str | None = None,
    temperature: float = 0.0,
    runtime: RuntimeState | None = None,
    tools: Sequence[Any] | None = None,
    system_prompt: str = ACTOR_PROMPT,
    message_sink: list[BaseMessage] | None = None,
    input_messages: Sequence[BaseMessage] | None = None,
    role: str = "actor",
    task_id: str = "",
    approval_tools: Sequence[str] | None = None,
    checkpointer: Any = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    extra_middleware: Sequence[AgentMiddleware] | None = None,
    summary_token_limit: int = 80_000,
    resume: Any = None,
    stream_tokens: bool = False,
) -> Iterator[dict[str, Any]]:
    """Run a create_agent graph and translate its native stream to events."""
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 不能为空")
    if not isinstance(max_loops, int) or isinstance(max_loops, bool) or max_loops < 1:
        raise ValueError(f"max_loops 必须是大于 0 的整数，收到 {max_loops!r}")

    if runtime is None:
        if workspace is None:
            raise ValueError("必须提供 workspace 或 runtime 之一")
        runtime = RuntimeState(workspace=workspace)
    if approval_tools and checkpointer is None:
        raise ValueError("approval_tools 需要 SQLite/InMemory checkpointer")

    tool_list = list(tools) if tools is not None else build_tools(runtime)
    chat_model = _as_chat_model(
        model
        if model is not None
        else create_model(model=model_name, temperature=temperature)
    )
    stack = (
        list(middleware)
        if middleware is not None
        else build_agent_middleware(
            chat_model,
            max_model_calls=max_loops,
            summary_token_limit=summary_token_limit,
            approval_tools=approval_tools,
        )
    )
    if extra_middleware:
        stack.extend(extra_middleware)
    agent = create_agent(
        chat_model,
        tools=tool_list,
        system_prompt=system_prompt,
        middleware=stack,
        checkpointer=checkpointer,
        name=f"{role}-agent",
    )

    initial = list(input_messages) if input_messages is not None else []
    if not initial:
        initial = [HumanMessage(content=task)]
    thread_id = str((runtime.metadata or {}).get("thread_id") or "")
    if checkpointer is not None and not thread_id:
        thread_id = f"{role}-{uuid4().hex}"
    config: dict[str, Any] = {
        "recursion_limit": max(60, max_loops * 12 + 30)
    }
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}

    loops = 0
    last_ai_text = ""
    stopped_reason = "end"
    saw_final = False
    paused = False
    emitted_ai_ids: set[str] = set()
    pending_calls: list[dict[str, Any]] = []

    stream_input: Any = Command(resume=resume) if resume is not None else {"messages": initial}
    stream_mode: str | list[str] = (
        ["updates", "messages"] if stream_tokens else "updates"
    )
    for chunk in agent.stream(
        stream_input,
        config=config,
        stream_mode=stream_mode,
    ):
        if isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] in {
            "updates",
            "messages",
        }:
            mode, payload = chunk
        else:
            mode, payload = "updates", chunk
        if mode == "messages":
            if role != "verifier" and isinstance(payload, tuple) and payload:
                message = payload[0]
                if isinstance(message, (AIMessage, AIMessageChunk)):
                    text = message_text(message.content)
                    if text:
                        yield {
                            "type": "ai_token",
                            "content": text,
                            "loop": loops,
                        }
            continue

        if not isinstance(payload, Mapping):
            continue
        update = payload
        interrupts = update.get("__interrupt__")
        if interrupts:
            for item in interrupts:
                value = getattr(item, "value", {}) or {}
                tool_name = str(value.get("name") or "")
                call = next(
                    (
                        pending
                        for pending in pending_calls
                        if str(pending.get("id") or "")
                        == str(value.get("tool_call_id") or "")
                    ),
                    None,
                )
                if call is not None:
                    pending_calls.remove(call)
                    yield {
                        "type": "tool_call",
                        "name": str(call.get("name") or ""),
                        "args": call.get("args") or {},
                        "id": str(call.get("id") or ""),
                        "loop": loops,
                    }
                yield {
                    "type": "approval_request",
                    "id": str(getattr(item, "id", "") or ""),
                    "tool": tool_name,
                    "args": value.get("args") or {},
                    "detail": str(value.get("description") or ""),
                    "thread_id": thread_id,
                    "loop": loops,
                }
            paused = True
            break
        update = payload.get("model")
        if isinstance(update, Mapping):
            for message in update.get("messages") or []:
                if not isinstance(message, AIMessage):
                    continue
                loops += 1
                message_id = str(message.id or f"ai-{loops}")
                if message_id in emitted_ai_ids:
                    continue
                emitted_ai_ids.add(message_id)
                text = message_text(message.content)
                if text.strip():
                    last_ai_text = text
                tool_calls = list(message.tool_calls or [])
                yield {
                    "type": "ai_message",
                    "content": text,
                    "tool_calls": tool_calls,
                    "loop": loops,
                }
                pending_calls.extend(dict(call) for call in tool_calls)
                if not tool_calls:
                    saw_final = True
                    stopped_reason = "no_tool_calls"
                tagged = tag_message_context(
                    message,
                    role=role,
                    task_id=task_id or thread_id,
                    visible_to=(
                        ["planner", "actor", "verifier", "final"]
                        if role != "verifier"
                        else ["actor", "verifier", "final"]
                    ),
                )
                if message_sink is not None:
                    message_sink.append(tagged)

        update = payload.get("tools")
        if isinstance(update, Mapping):
            for message in update.get("messages") or []:
                if not isinstance(message, ToolMessage):
                    continue
                payload = _decode_tool_content(message.content)
                call_id = str(message.tool_call_id or "")
                call = next(
                    (item for item in pending_calls if str(item.get("id") or "") == call_id),
                    None,
                )
                if call is None and pending_calls:
                    call = pending_calls[0]
                if call is not None:
                    pending_calls.remove(call)
                    yield {
                        "type": "tool_call",
                        "name": str(call.get("name") or ""),
                        "args": call.get("args") or {},
                        "id": str(call.get("id") or ""),
                        "loop": loops,
                    }
                yield {
                    "type": "tool_result",
                    "name": message.name or "",
                    "result": payload,
                    "content": serialize_tool_result(payload),
                    "ok": _tool_result_ok(message),
                    "loop": loops,
                }
                tagged = tag_message_context(
                    message,
                    role=role,
                    task_id=task_id or thread_id,
                    visible_to=["actor", "verifier", "final"],
                )
                if message_sink is not None:
                    message_sink.append(tagged)

    for call in pending_calls:
        yield {
            "type": "tool_call",
            "name": str(call.get("name") or ""),
            "args": call.get("args") or {},
            "id": str(call.get("id") or ""),
            "loop": loops,
        }
    if paused:
        stopped_reason = "approval_required"
    elif not saw_final and loops >= max_loops:
        stopped_reason = "max_loops"
    yield {
        "type": "final_answer",
        "content": last_ai_text,
        "loops": loops,
        "stopped_reason": stopped_reason,
    }
