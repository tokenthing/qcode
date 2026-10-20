from . import files, search, shell, web
from .registry import Tool, ToolContext, ToolError, normalize_args

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in (*files.TOOLS, *search.TOOLS, *shell.TOOLS)}
# Only offered on turns where the user asked for something online (see agent.wants_web).
WEB_TOOLS: dict[str, Tool] = {t.name: t for t in web.TOOLS}

__all__ = ["ALL_TOOLS", "WEB_TOOLS", "Tool", "ToolContext", "ToolError", "normalize_args"]
