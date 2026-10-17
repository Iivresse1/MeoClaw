"""从当前激活供应商（CC Switch 式配置）构造 ChatOpenAI；支持对任意供应商条目「测试连接」。"""
from .. import config


def build_model(settings: dict | None = None):
    p = config.get_active_provider(settings)
    if not p:
        raise RuntimeError("尚未配置模型供应商，请打开设置页添加")
    return build_model_for(p, settings)


def build_model_for(provider: dict, settings: dict | None = None):
    """provider: {name, model, base_url, api_key}。"""
    from langchain_openai import ChatOpenAI

    api_key = provider.get("api_key") or ""
    if not api_key:
        raise RuntimeError(f"供应商「{provider.get('name')}」缺少 API Key，请在设置页填写")
    kwargs = dict(
        model=provider.get("model") or "gpt-4o-mini",
        api_key=api_key,
        temperature=float((settings or config.load_settings()).get("temperature", 0.0)),
    )
    base_url = (provider.get("base_url") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)
