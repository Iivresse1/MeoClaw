"""FastAPI + WebSocket 桥接服务：仅监听 127.0.0.1，供 Electron 壳调用。

REST：会话/消息、设置、供应商（CC Switch 式）、审批 resolve、启动任务。
WS：/ws/task/{task_id} 推送 runner 事件流；final_answer 自动落库为助手消息。

启动：python -m meoclaw_bridge.server（环境变量 MEOCLAW_BRIDGE_PORT，默认 8931）
"""
import asyncio
import os
import queue
import uuid

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, store
from .adapters import model_factory, runner

app = FastAPI(title="MeoClaw Bridge")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TASKS: dict[str, tuple[str, queue.Queue]] = {}  # task_id -> (session_id, 事件队列)


class TaskReq(BaseModel):
    task: str
    mode: str = "workspace"          # chat / workspace / system
    session_id: str | None = None


class ProviderReq(BaseModel):
    name: str
    model: str = ""
    base_url: str = ""
    api_key: str | None = None       # 空/None = 保持已存密钥不变


class ActiveReq(BaseModel):
    name: str


class ApprovalReq(BaseModel):
    decision: bool


class TestReq(BaseModel):
    name: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""                # 留空则读取该供应商已存密钥


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/sessions")
def sessions():
    return store.list_sessions()


@app.get("/api/sessions/{sid}/messages")
def messages(sid: str):
    return store.get_messages(sid)


@app.delete("/api/sessions/{sid}")
def remove_session(sid: str):
    store.delete_session(sid)
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    s = config.load_settings()
    out = dict(s)
    out["providers"] = [
        {**p, "has_key": bool(config.get_secret(config.provider_key(p.get("name", ""))))}
        for p in s.get("providers", [])
    ]
    return out


@app.post("/api/settings")
def patch_settings(patch: dict):
    allowed = {k: patch[k] for k in ("temperature", "max_attempts", "permission", "sidebar_open") if k in patch}
    return config.save_settings(allowed)


@app.post("/api/providers")
def upsert_provider(req: ProviderReq):
    name = req.name.strip()
    if not name:
        return {"ok": False, "message": "供应商名称不能为空"}
    s = config.load_settings()
    providers = [p for p in s.get("providers", []) if p.get("name") != name]
    providers.append({"name": name, "model": req.model.strip(), "base_url": req.base_url.strip()})
    if req.api_key:
        config.set_secret(config.provider_key(name), req.api_key)
    s = config.save_settings({"providers": providers})
    if not s.get("active_provider"):
        config.save_settings({"active_provider": name})
    return {"ok": True}


@app.delete("/api/providers/{name}")
def delete_provider(name: str):
    s = config.load_settings()
    providers = [p for p in s.get("providers", []) if p.get("name") != name]
    active = s.get("active_provider") or ""
    if active == name:
        active = ""
    config.set_secret(config.provider_key(name), "")
    return config.save_settings({"providers": providers, "active_provider": active})


@app.post("/api/providers/active")
def set_active(req: ActiveReq):
    return config.save_settings({"active_provider": req.name})


@app.post("/api/test-connection")
def test_conn(req: TestReq):
    key = req.api_key or config.get_secret(config.provider_key(req.name))
    p = {"name": req.name, "model": req.model, "base_url": req.base_url, "api_key": key}
    settings = {"temperature": config.load_settings().get("temperature", 0.0)}
    try:
        m = model_factory.build_model_for(p, settings)
        r = m.invoke("回复：ok")
        return {"ok": True, "message": f"连接成功：{str(getattr(r, 'content', r))[:60]}"}
    except Exception as e:
        return {"ok": False, "message": f"连接失败：{type(e).__name__}: {e}"[:200]}


@app.post("/api/task")
def start_task(req: TaskReq):
    task = (req.task or "").strip()
    if not task:
        return {"ok": False, "message": "任务内容为空"}
    mode = req.mode if req.mode in ("chat", "workspace", "system") else "workspace"
    settings = config.load_settings()
    sid = req.session_id or store.create_session(task[:60], mode)  # 懒创建：首条消息才建会话
    store.add_message(sid, "user", task)
    history = None
    if mode == "chat":
        rows = store.get_messages(sid)
        history = [(r["role"], r["content"]) for r in rows if r["role"] in ("user", "assistant")][:-1]
    q = runner.start(mode, task, settings, config.session_workspace(sid), history)
    tid = uuid.uuid4().hex[:12]
    TASKS[tid] = (sid, q)
    return {"ok": True, "task_id": tid, "session_id": sid}


@app.post("/api/approval/{aid}")
def resolve_approval(aid: str, req: ApprovalReq):
    runner.APPROVALS.resolve(aid, req.decision)
    return {"ok": True}


@app.websocket("/ws/task/{tid}")
async def ws_task(ws: WebSocket, tid: str):
    await ws.accept()
    rec = TASKS.get(tid)
    if not rec:
        await ws.send_json({"type": "error", "message": "任务不存在或已结束"})
        await ws.close()
        return
    sid, q = rec
    try:
        while True:
            try:
                ev = await asyncio.to_thread(q.get, True, 125)  # 略大于审批超时
            except queue.Empty:
                await ws.send_json({"type": "error", "message": "等待结果超时"})
                break
            if ev.get("type") == "final_answer" and ev.get("content"):
                store.add_message(sid, "assistant", str(ev["content"]), {"node": ev.get("node", "")})
            await ws.send_json(ev)
            if ev.get("type") == "ui_done":
                break
    except WebSocketDisconnect:
        pass
    finally:
        TASKS.pop(tid, None)
        try:
            await ws.close()
        except Exception:
            pass


def main():
    store.init()
    port = int(os.environ.get("MEOCLAW_BRIDGE_PORT", "8931"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
