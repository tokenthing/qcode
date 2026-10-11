"""Unified diffs shown before file changes are approved."""

from __future__ import annotations

import difflib


def unified_diff(old: str, new: str, path: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
