"""read_file, write_file, edit_file."""

from __future__ import annotations

from ..diffview import unified_diff
from .registry import Tool, ToolContext, ToolError, params

DEFAULT_READ_LIMIT = 250


def _validate(p, text: str) -> str:
    """A note for the model when it just broke a structured file, so it fixes it before finishing."""
    if p.suffix.lower() == ".json":
        import json
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            return (f"\nWARNING: {p.name} is no longer valid JSON ({e.msg} at line {e.lineno}, column {e.colno}). "
                    "Read it and fix it now.")
    return ""


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
    ctx.mark_seen(ctx.resolve(args["path"]))
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


def _check_overwrite(ctx: ToolContext, args: dict):
    p = ctx.resolve(args["path"])
    if ctx.guard_overwrite and p.is_file() and str(p) not in ctx.seen and p.stat().st_size > 0:
        raise ToolError(
            f"{args['path']} already exists and you haven't read it in this conversation, so overwriting it could "
            "destroy work. Call read_file on it first. To change only part of it, use edit_file instead."
        )
    return p


def _write_preview(ctx: ToolContext, args: dict) -> str:
    p = _check_overwrite(ctx, args)
    old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    return unified_diff(old, args["content"], ctx.rel(p))


def write_file(ctx: ToolContext, args: dict) -> str:
    p = _check_overwrite(ctx, args)
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    ctx.mark_seen(p)
    n = len(args["content"].splitlines())
    return f"{'Overwrote' if existed else 'Created'} {ctx.rel(p)} ({n} lines)" + _validate(p, args["content"])


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _match_ignoring_indent(text: str, old_s: str, new_s: str) -> str | None:
    """Small models often get indentation wrong (4 spaces where the file has 2). If old_string matches exactly one
    place once leading whitespace is ignored, apply the edit there, shifting new_string to the file's indentation."""
    old_lines = old_s.strip("\n").split("\n")
    if not any(l.strip() for l in old_lines):
        return None
    lines = text.split("\n")
    want = [l.strip() for l in old_lines]
    hits = [i for i in range(len(lines) - len(want) + 1) if [l.strip() for l in lines[i:i + len(want)]] == want]
    if len(hits) != 1:
        return None
    i = hits[0]
    first = next(k for k, l in enumerate(old_lines) if l.strip())
    have, given = _indent(lines[i + first]), _indent(old_lines[first])
    shifted = []
    for line in new_s.strip("\n").split("\n"):
        if line.startswith(given):
            shifted.append(have + line[len(given):])
        elif line.strip():
            shifted.append(have + line.lstrip(" \t"))
        else:
            shifted.append(line)
    return "\n".join(lines[:i] + shifted + lines[i + len(want):])


def _closest_line_hint(text: str, old_s: str) -> str:
    target = next((l.strip() for l in old_s.split("\n") if l.strip()), "")
    if not target:
        return ""
    import difflib
    lines = text.split("\n")
    best = difflib.get_close_matches(target, [l.strip() for l in lines], n=1, cutoff=0.6)
    if not best:
        return ""
    n = next(k for k, l in enumerate(lines) if l.strip() == best[0])
    return f" Closest line is {n + 1}: {lines[n]!r}"


def _apply_edit(ctx: ToolContext, args: dict) -> tuple[str, str]:
    old_s, new_s = args["old_string"], args["new_string"]
    if old_s == new_s:
        raise ToolError("old_string and new_string are identical")
    if not ctx.resolve(args["path"]).exists():
        raise ToolError(f"{args['path']} does not exist. To create a new file, call write_file with path and content.")
    text = _read_text(ctx, args["path"])
    count = text.count(old_s)
    if count == 0:
        fixed = _match_ignoring_indent(text, old_s, new_s)
        if fixed is not None:
            return text, fixed
        raise ToolError("old_string not found in file; read the file and copy the text exactly (including indentation)."
                        + _closest_line_hint(text, old_s))
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
    ctx.mark_seen(p)
    return f"Edited {ctx.rel(p)}" + _validate(p, new)


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
