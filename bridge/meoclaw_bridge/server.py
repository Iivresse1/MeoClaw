"""FastAPI + WebSocket 桥接服务：仅监听 127.0.0.1，供 Electron 壳调用。

REST：会话/消息、设置、供应商（CC Switch 式）、审批 resolve、启动任务。
WS：/ws/task/{task_id} 推送 runner 事件流；final_answer 自动落库为助手消息。

启动：python -m meoclaw_bridge.server（环境变量 MEOCLAW_BRIDGE_PORT，默认 8931）
"""
import asyncio
import hmac
import os
import queue
import shutil
import uuid

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from meoclaw.core.checkpoints import create_checkpointer

from . import config, store
from .adapters import model_factory, runner

app = FastAPI(title="MeoClaw Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.allowed_origins(),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

BRIDGE_TOKEN = ""
CHECKPOINTS = None

TASKS: dict[str, tuple[str, queue.Queue]] = {}  # task_id -> (session_id, 事件队列)


def _authorized(value: str) -> bool:
    global BRIDGE_TOKEN
    if not BRIDGE_TOKEN:
        BRIDGE_TOKEN = config.get_or_create_bridge_token()
    token = value.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, BRIDGE_TOKEN)


def _checkpointer():
    global CHECKPOINTS
    if CHECKPOINTS is None:
        CHECKPOINTS = create_checkpointer(config.CHECKPOINTS_DB)
    return CHECKPOINTS.saver


@app.middleware("http")
async def require_bridge_token(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path == "/api/health":
        return await call_next(request)
    if not _authorized(request.headers.get("authorization", "")):
        origin = request.headers.get("origin", "")
        headers = (
            {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
            if origin in config.allowed_origins()
            else None
        )
        return JSONResponse(
            status_code=401,
            content={"ok": False, "message": "unauthorized"},
            headers=headers,
        )
    return await call_next(request)


class TaskReq(BaseModel):
    task: str
    permission: str = "human"        # human / ai / full
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


class ArchiveReq(BaseModel):
    archived: bool = True


class TestReq(BaseModel):
    name: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""                # 留空则读取该供应商已存密钥


class TavilyReq(BaseModel):
    api_key: str


@app.get("/api/health")
def health():
    sandbox = os.environ.get("MEOCLAW_SANDBOX", "docker").lower()
    return {
        "ok": True,
        "sandbox": sandbox,
        "sandbox_available": sandbox != "docker" or shutil.which("docker") is not None,
    }


@app.get("/api/sessions")
def sessions():
    return store.list_sessions()


@app.get("/api/sessions/{sid}/messages")
def messages(sid: str):
    return store.get_messages(sid)


@app.post("/api/sessions/{sid}/archive")
def archive_session(sid: str, req: ArchiveReq):
    if not store.set_session_archived(sid, req.archived):
        return {"ok": False, "message": "会话不存在或 ID 非法"}
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
    if patch.get("permission") not in (None, "human", "ai", "full"):
        return {"ok": False, "message": "permission 必须是 human / ai / full"}
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


@app.get("/api/tavily")
def tavily_status():
    key, source = config.tavily_key_status()
    return {"ok": True, "configured": bool(key), "source": source}


@app.post("/api/tavily")
def configure_tavily(req: TavilyReq):
    key = (req.api_key or "").strip()
    if not key:
        return {"ok": False, "message": "Tavily API Key 不能为空"}

    from meoclaw.tools.web_search_tool import web_search

    result = web_search(
        "Tavily connectivity check",
        max_results=1,
        include_answer=False,
        api_key=key,
    )
    if result.get("ok") is not True:
        return {
            "ok": False,
            "message": f"连接失败：{result.get('error') or '未知错误'}"[:300],
        }

    config.set_tavily_api_key(key)
    return {"ok": True, "message": "Tavily 已配置并通过连接测试"}


@app.delete("/api/tavily")
def remove_tavily():
    config.set_tavily_api_key("")
    return {"ok": True, "message": "Tavily API Key 已移除"}


@app.post("/api/task")
def start_task(req: TaskReq):
    task = (req.task or "").strip()
    if not task:
        return {"ok": False, "message": "任务内容为空"}
    permission = (
        req.permission
        if req.permission in ("human", "ai", "full")
        else "human"
    )
    if req.session_id:
        if not config.SESSION_ID_RE.fullmatch(req.session_id) or not store.session_exists(
            req.session_id
        ):
            return {"ok": False, "message": "session_id 非法或会话不存在"}
        if store.session_is_archived(req.session_id):
            return {"ok": False, "message": "归档会话只能查看历史，请先恢复会话"}
        sid = req.session_id
        store.set_session_permission(sid, permission)
    else:
        sid = store.create_session(task[:60], permission)
    settings = config.load_settings()
    store.add_message(sid, "user", task)
    tid = uuid.uuid4().hex[:12]
    q = runner.start(
        permission,
        task,
        settings,
        config.session_workspace(sid),
        None,
        task_id=tid,
        session_id=sid,
        checkpointer=_checkpointer(),
    )
    TASKS[tid] = (sid, q)
    return {"ok": True, "task_id": tid, "session_id": sid}


@app.post("/api/approval/{aid}")
def resolve_approval(aid: str, req: ApprovalReq):
    if not runner.resume_approval(aid, req.decision):
        return {"ok": False, "message": "审批不存在、已过期或任务已结束"}
    return {"ok": True}


@app.websocket("/ws/task/{tid}")
async def ws_task(ws: WebSocket, tid: str):
    if not _authorized(ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
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
                ev = await asyncio.to_thread(q.get, True, 3600)
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
    reset_legacy = store.init()
    if reset_legacy:
        if config.SESSIONS.exists():
            for child in config.SESSIONS.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        config.CHECKPOINTS_DB.unlink(missing_ok=True)
    _checkpointer()
    port = int(os.environ.get("MEOCLAW_BRIDGE_PORT", "8931"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
