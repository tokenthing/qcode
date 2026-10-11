"""read_file, write_file, edit_file."""

from __future__ import annotations

from ..diffview import unified_diff
from .registry import Tool, ToolContext, ToolError, params

DEFAULT_READ_LIMIT = 250


def _read_text(ctx: ToolContext, path: str) -> str:
    p = ctx.resolve(path)
    if not p.exists():
        raise ToolError(f"{path} does not exist")
    if p.is_dir():
        raise ToolError(f"{path} is a directory; use list_dir")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not a UTF-8 text file") from None


def read_file(ctx: ToolContext, args: dict) -> str:
    text = _read_text(ctx, args["path"])
    lines = text.splitlines()
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, int(args.get("limit") or DEFAULT_READ_LIMIT))
    chunk = lines[offset - 1 : offset - 1 + limit]
    if not lines:
        return "(empty file)"
    body = "\n".join(f"{n:>5}\t{line}" for n, line in enumerate(chunk, start=offset))
    end = offset + len(chunk) - 1
    if offset > 1 or end < len(lines):
        body += f"\n(showing lines {offset}-{end} of {len(lines)}; pass offset to read more)"
    return body


def _write_preview(ctx: ToolContext, args: dict) -> str:
    p = ctx.resolve(args["path"])
    old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    return unified_diff(old, args["content"], ctx.rel(p))


def write_file(ctx: ToolContext, args: dict) -> str:
    p = ctx.resolve(args["path"])
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    n = len(args["content"].splitlines())
    return f"{'Overwrote' if existed else 'Created'} {ctx.rel(p)} ({n} lines)"


def _apply_edit(ctx: ToolContext, args: dict) -> tuple[str, str]:
    old_s, new_s = args["old_string"], args["new_string"]
    if old_s == new_s:
        raise ToolError("old_string and new_string are identical")
    text = _read_text(ctx, args["path"])
    count = text.count(old_s)
    if count == 0:
        raise ToolError("old_string not found in file; read the file and copy the text exactly (including indentation)")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_string matches {count} places; include more surrounding lines to make it unique, or set replace_all")
    return text, text.replace(old_s, new_s) if args.get("replace_all") else text.replace(old_s, new_s, 1)


def _edit_preview(ctx: ToolContext, args: dict) -> str:
    old, new = _apply_edit(ctx, args)
    return unified_diff(old, new, ctx.rel(ctx.resolve(args["path"])))


def edit_file(ctx: ToolContext, args: dict) -> str:
    _, new = _apply_edit(ctx, args)
    p = ctx.resolve(args["path"])
    p.write_text(new, encoding="utf-8")
    return f"Edited {ctx.rel(p)}"


TOOLS = [
    Tool(
        "read_file",
        "Read a text file. Returns numbered lines. Large files are paged: use offset/limit.",
        params(
            ["path"],
            path={"type": "string", "description": "File path relative to the project root"},
            offset={"type": "integer", "description": "1-based line to start at (default 1)"},
            limit={"type": "integer", "description": f"Max lines to return (default {DEFAULT_READ_LIMIT})"},
        ),
        read_file,
        read_only=True,
    ),
    Tool(
        "write_file",
        "Create a new file or completely overwrite an existing one. Prefer edit_file for changes to existing files.",
        params(
            ["path", "content"],
            path={"type": "string", "description": "File path relative to the project root"},
            content={"type": "string", "description": "Full file contents"},
        ),
        write_file,
        preview=_write_preview,
    ),
    Tool(
        "edit_file",
        "Replace an exact snippet in a file. old_string must match the file exactly (including indentation) and be unique unless replace_all is true.",
        params(
            ["path", "old_string", "new_string"],
            path={"type": "string", "description": "File path relative to the project root"},
            old_string={"type": "string", "description": "Exact text to replace"},
            new_string={"type": "string", "description": "Replacement text"},
            replace_all={"type": "boolean", "description": "Replace every occurrence (default false)"},
        ),
        edit_file,
        preview=_edit_preview,
    ),
]
