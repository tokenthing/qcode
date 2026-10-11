"""Tool definitions shared by every tool module."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".gradle", ".idea"}


class ToolError(Exception):
    """An error the model should see and recover from."""


@dataclass
class ToolContext:
    root: Path

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not p.is_relative_to(self.root):
            raise ToolError(f"{path} is outside the project root {self.root}")
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


def params(required: list[str], **props: dict) -> dict:
    return {"type": "object", "properties": props, "required": required}
