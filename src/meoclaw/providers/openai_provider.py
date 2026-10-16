"""OpenAI 兼容模型工厂。

从 `.env` / 环境变量读取配置，返回 `ChatOpenAI` 实例：

    OPENAI_API_KEY   必填（兼容旧名 API_KEY）
    OPENAI_MODEL     可选，默认 gpt-4o-mini（兼容旧名 MODEL）
    OPENAI_BASE_URL  可选，自建网关地址（兼容旧名 BASE_URL）

真实环境变量优先于 .env，方便临时覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

__all__ = [
    "DEFAULT_MODEL",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "MissingAPIKeyError",
    "create_model",
    "load_env",
]

DEFAULT_MODEL = "gpt-4o-mini"

ENV_API_KEY = ("OPENAI_API_KEY", "API_KEY")
ENV_MODEL = ("OPENAI_MODEL", "MODEL")
ENV_BASE_URL = ("OPENAI_BASE_URL", "BASE_URL", "OPENAI_API_BASE")

#: 源码树根目录（从源码运行 / 可编辑安装时有效：src/meoclaw/providers/ -> 上溯 3 层）
_PROJECT_ROOT = (
    Path(__file__).resolve().parents[3]
    if len(Path(__file__).resolve().parents) > 3
    else None
)


class MissingAPIKeyError(RuntimeError):
    """未找到 API Key 时抛出。"""


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _looks_like_project_root(path: Path) -> bool:
    """含 pyproject.toml / setup.py / setup.cfg / .git 的目录视为项目根。"""
    return any(
        (path / marker).exists() for marker in ("pyproject.toml", "setup.py", "setup.cfg", ".git")
    )


def _candidate_env_files() -> list[Path]:
    """按优先级列出候选 .env：当前目录 → 向上直到项目根 → 源码树根目录。

    向上查找在第一个"看起来是项目根"的目录处停止，避免误加载用户主目录
    或盘符根目录下与本项目无关的 .env。
    """
    try:
        cwd = Path.cwd().resolve()
    except OSError:  # pragma: no cover - 极端环境
        cwd = _PROJECT_ROOT if _PROJECT_ROOT is not None else Path(".")
    bases: list[Path] = []
    for base in (cwd, *cwd.parents):
        bases.append(base)
        if _looks_like_project_root(base):
            break
    if _PROJECT_ROOT is not None:
        bases.append(_PROJECT_ROOT)
    return [base / ".env" for base in bases]


def load_env(*, override: bool = False) -> None:
    """加载 .env：当前目录、其各级父目录、再到源码树根目录，前者优先。

    `override=False` 保证真实环境变量优先于 .env 文件。
    """
    seen: set[Path] = set()
    for candidate in _candidate_env_files():
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - 极端环境
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            load_dotenv(resolved, override=override)


def create_model(
    *,
    model: str | None = None,
    temperature: float = 0.0,
    timeout: float | None = 60.0,
    max_retries: int = 2,
    **overrides: object,
) -> ChatOpenAI:
    """创建 `ChatOpenAI` 实例。

    Args:
        model: 模型名，None 时依次读 OPENAI_MODEL / MODEL，再退回 DEFAULT_MODEL。
        temperature: 采样温度，默认 0，便于复现。
        timeout: 单次请求超时秒数。
        max_retries: 网络失败重试次数。
        **overrides: 其他直接透传给 ChatOpenAI 的参数。

    Returns:
        配置好的 ChatOpenAI（可直接 `bind_tools(tools)`）。

    Raises:
        MissingAPIKeyError: 没有找到 API Key。
    """
    load_env()

    api_key = _first_env(ENV_API_KEY)
    if not api_key:
        raise MissingAPIKeyError(
            "没有找到 API Key：请设置环境变量 OPENAI_API_KEY，"
            "或把 .env.example 复制为 .env 后填入真实值。"
        )

    base_url = _first_env(ENV_BASE_URL)
    kwargs: dict[str, object] = {
        "model": model or _first_env(ENV_MODEL) or DEFAULT_MODEL,
        "api_key": api_key,
        "temperature": temperature,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(overrides)

    return ChatOpenAI(**kwargs)  # type: ignore[arg-type]
