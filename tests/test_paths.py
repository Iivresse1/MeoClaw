"""core.paths / core.state 的路径安全测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from meoclaw.core.paths import (
    WorkspaceError,
    display_path,
    is_within,
    resolve_in_workspace,
    resolve_workspace,
)
from meoclaw.core.state import RuntimeState


def test_resolve_workspace_creates_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "ws"
    resolved = resolve_workspace(target)
    assert resolved.is_dir()
    assert resolved == target.resolve()


def test_resolve_workspace_rejects_file(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        resolve_workspace(file_path)


def test_resolve_workspace_rejects_blank() -> None:
    with pytest.raises(WorkspaceError):
        resolve_workspace("   ")


def test_resolve_in_workspace_normalizes_relative(tmp_path: Path) -> None:
    workspace = resolve_workspace(tmp_path / "ws")
    resolved = resolve_in_workspace(workspace, "sub/../b.txt")
    assert resolved == (workspace / "b.txt")


@pytest.mark.parametrize("bad", ["../outside.txt", "a/../../outside.txt"])
def test_resolve_in_workspace_blocks_parent_escape(tmp_path: Path, bad: str) -> None:
    workspace = resolve_workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(workspace, bad)


def test_resolve_in_workspace_blocks_absolute_outside(tmp_path: Path) -> None:
    workspace = resolve_workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(workspace, tmp_path / "outside.txt")


def test_resolve_in_workspace_must_exist(tmp_path: Path) -> None:
    workspace = resolve_workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(workspace, "missing.txt", must_exist=True)


def test_is_within(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert is_within(workspace, workspace)
    assert is_within(workspace, workspace / "a" / "b.txt")
    assert not is_within(workspace, tmp_path / "other.txt")


def test_display_path_falls_back_to_absolute(tmp_path: Path) -> None:
    workspace = resolve_workspace(tmp_path / "ws")
    assert display_path(workspace / "a.py", workspace) == "a.py"
    assert display_path(tmp_path / "other.py", workspace) == str((tmp_path / "other.py").resolve())


def test_runtime_state_normalizes_workspace(tmp_path: Path) -> None:
    state = RuntimeState(workspace=tmp_path / "ws")
    assert isinstance(state.workspace, Path)
    assert state.workspace.is_dir()
    assert state.root == state.workspace
    assert state.describe()["workspace"] == str(state.workspace)


def test_runtime_state_rejects_bad_budget(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RuntimeState(workspace=tmp_path / "ws", max_read_bytes=0)
