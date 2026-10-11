"""Project and global memory files (QCODE.md), injected into the system prompt."""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG_DIR

MEMORY_FILENAME = "QCODE.md"
MAX_MEMORY_CHARS = 6000


def global_memory_path() -> Path:
    return CONFIG_DIR / MEMORY_FILENAME


def project_memory_path(root: Path) -> Path:
    return root / MEMORY_FILENAME


def load_memory(root: Path) -> str:
    parts = []
    for label, path in (("Global notes", global_memory_path()), ("Project notes (QCODE.md)", project_memory_path(root))):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                parts.append(f"## {label}\n{text[:MAX_MEMORY_CHARS]}")
    return "\n\n".join(parts)
