"""后台工作线程：按权限模式调用后端公开接口，事件推入队列；HITL 审批注册表。

chat      仅对话：直连模型（带最近会话历史）
workspace 在工作区内修改：meoclaw.core.agent.stream_agent_events（planner→actor→verifier→final）
system    系统工具：meoclaw.core.react.stream_react_events + 桌面层系统工具集（高危需审批）
"""
import queue
import threading
import uuid
from typing import Any

from . import model_factory, system_tools

CHAT_SYSTEM_PROMPT = "你是一位简洁可靠的中文桌面助理。"
SYSTEM_PROMPT = (
    "你是我的桌面助理，可以调用系统工具（截图/剪贴板/打开链接与工作区文件）。"
    "需要用户批准的工具会在调用时弹出确认；被拒绝时不要重试，改向用户说明。"
    "回答保持简洁、中文。"
)


class Approvals:
    """审批注册表：工具线程挂起等待，UI 侧 resolve 后放行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}

    def request(self, q: queue.Queue, tool: str, detail: str, timeout: float = 120.0) -> bool:
        aid = uuid.uuid4().hex[:10]
        ev = threading.Event()
        with self._lock:
            self._pending[aid] = (ev, {"decision": False})
        q.put({"type": "approval_request", "id": aid, "tool": tool, "detail": detail})
        got = ev.wait(timeout)
        with self._lock:
            rec = self._pending.pop(aid, (None, {}))
        return bool(got and rec[1].get("decision"))

    def resolve(self, aid: str, decision: bool) -> None:
        with self._lock:
            rec = self._pending.get(aid)
        if rec:
            rec[1]["decision"] = decision
            rec[0].set()


APPROVALS = Approvals()


def start(mode: str, task: str, settings: dict, session_ws, history: list | None = None) -> queue.Queue:
    """启动后台执行，返回事件队列。history=[(role,content)] 仅 chat 用于多轮上下文。"""
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_worker, args=(q, mode, task, settings, session_ws, history), daemon=True).start()
    return q


def _push(q: queue.Queue, ev: dict[str, Any]) -> None:
    q.put(ev)


def _worker(q, mode, task, settings, session_ws, history) -> None:
    try:
        model = model_factory.build_model(settings)
        if mode == "chat":
            _run_chat(q, model, task, history)
        elif mode == "workspace":
            _run_agent(q, model, task, settings, session_ws)
        else:
            _run_tools(q, model, task, session_ws)
    except Exception as e:
        _push(q, {"type": "error", "message": f"{type(e).__name__}: {e}"[:300]})
    finally:
        _push(q, {"type": "ui_done"})


def _run_chat(q, model, task, history):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    msgs = [SystemMessage(content=CHAT_SYSTEM_PROMPT)]
    for role, content in (history or []):
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    msgs.append(HumanMessage(content=task))
    full = []
    for chunk in model.stream(msgs):
        text = getattr(chunk, "content", "") or ""
        if text:
            full.append(text)
            _push(q, {"type": "ai_token", "content": text})
    _push(q, {"type": "final_answer", "content": "".join(full), "node": "chat"})


def _run_agent(q, model, task, settings, session_ws):
    from meoclaw.core.agent import stream_agent_events

    for ev in stream_agent_events(
        task,
        workspace=str(session_ws),
        max_attempts=int(settings.get("max_attempts", 3)),
        model=model,
    ):
        _push(q, ev)


def _run_tools(q, model, task, session_ws):
    from meoclaw.core.react import stream_react_events

    tools = system_tools.build_system_tools(session_ws, lambda t, d: APPROVALS.request(q, t, d))
    for ev in stream_react_events(
        task,
        workspace=str(session_ws),
        model=model,
        tools=tools,
        max_loops=8,
        system_prompt=SYSTEM_PROMPT,
    ):
        _push(q, ev)
