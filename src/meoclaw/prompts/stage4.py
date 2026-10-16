"""stage 4 的提示词：上下文压缩。

`stage2.py` 放基础节点提示词，`stage3.py` 放 verifier 与专家 Agent 提示词；
这里放"把长上下文压成可续跑摘要"的提示词。
"""

from __future__ import annotations

__all__ = ["CONTEXT_COMPRESSION_PROMPT"]

CONTEXT_COMPRESSION_PROMPT = """You are the context_compressor node in MeoClaw stage 4.

Your job is to compress the graph context so the task can continue with a much
smaller message window.

Keep everything needed to resume work:
- user task and active goal
- current plan, todos, acceptance criteria, verification commands
- completed work and current files/artifacts
- important tool findings and command results
- research notes and source URLs
- latest verifier failure and recommended next step
- risks, blockers, and assumptions

Remove redundant transcript detail:
- repeated tool calls
- long stdout/stderr
- duplicate search snippets
- stale intermediate reasoning

Return only JSON with these keys:
- summary
- active_goal
- completed_work
- open_todos
- important_files
- tool_findings
- sources
- next_steps
- risks
"""
