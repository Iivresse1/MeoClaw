"""路径与设置：settings.json 存非敏感项；API Key 按「供应商」存 keyring（失败降级本地文件）。

模型配置采用 CC Switch 式逻辑：多供应商条目（名称/模型/Base URL/API Key），
一个 active_provider 指向当前使用的供应商，可在设置页一键切换。
"""
import json
import os
import re
import secrets
from pathlib import Path

APP_NAME = "MeoClawDesktop"
BASE = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
SESSIONS = BASE / "sessions"
SETTINGS_FILE = BASE / "settings.json"
FALLBACK_SECRETS = BASE / ".secrets.json"
BRIDGE_TOKEN_FILE = BASE / "bridge_token"
CHECKPOINTS_DB = BASE / "checkpoints.sqlite"
SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")
TAVILY_SECRET_NAME = "tavily:api_key"

DEFAULTS = {
    "temperature": 0.0,
    "max_attempts": 3,
    "permission": "human",           # human / ai / full
    "providers": [],                 # [{"name","model","base_url"}]
    "active_provider": "",           # 当前供应商 name
    "sidebar_open": True,
}

def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    if s.get("permission") not in {"human", "ai", "full"}:
        s["permission"] = "human"
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    s.update(patch)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    return s


def provider_key(name: str) -> str:
    return f"provider:{name}"


def set_secret(name: str, value: str) -> None:
    try:
        import keyring
        keyring.set_password(APP_NAME, name, value)
        return
    except Exception:
        pass
    data = _fallback_read()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    FALLBACK_SECRETS.write_text(json.dumps(data), encoding="utf-8")
    try:
        FALLBACK_SECRETS.chmod(0o600)
    except Exception:
        pass


def get_secret(name: str) -> str:
    try:
        import keyring
        v = keyring.get_password(APP_NAME, name)
        if v:
            return v
    except Exception:
        pass
    return _fallback_read().get(name, "")


def _fallback_read() -> dict:
    if FALLBACK_SECRETS.exists():
        try:
            return json.loads(FALLBACK_SECRETS.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def get_active_provider(settings: dict | None = None) -> dict | None:
    """返回当前激活供应商的完整配置（含 api_key）。"""
    s = settings or load_settings()
    active = s.get("active_provider") or ""
    for p in s.get("providers", []):
        if p.get("name") == active:
            out = dict(p)
            out["api_key"] = get_secret(provider_key(active))
            return out
    return None


def tavily_key_status() -> tuple[str, str]:
    """Return ``(key, source)`` without exposing the key through the API."""
    configured = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if configured:
        return configured, "env"
    stored = (get_secret(TAVILY_SECRET_NAME) or "").strip()
    return (stored, "keyring") if stored else ("", "")


def get_tavily_api_key() -> str:
    return tavily_key_status()[0]


def set_tavily_api_key(value: str) -> None:
    """Persist the setup key in the OS keyring with the existing fallback."""
    key = (value or "").strip()
    set_secret(TAVILY_SECRET_NAME, key)


def session_workspace(session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(str(session_id or "")):
        raise ValueError("非法 session_id")
    root = SESSIONS.resolve()
    ws = (root / session_id).resolve()
    if ws.parent != root:
        raise ValueError("session_id 越出会话目录")
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def get_or_create_bridge_token() -> str:
    configured = os.environ.get("MEOCLAW_BRIDGE_TOKEN", "").strip()
    if configured:
        return configured
    if BRIDGE_TOKEN_FILE.exists():
        try:
            existing = BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
    token = secrets.token_urlsafe(32)
    BRIDGE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    BRIDGE_TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        BRIDGE_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def allowed_origins() -> list[str]:
    configured = os.environ.get("MEOCLAW_ALLOWED_ORIGINS", "")
    if configured.strip():
        return [item.strip() for item in configured.split(",") if item.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "null",  # packaged file:// renderer; still requires capability token
    ]
