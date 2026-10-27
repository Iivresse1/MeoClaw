"""Background task execution and blocking tool-approval routing."""

from __future__ import annotations

import os
import queue
import threading
from typing import Any
from uuid import uuid4

from .. import config
from . import model_factory


class ApprovalRegistry:
    """Block one worker thread until the matching approval request is resolved."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict[str, bool]]] = {}

    def request(
        self,
        q: queue.Queue,
        tool: str,
        detail: str,
        *,
        timeout: float = 3600,
    ) -> bool:
        approval_id = uuid4().hex
        event = threading.Event()
        result: dict[str, bool] = {}
        with self._lock:
            self._pending[approval_id] = (event, result)
        q.put(
            {
                "type": "approval_request",
                "id": approval_id,
                "tool": tool,
                "detail": detail,
            }
        )
        if event.wait(timeout):
            return bool(result.get("decision"))
        with self._lock:
            self._pending.pop(approval_id, None)
        return False

    def resolve(self, approval_id: str, decision: bool) -> bool:
        with self._lock:
            pending = self._pending.pop(approval_id, None)
        if pending is None:
            return False
        event, result = pending
        result["decision"] = bool(decision)
        event.set()
        return True


# Backwards-compatible import for existing callers.
ResumeRegistry = ApprovalRegistry
REGISTRY = ApprovalRegistry()


def start(
    permission: str,
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
    threading.Thread(
        target=_worker,
        kwargs={
            "q": q,
            "permission": permission,
            "task": task,
            "settings": settings,
            "session_ws": session_ws,
            "history": history,
            "task_id": task_id,
            "session_id": session_id,
            "checkpointer": checkpointer,
        },
        daemon=True,
    ).start()
    return q


def resume_approval(approval_id: str, decision: bool) -> bool:
    """Resolve a blocked tool call exactly once."""
    return REGISTRY.resolve(approval_id, decision)


def _worker(
    q: queue.Queue,
    permission: str,
    task: str,
    settings: dict,
    session_ws,
    history: list | None,
    task_id: str,
    session_id: str,
    checkpointer: Any,
) -> None:
    try:
        tavily_key = config.get_tavily_api_key()
        from meoclaw.tools.web_access import tavily_api_key_scope

        with tavily_api_key_scope(tavily_key):
            model = model_factory.build_model(settings)
            _run_agent(
                q,
                model,
                task,
                settings,
                session_ws,
                task_id,
                session_id,
                checkpointer,
                permission,
            )
    except Exception as exc:  # noqa: BLE001 - worker boundary must forward every failure
        q.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        q.put({"type": "ui_done"})


def _run_agent(
    q,
    model,
    task,
    settings,
    session_ws,
    task_id,
    session_id,
    checkpointer,
    permission,
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
        permission_policy=permission,
        approval_handler=lambda tool, detail: REGISTRY.request(q, tool, detail),
        decision_sink=q.put,
        auto_route=True,
    ):
        q.put(ev)
