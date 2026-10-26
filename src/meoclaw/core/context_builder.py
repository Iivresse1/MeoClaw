"""Build role-specific model contexts from the canonical conversation history.

The graph owns one append-only ``messages`` timeline.  A ContextBuilder projects
that timeline into the smallest safe view needed by a role instead of sending the
same transcript to every model call.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from meoclaw.core.context import require_runtime
from meoclaw.graph.memory import build_layered_memory, format_layered_memory_for_prompt

__all__ = [
    "MESSAGE_ROLES_KEY",
    "MESSAGE_TASK_ID_KEY",
    "ContextBuilder",
    "ContextBundle",
    "tag_message_context",
]

ContextRole = Literal["planner", "actor", "verifier", "final"]

MESSAGE_ROLES_KEY = "context_roles"
MESSAGE_TASK_ID_KEY = "context_task_id"

# The verifier must not be primed with unverified actor narration.  These roles
# may instead inspect the current task, structured state and tool evidence.
_ROLE_ORDER: tuple[str, ...] = ("planner", "actor", "verifier", "final")


def _message_roles(message: BaseMessage) -> frozenset[str]:
    raw = (message.additional_kwargs or {}).get(MESSAGE_ROLES_KEY) or []
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item) for item in raw)


def tag_message_context(
    message: BaseMessage,
    *,
    role: str,
    task_id: str = "",
    visible_to: Iterable[str] | None = None,
) -> BaseMessage:
    """Return a copy of ``message`` tagged for task/role context filtering."""
    metadata = dict(message.additional_kwargs or {})
    roles = set(_message_roles(message))
    roles.add(role)
    roles.update(str(item) for item in (visible_to or ()))
    metadata[MESSAGE_ROLES_KEY] = [name for name in _ROLE_ORDER if name in roles]
    if task_id:
        metadata[MESSAGE_TASK_ID_KEY] = task_id
    return message.model_copy(update={"additional_kwargs": metadata})


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts).strip()
    return str(content).strip()


def _task_id(state: Mapping[str, Any]) -> str:
    runtime = require_runtime(state)
    return str((runtime.metadata or {}).get("task_id") or "")


def _current_run_messages(
    messages: Sequence[BaseMessage], task_id: str
) -> list[BaseMessage]:
    if not task_id:
        return list(messages)
    selected: list[BaseMessage] = []
    for message in messages:
        metadata = message.additional_kwargs or {}
        if str(metadata.get(MESSAGE_TASK_ID_KEY) or "") == task_id:
            selected.append(message)
    return selected


def _conversation_summary(role: str, state: Mapping[str, Any]) -> str:
    require_runtime(state)
    memory = build_layered_memory(state, node=role)
    return format_layered_memory_for_prompt(memory)


def _structured_state(state: Mapping[str, Any], role: str) -> dict[str, Any]:
    """Select typed state fields that are authoritative for this role."""
    common: dict[str, Any] = {
        "task": str(state.get("task") or ""),
        "max_attempts": int(state.get("max_attempts") or 3),
        "attempts": int(state.get("attempts") or 0),
        "last_error": str(state.get("last_error") or ""),
    }
    if role == "planner":
        common.update(
            {
                "plan_summary": str(state.get("plan_summary") or ""),
                "todos": list(state.get("todos") or []),
                "acceptance_criteria": list(state.get("acceptance_criteria") or []),
                "next_instruction": str(state.get("next_instruction") or ""),
                "research_notes": str(state.get("research_notes") or ""),
                "sources": list(state.get("sources") or []),
            }
        )
    elif role == "actor":
        common.update(
            {
                "plan_summary": str(state.get("plan_summary") or ""),
                "todos": list(state.get("todos") or []),
                "acceptance_criteria": list(state.get("acceptance_criteria") or []),
                "verification_commands": list(state.get("verification_commands") or []),
                "next_instruction": str(state.get("next_instruction") or ""),
                "research_notes": str(state.get("research_notes") or ""),
                "sources": list(state.get("sources") or []),
            }
        )
    elif role == "verifier":
        common.update(
            {
                "plan_summary": str(state.get("plan_summary") or ""),
                "todos": list(state.get("todos") or []),
                "acceptance_criteria": list(state.get("acceptance_criteria") or []),
                "verification_commands": list(state.get("verification_commands") or []),
                "verification_results": list(state.get("verification_results") or []),
                "last_actor_summary": str(state.get("last_actor_summary") or ""),
            }
        )
    else:
        common.update(
            {
                "verification_checks": list(state.get("verification_checks") or []),
                "verification_results": list(state.get("verification_results") or []),
                "last_actor_summary": str(state.get("last_actor_summary") or ""),
                "next_instruction": str(state.get("next_instruction") or ""),
            }
        )
    return common


def _evidence_from_tools(
    messages: Sequence[BaseMessage], *, max_chars: int = 12_000
) -> str:
    blocks: list[str] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            text = _text(message)
            if text:
                name = message.name or "tool"
                status = getattr(message, "status", None) or "success"
                blocks.append(f"[{name}; {status}]\n{text}")
    text = "\n\n".join(blocks)
    return text if len(text) <= max_chars else text[: max(0, max_chars - 3)] + "..."


@dataclass(frozen=True)
class ContextBundle:
    """A model-ready, role-scoped projection of graph state."""

    system_prompt: str
    messages: list[BaseMessage]
    role: str
    task_id: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ContextBuilder:
    """Compile canonical messages and typed state into a role-specific view."""

    def __init__(
        self,
        *,
        max_cross_task_messages: int = 6,
        max_message_chars: int = 4_000,
        max_evidence_chars: int = 12_000,
    ) -> None:
        self.max_cross_task_messages = max(0, int(max_cross_task_messages))
        self.max_message_chars = max(256, int(max_message_chars))
        self.max_evidence_chars = max(512, int(max_evidence_chars))

    def build(
        self,
        role: ContextRole,
        state: Mapping[str, Any],
        system_prompt: str,
        *,
        task_id: str | None = None,
        instruction: str = "",
    ) -> ContextBundle:
        timeline = list(state.get("messages") or [])
        current_task_id = task_id if task_id is not None else _task_id(state)
        current = _current_run_messages(timeline, current_task_id)

        if role == "actor":
            # Preserve complete AI/tool pairs from the current task attempt.
            history = current
        elif role == "verifier":
            # Evidence is rendered as text so a ToolMessage is never orphaned
            # from its original AI tool-call message.
            history = []
        else:
            # Planner/final get concise text conversation only.  Structured state
            # below carries plans, todos and verification facts.
            candidates = [
                message
                for message in timeline
                if isinstance(message, (HumanMessage, AIMessage))
                and not getattr(message, "tool_calls", None)
                and _text(message)
            ]
            history = (
                candidates[-self.max_cross_task_messages :]
                if self.max_cross_task_messages
                else []
            )
            history = [
                message.model_copy(
                    update={
                        "content": _text(message)[: self.max_message_chars]
                    }
                )
                for message in history
            ]

        payload = {
            "role": role,
            "task_id": current_task_id,
            "instruction": instruction.strip(),
            "state": _structured_state(state, role),
            "memory": _conversation_summary(role, state),
        }
        if role == "verifier":
            evidence = _evidence_from_tools(
                current, max_chars=self.max_evidence_chars
            )
            if evidence:
                payload["tool_evidence"] = evidence

        prior_failure = str(state.get("last_error") or "").strip()
        failure_notice = (
            "Previous attempt failed verification. Fix the specific issue below.\n\n"
            if prior_failure
            else ""
        )
        instruction_section = (
            f"## Role instruction\n{instruction.strip()}\n\n"
            if instruction.strip()
            else ""
        )
        brief = (
            "## Role context\n"
            "Use the structured state and memory below as context. Verification "
            "evidence from the workspace and commands takes precedence over prose.\n\n"
            + failure_notice
            + instruction_section
            + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        )
        messages = [*history, HumanMessage(content=brief)]
        return ContextBundle(
            system_prompt=system_prompt,
            messages=messages,
            role=role,
            task_id=current_task_id,
            diagnostics={
                "history_messages": len(history),
                "tool_evidence_chars": len(payload.get("tool_evidence", "")),
            },
        )
