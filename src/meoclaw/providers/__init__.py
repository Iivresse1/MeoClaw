"""模型提供方：目前只有 OpenAI 兼容实现。"""

from meoclaw.providers.openai_provider import create_model

__all__ = ["create_model"]
