"""graph.memory 测试：三层记忆组装的边界与内容。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meoclaw.core.state import RuntimeState
from meoclaw.graph.memory import (
    HANDOFF_FIELD_LIMIT,
    HANDOFF_KEEP,
    HISTORY_LIMIT,
    HISTORY_SUMMARY_FILENAME,
    LAST_ERROR_LIMIT,
    NOTEPAD_LIMIT,
    NOTES_LIMIT,
    RULES_LAYER,
    _compact_sources,
    _short_text,
    _trim_handoffs,
    append_history_summary,
    build_layered_memory,
    format_layered_memory_for_prompt,
    read_history_summary,
    read_notepad,
    summarize_task_for_history,
)
from meoclaw.tools.notepad_tool import NOTEPAD_FILENAME, notepad_append
from meoclaw.tools.todo_tool import TODO_FILENAME


@pytest.fixture()
def runtime(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path / "ws")


@pytest.fixture()
def state(runtime: RuntimeState) -> dict[str, Any]:
    return {
        "task": "给 utils.py 加 add(a, b) 并写测试",
        "runtime": runtime,
        "plan_summary": "先实现 add，再补测试",
        "todos": [{"id": "t1", "content": "实现 add", "status": "pending", "note": ""}],
        "acceptance_criteria": ["add(1, 2) == 3"],
        "verification_commands": ["python -m pytest -q"],
        "attempts": 1,
        "max_attempts": 3,
    }


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def test_short_text() -> None:
    assert _short_text("abc", 10) == "abc"
    assert _short_text("a" * 100, 10) == "aaaaaaa..."
    assert len(_short_text("a" * 100, 10)) == 10
    assert _short_text(None, 10) == ""
    assert _short_text("abc", 0) == ""
    assert _short_text("abc", 3) == "abc"
    assert _short_text("abcdef", 3) == "..."
    assert _short_text(12345, 10) == "12345"


def test_trim_handoffs_keeps_last_six() -> None:
    handoffs = [
        {"from_agent": "planner", "to_agent": "codeAgent", "instruction": f"任务{i}", "result": "ok"}
        for i in range(8)
    ]

    trimmed = _trim_handoffs(handoffs)

    assert len(trimmed) == HANDOFF_KEEP == 6
    assert [item["instruction"] for item in trimmed] == [f"任务{i}" for i in range(2, 8)]


def test_trim_handoffs_clips_fields() -> None:
    trimmed = _trim_handoffs([{"result": "x" * 2000}])
    assert len(trimmed[0]["result"]) <= HANDOFF_FIELD_LIMIT


def test_trim_handoffs_ignores_non_mapping() -> None:
    assert _trim_handoffs(["oops", {"to_agent": "codeAgent"}]) == [
        {"to_agent": "codeAgent"}
    ]
    assert _trim_handoffs(None) == []


def test_compact_sources_keeps_only_title_and_url() -> None:
    compact = _compact_sources(
        [
            {"title": "Python 文档", "url": "https://docs.python.org", "snippet": "x" * 500},
            {"name": "别名 title", "link": "https://example.com"},
            "裸字符串",
            {"snippet": "没有 title 也没有 url"},
        ]
    )

    assert compact == [
        {"title": "Python 文档", "url": "https://docs.python.org"},
        {"title": "别名 title", "url": "https://example.com"},
        {"title": "裸字符串", "url": ""},
    ]


# --------------------------------------------------------------------------- #
# 文档读取
# --------------------------------------------------------------------------- #
def test_read_notepad_and_history_when_missing(runtime: RuntimeState) -> None:
    notepad = read_notepad(runtime)
    history = read_history_summary(runtime)

    assert notepad == {"path": NOTEPAD_FILENAME, "exists": False, "content": ""}
    assert history == {"path": HISTORY_SUMMARY_FILENAME, "exists": False, "content": ""}


def test_read_notepad_when_present(runtime: RuntimeState) -> None:
    notepad_append(runtime, "决策", "用纯函数实现 add")

    document = read_notepad(runtime)

    assert document["exists"] is True
    assert "用纯函数实现 add" in document["content"]


def test_append_history_summary_creates_and_appends(runtime: RuntimeState) -> None:
    assert append_history_summary(runtime, "第一段历史", heading="Round 1") == HISTORY_SUMMARY_FILENAME
    append_history_summary(runtime, "第二段历史", heading="Round 2")

    text = (runtime.workspace / HISTORY_SUMMARY_FILENAME).read_text(encoding="utf-8")

    assert text.startswith("# HISTORY SUMMARY")
    assert "第一段历史" in text and "第二段历史" in text
    assert "## Round 1" in text and "## Round 2" in text
    assert read_history_summary(runtime)["exists"] is True


def test_append_history_summary_rejects_empty(runtime: RuntimeState) -> None:
    with pytest.raises(ValueError):
        append_history_summary(runtime, "   ")


def test_append_history_summary_deduplicates_entry_id(runtime: RuntimeState) -> None:
    append_history_summary(
        runtime,
        "同一个任务的结算摘要",
        heading="Task task-1",
        entry_id="task-1",
    )
    append_history_summary(
        runtime,
        "恢复执行时不应重复追加",
        heading="Task task-1",
        entry_id="task-1",
    )

    text = (runtime.workspace / HISTORY_SUMMARY_FILENAME).read_text(encoding="utf-8")

    assert text.count("<!-- history-entry:task-1 -->") == 1
    assert "同一个任务的结算摘要" in text
    assert "恢复执行时不应重复追加" not in text


def test_append_history_summary_trims_when_too_large(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path / "ws", max_read_bytes=500)
    append_history_summary(runtime, "旧" * 400)
    assert not (runtime.workspace / HISTORY_SUMMARY_FILENAME).read_text(
        encoding="utf-8"
    ).startswith("…（旧历史已裁剪）")

    # 再追加一大段就会超过 500，触发"只留最新"的裁剪
    append_history_summary(runtime, "新" * 300, heading="Latest")

    text = (runtime.workspace / HISTORY_SUMMARY_FILENAME).read_text(encoding="utf-8")

    assert len(text) <= 500
    assert text.startswith("…（旧历史已裁剪）")
    assert "新" * 100 in text  # 最新的历史保住了
    assert "## Latest" in text


def test_summarize_task_for_history_uses_structured_result(state: dict[str, Any]) -> None:
    state.update(
        {
            "passed": False,
            "attempts": 2,
            "max_attempts": 3,
            "last_actor_summary": "修改了 utils.py，但测试失败",
            "last_error": "AssertionError: add(1, 2) != 3",
            "next_instruction": "修正 add 的返回值",
            "verification_results": [
                {"command": "pytest", "ok": False, "exit_code": 1},
            ],
        }
    )

    summary = summarize_task_for_history(state)

    assert "- Outcome: FAILED" in summary
    assert "- Attempts: 2/3" in summary
    assert "修改了 utils.py，但测试失败" in summary
    assert "- Verification commands: 0/1 passed" in summary
    assert "AssertionError" in summary
    assert "修正 add 的返回值" in summary


# --------------------------------------------------------------------------- #
# 三层组装
# --------------------------------------------------------------------------- #
def test_build_layered_memory_structure(state: dict[str, Any]) -> None:
    memory = build_layered_memory(state, node="planner")

    assert set(memory) == {"rules", "working_memory", "history_summary_store"}
    assert memory["rules"] == RULES_LAYER
    assert memory["rules"] is not RULES_LAYER  # 拷贝，外部改不动全局

    working = memory["working_memory"]
    assert working["node"] == "planner"
    assert working["task"] == state["task"]
    assert working["plan_summary"] == "先实现 add，再补测试"
    assert working["todos"] == state["todos"]
    assert working["acceptance_criteria"] == ["add(1, 2) == 3"]
    assert working["verification_commands"] == ["python -m pytest -q"]
    assert working["attempts"] == 1
    assert working["max_attempts"] == 3
    assert working["research_notes"] == ""
    assert working["sources"] == []
    assert working["agent_handoffs"] == []

    store = memory["history_summary_store"]
    assert store["history_path"] == HISTORY_SUMMARY_FILENAME
    assert store["history_exists"] is False
    assert store["notepad_path"] == NOTEPAD_FILENAME
    assert store["notepad_exists"] is False
    assert store["todo_path"] == TODO_FILENAME


def test_build_layered_memory_clips_long_fields(state: dict[str, Any]) -> None:
    state["research_notes"] = "n" * 5000
    state["last_error"] = "e" * 5000
    state["code_agent_summary"] = "c" * 5000
    state["verifier_summary"] = "v" * 5000

    working = build_layered_memory(state)["working_memory"]

    assert len(working["research_notes"]) <= NOTES_LIMIT
    assert working["research_notes"].endswith("...")
    assert len(working["last_error"]) <= LAST_ERROR_LIMIT
    assert len(working["code_agent_summary"]) <= 1000
    assert len(working["verifier_summary"]) <= 1000


def test_build_layered_memory_includes_notepad_and_history(
    state: dict[str, Any], runtime: RuntimeState
) -> None:
    notepad_append(runtime, "重要文件", "utils.py")
    append_history_summary(runtime, "上一轮把 add 写错了")
    (runtime.workspace / TODO_FILENAME).write_text("# TODO\n- [x] t1\n", encoding="utf-8")

    store = build_layered_memory(state)["history_summary_store"]

    assert store["notepad_exists"] is True
    assert "utils.py" in store["notepad"]
    assert store["history_exists"] is True
    assert "上一轮把 add 写错了" in store["history_summary"]


def test_history_summary_is_clipped(state: dict[str, Any], runtime: RuntimeState) -> None:
    append_history_summary(runtime, "h" * 6000)

    store = build_layered_memory(state)["history_summary_store"]

    assert len(store["history_summary"]) <= HISTORY_LIMIT
    assert len(store["notepad"]) <= NOTEPAD_LIMIT


def test_session_fields_come_from_runtime_metadata(
    state: dict[str, Any], runtime: RuntimeState
) -> None:
    runtime.metadata["session_id"] = "s-42"
    runtime.metadata["turn"] = 3

    working = build_layered_memory(state)["working_memory"]

    assert working["session_id"] == "s-42"
    assert working["session_turn"] == 3


def test_session_fields_tolerate_garbage(runtime: RuntimeState) -> None:
    runtime.metadata["session_turn"] = "not-a-number"

    working = build_layered_memory(runtime)["working_memory"]

    assert working["session_turn"] == 0
    assert working["node"] == "graph"


def test_build_layered_memory_accepts_runtime_state(runtime: RuntimeState) -> None:
    memory = build_layered_memory(runtime, node="codeAgent")

    assert memory["working_memory"]["node"] == "codeAgent"
    assert memory["working_memory"]["task"] == ""


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def test_format_layered_memory_for_prompt_is_json(state: dict[str, Any]) -> None:
    memory = build_layered_memory(state, node="verifier")

    text = format_layered_memory_for_prompt(memory)
    restored = json.loads(text)

    assert restored == json.loads(json.dumps(memory, ensure_ascii=False, default=str))
    assert restored["working_memory"]["node"] == "verifier"
    assert "Do not expose memory write tools" in text


def test_rules_layer_content() -> None:
    assert RULES_LAYER["scope"] == "workspace"
    assert RULES_LAYER["storage"] == "internal"
    rules = RULES_LAYER["rules"]
    assert len(rules) == 5
    assert any("HISTORY_SUMMARY.md" in rule for rule in rules)
    assert any("Do not expose memory write tools" in rule for rule in rules)
