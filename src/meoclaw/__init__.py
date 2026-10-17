"""MeoClaw —— 工作区受限的编码智能体。

包根只暴露轻量对象，避免 `import meoclaw` 时就把 langchain 拉进来。
"""

from meoclaw.core.paths import WorkspaceError, resolve_in_workspace, resolve_workspace
from meoclaw.core.state import RuntimeState

__version__ = "0.1.0"

__all__ = [
    "RuntimeState",
    "WorkspaceError",
    "resolve_in_workspace",
    "resolve_workspace",
    "__version__",
]
