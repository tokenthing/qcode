from . import files, search, shell
from .registry import Tool, ToolContext, ToolError

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in (*files.TOOLS, *search.TOOLS, *shell.TOOLS)}

__all__ = ["ALL_TOOLS", "Tool", "ToolContext", "ToolError"]
