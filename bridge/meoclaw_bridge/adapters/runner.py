"""Background task execution and LangGraph HITL resume routing.

Agent execution itself is owned by LangGraph/LangChain.  This adapter only moves
native events into an asynchronously consumed queue and routes approval decisions
back through ``Command(resume=...)``.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Any

from .. import config
from . import model_factory, system_tools

CHAT_SYSTEM_PROMPT = (
    "你是一位简洁可靠的中文桌面助理。"
    "需要最新信息时可以使用 WebSearchTool 搜索，并用 WebFetchTool 读取网页正文。"
    "回答引用网页资料时应说明来源。"
)
SYSTEM_PROMPT = (
    "你是我的桌面助理，可以联网搜索和读取网页，也可以调用系统工具"
    "（截图/剪贴板/打开链接与工作区文件）。"
    "敏感操作会由 LangChain 中断并要求用户批准；被拒绝时不要重试。"
    "回答保持简洁、中文。"
)
APPROVAL_TOOLS = (
    "TakeScreenshot",
    "ClipboardRead",
    "ClipboardWrite",
    "OpenUrl",
    "OpenWorkspaceFile",
)


class ResumeRegistry:
    """Map LangGraph interrupt IDs to task queues without blocking worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._approval_tasks: dict[str, str] = {}
        self._queues: dict[str, queue.Queue] = {}

    def register_queue(self, task_id: str, q: queue.Queue) -> None:
        with self._lock:
            self._queues[task_id] = q

    def register_approval(self, approval_id: str, task_id: str) -> None:
        with self._lock:
            self._approval_tasks[approval_id] = task_id

    def take_approval(self, approval_id: str) -> tuple[str, queue.Queue] | None:
        with self._lock:
            task_id = self._approval_tasks.pop(approval_id, None)
            if not task_id:
                return None
            q = self._queues.get(task_id)
            return (task_id, q) if q is not None else None

    def remove_task(self, task_id: str) -> None:
        with self._lock:
            self._queues.pop(task_id, None)
            self._approval_tasks = {
                aid: tid
                for aid, tid in self._approval_tasks.items()
                if tid != task_id
            }


REGISTRY = ResumeRegistry()


def start(
    mode: str,
    task: str,
    settings: dict,
    session_ws,
    history: list | None = None,
    *,
    task_id: str,
    session_id: str,
    checkpointer: Any,
) -> queue.Queue:
    q: queue.Queue = queue.Queue()
    REGISTRY.register_queue(task_id, q)
    threading.Thread(
        target=_worker,
        kwargs={
            "q": q,
            "mode": mode,
            "task": task,
            "settings": settings,
            "session_ws": session_ws,
            "history": history,
            "task_id": task_id,
            "session_id": session_id,
            "checkpointer": checkpointer,
            "resume": None,
        },
        daemon=True,
    ).start()
    return q


def resume_approval(approval_id: str, decision: bool) -> bool:
    """Resume the interrupted LangGraph task exactly once."""
    found = REGISTRY.take_approval(approval_id)
    if found is None:
        return False
    task_id, q = found
    context = _TASK_CONTEXTS.get(task_id)
    if context is None:
        return False
    resume_value: dict[str, Any]
    if decision:
        resume_value = {"type": "approve"}
    else:
        resume_value = {"type": "reject", "message": "用户拒绝了该操作"}
    threading.Thread(
        target=_worker,
        kwargs={**context, "q": q, "resume": resume_value},
        daemon=True,
    ).start()
    return True


_TASK_CONTEXTS: dict[str, dict[str, Any]] = {}


def _worker(
    q: queue.Queue,
    mode: str,
    task: str,
    settings: dict,
    session_ws,
    history: list | None,
    task_id: str,
    session_id: str,
    checkpointer: Any,
    resume: dict[str, Any] | None,
) -> None:
    _TASK_CONTEXTS[task_id] = {
        "mode": mode,
        "task": task,
        "settings": settings,
        "session_ws": session_ws,
        "history": history,
        "task_id": task_id,
        "session_id": session_id,
        "checkpointer": checkpointer,
    }
    paused = False
    try:
        tavily_key = config.get_tavily_api_key()
        from meoclaw.tools.web_access import tavily_api_key_scope

        with tavily_api_key_scope(tavily_key):
            model = model_factory.build_model(settings)
            if mode == "chat" and resume is None:
                _run_chat(q, model, task, history, session_ws, task_id, session_id)
            elif mode == "workspace":
                _run_agent(
                    q, model, task, settings, session_ws, task_id, session_id, checkpointer
                )
            else:
                paused = _run_tools(
                    q,
                    model,
                    task,
                    session_ws,
                    task_id,
                    session_id,
                    checkpointer,
                    resume,
                )
    except Exception as exc:  # noqa: BLE001 - worker boundary must forward every failure
        q.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        if not paused:
            REGISTRY.remove_task(task_id)
            _TASK_CONTEXTS.pop(task_id, None)
            q.put({"type": "ui_done"})


def _run_chat(q, model, task, history, session_ws, task_id, session_id):
    from langchain_core.messages import AIMessage, HumanMessage

    from meoclaw.core.react import stream_react_events
    from meoclaw.core.state import RuntimeState
    from meoclaw.tools.registry import build_web_tools

    msgs = []
    for role, content in history or []:
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    msgs.append(HumanMessage(content=task))

    runtime = RuntimeState(workspace=session_ws)
    runtime.metadata.update(
        {"task_id": task_id, "thread_id": task_id, "session_id": session_id}
    )
    for event in stream_react_events(
        task,
        runtime=runtime,
        model=model,
        tools=build_web_tools(),
        max_loops=6,
        system_prompt=CHAT_SYSTEM_PROMPT,
        input_messages=msgs,
        role="chat",
        task_id=task_id,
        stream_tokens=True,
    ):
        event.setdefault("node", "chat")
        q.put(event)


def _run_agent(
    q,
    model,
    task,
    settings,
    session_ws,
    task_id,
    session_id,
    checkpointer,
):
    from meoclaw.core.agent import stream_agent_events

    for ev in stream_agent_events(
        task,
        runtime=None,
        workspace=str(session_ws),
        max_attempts=int(settings.get("max_attempts", 3)),
        model=model,
        task_id=task_id,
        session_id=session_id,
        checkpointer=checkpointer,
        sandbox_mode=os.environ.get("MEOCLAW_SANDBOX", "docker"),
    ):
        q.put(ev)


def _run_tools(
    q,
    model,
    task,
    session_ws,
    task_id,
    session_id,
    checkpointer,
    resume,
) -> bool:
    from meoclaw.core.react import stream_react_events
    from meoclaw.core.state import RuntimeState

    runtime = RuntimeState(workspace=session_ws)
    runtime.metadata.update(
        {"task_id": task_id, "thread_id": task_id, "session_id": session_id}
    )
    tools = system_tools.build_system_tools(session_ws)
    paused = False
    for ev in stream_react_events(
        task,
        runtime=runtime,
        model=model,
        tools=tools,
        max_loops=8,
        system_prompt=SYSTEM_PROMPT,
        role="system",
        task_id=task_id,
        approval_tools=APPROVAL_TOOLS,
        checkpointer=checkpointer,
        resume=resume,
        stream_tokens=True,
    ):
        if ev.get("type") == "approval_request":
            paused = True
            REGISTRY.register_approval(str(ev.get("id") or ""), task_id)
        elif ev.get("type") == "final_answer" and ev.get("stopped_reason") == "approval_required":
            continue
        q.put(ev)
    return paused
