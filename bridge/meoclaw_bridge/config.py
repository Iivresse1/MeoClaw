"""路径与设置：settings.json 存非敏感项；API Key 按「供应商」存 keyring（失败降级本地文件）。

模型配置采用 CC Switch 式逻辑：多供应商条目（名称/模型/Base URL/API Key），
一个 active_provider 指向当前使用的供应商，可在设置页一键切换。
"""
import json
import os
from pathlib import Path

APP_NAME = "MeoClawDesktop"
BASE = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
SESSIONS = BASE / "sessions"
SETTINGS_FILE = BASE / "settings.json"
FALLBACK_SECRETS = BASE / ".secrets.json"

DEFAULTS = {
    "temperature": 0.0,
    "max_attempts": 3,
    "permission": "workspace",       # chat / workspace / system
    "providers": [],                 # [{"name","model","base_url"}]
    "active_provider": "",           # 当前供应商 name
    "sidebar_open": True,
}

SESSIONS.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
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


def session_workspace(session_id: str) -> Path:
    ws = SESSIONS / session_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws
