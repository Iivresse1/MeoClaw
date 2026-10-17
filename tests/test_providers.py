"""providers 层测试：.env / 环境变量读取（构造模型不发起网络请求）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from meoclaw.providers import openai_provider
from meoclaw.providers.openai_provider import MissingAPIKeyError, create_model


@pytest.fixture()
def no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽真实 .env 文件与本机可能已有的网关配置，保证测试与机器环境无关。"""
    monkeypatch.setattr(openai_provider, "_candidate_env_files", lambda: [])
    for name in ("OPENAI_BASE_URL", "BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)


def test_create_model_requires_api_key(
    monkeypatch: pytest.MonkeyPatch, no_env_file: None
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError):
        create_model()


def test_create_model_reads_model_from_env(
    monkeypatch: pytest.MonkeyPatch, no_env_file: None
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    assert create_model().model_name == "gpt-test"


def test_create_model_accepts_legacy_env_names(
    monkeypatch: pytest.MonkeyPatch, no_env_file: None
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("API_KEY", "legacy-key")
    monkeypatch.setenv("MODEL", "legacy-model")
    assert create_model().model_name == "legacy-model"


def test_env_search_stops_at_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(project / "sub")

    names = [str(candidate).lower() for candidate in openai_provider._candidate_env_files()]
    assert str(project / ".env").lower() in names
    # 不应继续上溯到 pytest 临时目录，更不会碰到用户主目录
    assert str(tmp_path / ".env").lower() not in names
