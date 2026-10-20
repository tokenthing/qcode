"""Tool definitions shared by every tool module."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".gradle", ".idea"}


class ToolError(Exception):
    """An error the model should see and recover from."""


@dataclass
class ToolContext:
    root: Path
    guard_overwrite: bool = True  # refuse write_file over an existing file that wasn't read or written this session
    seen: set[str] = field(default_factory=set)  # absolute paths read or written this session
    location: str = ""  # the user's saved location, for the weather tool

    def mark_seen(self, p: Path) -> None:
        self.seen.add(str(p))

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not p.is_relative_to(self.root):
            raise ToolError(
                f"{path} is outside the project root {self.root}. Use a path relative to the project root, "
                f"e.g. {p.name!r}."
            )
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[ToolContext, dict], str]
    read_only: bool = False
    # Returns a diff (or other text) to show the user before approval. May raise ToolError.
    preview: Callable[[ToolContext, dict], str | None] | None = None

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


# Names small models use instead of the real parameter names.
ARG_ALIASES: dict[str, list[str]] = {
    "path": ["file_path", "filepath", "filename", "file", "file_name", "target_file"],
    "content": ["contents", "new_string", "text", "code", "file_content", "data", "body"],
    "old_string": ["old_str", "old_text", "old", "search", "find", "original"],
    "new_string": ["new_str", "new_text", "new", "replace", "replacement"],
    "command": ["cmd", "script", "shell_command"],
    "pattern": ["regex", "query", "search_pattern", "glob_pattern"],
}


def clean_file_content(path: str, content: str) -> str:
    """Undo common small-model mistakes in write_file content:
    - the whole file wrapped in a Markdown code fence (```python … ```), sometimes left unclosed;
    - a stray JSON tail like `"}` left on its own last line;
    - double-escaped text: no real line breaks, only literal backslash-n sequences."""
    if not isinstance(content, str):
        return content
    text = content
    if "\n" not in text and text.count("\\n") >= 2:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    is_markdown = path.lower().endswith((".md", ".markdown", ".mdx"))
    lines = text.split("\n")
    if not is_markdown and lines and lines[0].strip().startswith("```") and len(lines) > 1:
        lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "```":
            lines.pop()
    while lines and lines[-1].strip() in ('"}', '"}}', '"}]', "'}"):  # leaked end of the JSON tool call
        lines.pop()
    cleaned = "\n".join(lines)
    if content.endswith("\n") and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned


def normalize_args(tool: "Tool", args: dict) -> dict:
    """Rename well-known wrong parameter names to the tool's real ones (only for parameters that are missing),
    and clean up write_file content that small models commonly mangle."""
    props = tool.parameters.get("properties", {})
    fixed = dict(args)
    for param in props:
        if param in fixed:
            continue
        for alias in ARG_ALIASES.get(param, []):
            if alias in fixed and alias not in props:
                fixed[param] = fixed.pop(alias)
                break
    if tool.name == "write_file" and isinstance(fixed.get("content"), str):
        fixed["content"] = clean_file_content(str(fixed.get("path", "")), fixed["content"])
    return fixed


def params(required: list[str], **props: dict) -> dict:
    return {"type": "object", "properties": props, "required": required}
