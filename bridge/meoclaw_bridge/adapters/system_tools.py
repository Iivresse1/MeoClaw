"""系统工具集 + HITL 审批包装。高危动作执行前经 request_approval 挂起等 UI 确认。"""
import time
import webbrowser
from pathlib import Path

from meoclaw.core.paths import WorkspaceError, resolve_in_workspace


def build_system_tools(workspace: Path) -> list:
    """Build system tools; approval is enforced by HumanInTheLoopMiddleware."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class UrlArgs(BaseModel):
        url: str = Field(description="要打开的 http(s) 链接")

    class FileArgs(BaseModel):
        file_path: str = Field(description="会话工作区内的相对路径")

    class TextArgs(BaseModel):
        text: str = Field(description="要写入剪贴板的文本")

    def take_screenshot() -> str:
        """截取全屏，保存到当前会话工作区，返回文件路径。"""
        from PIL import ImageGrab

        out = Path(workspace) / f"screenshot_{int(time.time())}.png"
        ImageGrab.grab().save(out)
        return f"已截图：{out.name}（位于会话工作区）"

    def clipboard_read() -> str:
        """读取系统剪贴板文本。"""
        import pyperclip

        return pyperclip.paste() or "(剪贴板为空)"

    def clipboard_write(text: str) -> str:
        """写入系统剪贴板（需用户确认）。"""
        import pyperclip

        pyperclip.copy(text)
        return "已写入剪贴板"

    def open_url(url: str) -> str:
        """用默认浏览器打开链接（需用户确认）。"""
        if not url.lower().startswith(("http://", "https://")):
            return "仅允许 http(s) 链接"
        webbrowser.open(url)
        return "已在默认浏览器打开"

    def open_local_file(file_path: str) -> str:
        """打开会话工作区内的文件（需确认；越界路径直接拒绝）。"""
        try:
            real = resolve_in_workspace(workspace, file_path)
        except WorkspaceError:
            return "路径越界：只允许打开会话工作区内的文件"
        if not real.exists():
            return f"文件不存在：{file_path}"
        import os

        os.startfile(str(real))  # Windows
        return f"已打开 {real.name}"

    return [
        StructuredTool.from_function(take_screenshot, name="TakeScreenshot",
                                      description="截取全屏保存到会话工作区并返回文件名"),
        StructuredTool.from_function(clipboard_read, name="ClipboardRead",
                                     description="读取系统剪贴板文本"),
        StructuredTool.from_function(clipboard_write, name="ClipboardWrite",
                                     description="写入系统剪贴板文本（需用户批准）", args_schema=TextArgs),
        StructuredTool.from_function(open_url, name="OpenUrl",
                                     description="默认浏览器打开 http(s) 链接（需用户批准）", args_schema=UrlArgs),
        StructuredTool.from_function(open_local_file, name="OpenWorkspaceFile",
                                     description="打开会话工作区内的文件（需用户批准，越界拒绝）", args_schema=FileArgs),
    ]
