"""MeoClaw LangGraph 工作流的共享状态定义。

设计要点：
- `MeoGraphState` 用 `total=False`：节点只返回自己写过的字段，未写的字段不会报错。
- `messages` 用 `Annotated[list[BaseMessage], add_messages]` 标注 reducer，
  LangGraph 会把各节点新增的消息**追加合并**（并按 id 去重/更新），而不是整段覆盖。
- 注意这里**没有**使用 `from __future__ import annotations`：
  LangGraph 在运行时会读取类型注解来构建 state channel（`get_type_hints(..., include_extras=True)`），
  保持注解是真实对象最稳妥。Python >= 3.10 因此 `int | None` 可以直接用。
"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from meoclaw.core.state import RuntimeState

__all__ = [
    "AgentHandoff",
    "MeoGraphState",
    "SourceItem",
    "TodoItem",
    "VerificationCheck",
    "VerificationResult",
]


class TodoItem(TypedDict):
    """计划中的一条待办。

    status 取值："pending" | "in_progress" | "completed" | "blocked"
    """

    id: str
    content: str
    status: str
    note: str


class VerificationResult(TypedDict):
    """一次验收命令的执行结果（供"验证/修复"回路判断是否需要重试）。"""

    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str


class VerificationCheck(TypedDict):
    """验证节点给出的一条检查结论。"""

    name: str
    passed: bool
    detail: str


class SourceItem(TypedDict, total=False):
    """检索到的一条来源（记忆里只保留 title 与 url）。"""

    title: str
    url: str
    snippet: str


class AgentHandoff(TypedDict, total=False):
    """一次"planner → 专家 Agent"的委派记录。"""

    from_agent: str
    to_agent: str
    instruction: str
    result: str


class MeoGraphState(TypedDict, total=False):
    """整张图共享的状态。

    Attributes:
        task: 用户任务原文。
        runtime: 运行时状态，携带 workspace 与各类预算；所有工具都以它为准。
        messages: 对话消息（带 add_messages reducer，只增不覆盖）。
        plan_summary: 计划摘要，便于人类审阅与后续节点复用。
        todos: 拆解出的待办列表。
        acceptance_criteria: 判定"做完了"的标准。
        verification_commands: 需要执行的验收命令。
        verification_results: 上述命令的执行结果。
        verification_checks: 验证节点给出的逐条检查结论。
        passed: 验收是否通过。
        attempts: 已尝试轮数（verifier 每跑一次 +1）。
        max_attempts: 最大尝试轮数。
        last_actor_summary: actor 本轮的收尾总结。
        last_error: 最近一次失败原因（planner/actor 会读到它）。
        next_instruction: verifier 建议下一轮修复什么。
        final_answer: 最终交付给用户的回答。
        research_notes: 检索/调研得到的要点（进记忆前会被截断）。
        sources: 来源列表（进记忆时只保留 title/url）。
        agent_handoffs: 委派给专家 Agent 的交接记录（记忆里只留最近几条）。
        code_agent_summary: codeAgent 本轮的收尾总结。
        verifier_summary: verifier 的结论摘要。
    """

    task: str
    runtime: RuntimeState
    messages: Annotated[list[BaseMessage], add_messages]
    plan_summary: str
    todos: list[TodoItem]
    acceptance_criteria: list[str]
    verification_commands: list[str]
    verification_results: list[VerificationResult]
    verification_checks: list[VerificationCheck]
    passed: bool
    attempts: int
    max_attempts: int
    last_actor_summary: str
    last_error: str
    next_instruction: str
    final_answer: str
    # --- 检索 / 专家 Agent ---
    research_notes: str
    sources: list[SourceItem]
    agent_handoffs: list[AgentHandoff]
    code_agent_summary: str
    verifier_summary: str
