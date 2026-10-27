"""Workspace shell sandbox policy.

MeoClaw uses Docker for executable isolation.  Without Docker the caller must
fail closed rather than silently falling back to host shell execution.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from meoclaw.core.state import RuntimeState

__all__ = [
    "SandboxUnavailableError",
    "build_sandbox_shell_middleware",
    "build_sandboxed_command",
    "remove_sandbox_container",
    "sandbox_mode",
]


class SandboxUnavailableError(RuntimeError):
    pass


def sandbox_mode(runtime: RuntimeState) -> str:
    return str(
        (runtime.metadata or {}).get("sandbox")
        or os.environ.get("MEOCLAW_SANDBOX", "host")
    ).lower()


def _require_docker() -> None:
    if shutil.which("docker") is None:
        raise SandboxUnavailableError(
            "Docker 未安装，工作区 shell 已拒绝执行；请安装 Docker Desktop"
        )


def _workspace_docker_policy(container_name: str = "") -> Any:
    """Build the one Docker policy shared by actor shell and verifier commands."""
    _require_docker()
    from langchain.agents.middleware import DockerExecutionPolicy

    class WorkspaceDockerExecutionPolicy(DockerExecutionPolicy):
        """Mount the host workspace at the Linux container's ``/workspace``."""

        def _build_command(self, workspace, env, command):  # type: ignore[no-untyped-def]
            binary = self._resolve_binary()
            full_command = [binary, "run", "-i"]
            if self.remove_container_on_exit:
                full_command.append("--rm")
            if not self.network_enabled:
                full_command.extend(["--network", "none"])
            if self.memory_bytes is not None:
                full_command.extend(["--memory", str(self.memory_bytes)])

            host_workspace = workspace.resolve()
            full_command.extend(["-v", f"{host_workspace}:/workspace", "-w", "/workspace"])

            if self.read_only_rootfs:
                full_command.append("--read-only")
            for key, value in env.items():
                full_command.extend(["-e", f"{key}={value}"])
            if self.cpus is not None:
                full_command.extend(["--cpus", self.cpus])
            if self.user is not None:
                full_command.extend(["--user", self.user])
            if self.extra_run_args:
                full_command.extend(self.extra_run_args)
            full_command.append(self.image)
            full_command.extend(command)
            return full_command

        def build_command(
            self,
            workspace,  # type: ignore[no-untyped-def]
            env,  # type: ignore[no-untyped-def]
            command,  # type: ignore[no-untyped-def]
        ) -> list[str]:
            return self._build_command(workspace, env, command)

    image = os.environ.get("MEOCLAW_SANDBOX_IMAGE", "meoclaw-sandbox:latest")
    network = os.environ.get("MEOCLAW_SANDBOX_NETWORK", "0") == "1"
    extra_run_args = ("--name", container_name) if container_name else ()
    policy = WorkspaceDockerExecutionPolicy(
        image=image,
        network_enabled=network,
        remove_container_on_exit=True,
        read_only_rootfs=True,
        memory_bytes=2 * 1024 * 1024 * 1024,
        cpus="2",
        extra_run_args=extra_run_args,
    )
    return policy


def build_sandboxed_command(
    runtime: RuntimeState,
    command: str,
    *,
    container_name: str = "",
) -> list[str]:
    """Build a one-shot Docker command for a verifier check."""
    policy = _workspace_docker_policy(container_name=container_name)
    return policy.build_command(
        runtime.workspace,
        {},
        ("/bin/bash", "-lc", command),
    )


def remove_sandbox_container(container_name: str) -> bool:
    """Best-effort removal after a timeout or interrupted command."""
    if not container_name:
        return False
    binary = shutil.which("docker")
    if binary is None:
        return False
    try:
        result = subprocess.run(
            [binary, "rm", "-f", container_name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def build_sandbox_shell_middleware(runtime: RuntimeState) -> Any:
    """Return a ``ShellToolMiddleware`` backed by a restricted Docker container."""
    from langchain.agents.middleware import ShellToolMiddleware

    class WindowsSafeShellToolMiddleware(ShellToolMiddleware):
        """Avoid CRLF translation on the host-to-container shell pipe."""

        def _create_resources(self):  # type: ignore[no-untyped-def]
            resources = super()._create_resources()
            stdin = getattr(resources.session, "_stdin", None)
            if os.name == "nt" and stdin is not None:
                stdin.reconfigure(newline="\n")
            return resources

    return WindowsSafeShellToolMiddleware(
        workspace_root=runtime.workspace,
        execution_policy=_workspace_docker_policy(),
        tool_name="BashTool",
        tool_description=(
            "在 Docker 隔离的工作区中执行非交互 shell 命令。"
            "默认无网络；工作区是唯一持久可写挂载。"
        ),
    )
