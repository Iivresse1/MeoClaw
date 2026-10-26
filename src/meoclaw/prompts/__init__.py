"""各节点的系统提示词。

按"阶段"分文件：`stage2.py` 是 planner / actor / verifier / final 四个基础节点的提示词。
后续阶段（例如 supervisor + 专家 agent）再新增自己的模块，避免同一份提示词两处维护。
"""

from meoclaw.prompts.stage2 import (
    ACTOR_PROMPT,
    FINAL_PROMPT,
    PLANNER_PROMPT,
)
from meoclaw.prompts.stage3 import (
    CODE_AGENT_PROMPT,
    SEARCH_AGENT_PROMPT,
    SUPERVISOR_PROMPT,
    VERIFIER_PROMPT,
)

__all__ = [
    "ACTOR_PROMPT",
    "CODE_AGENT_PROMPT",
    "FINAL_PROMPT",
    "PLANNER_PROMPT",
    "SEARCH_AGENT_PROMPT",
    "SUPERVISOR_PROMPT",
    "VERIFIER_PROMPT",
]
