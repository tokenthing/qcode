"""list_dir, glob, grep."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess

from .registry import SKIP_DIRS, Tool, ToolContext, ToolError, params

MAX_RESULTS = 200


def list_dir(ctx: ToolContext, args: dict) -> str:
    p = ctx.resolve(args.get("path") or ".")
    if not p.is_dir():
        raise ToolError(f"{args.get('path')} is not a directory")
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries if e.name not in SKIP_DIRS]
    if len(lines) > MAX_RESULTS:
        lines = lines[:MAX_RESULTS] + [f"... ({len(entries) - MAX_RESULTS} more)"]
    return "\n".join(lines) or "(empty directory)"


def _walk_files(ctx: ToolContext, start):
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def glob(ctx: ToolContext, args: dict) -> str:
    pattern = args["pattern"]
    matches = []
    for f in _walk_files(ctx, ctx.root):
        rel = os.path.relpath(f, ctx.root).replace(os.sep, "/")
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(f), pattern):
            matches.append(rel)
            if len(matches) >= MAX_RESULTS:
                matches.append(f"... (stopped at {MAX_RESULTS} results)")
                break
    return "\n".join(matches) or "No files matched"


def grep(ctx: ToolContext, args: dict) -> str:
    pattern = args["pattern"]
    start = ctx.resolve(args.get("path") or ".")
    file_glob = args.get("glob")
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}") from None

    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--color", "never", "--path-separator", "/", "-m", "50"]
        if file_glob:
            cmd += ["-g", file_glob]
        cmd += ["-e", pattern, ctx.rel(start)]
        out = subprocess.run(cmd, cwd=ctx.root, capture_output=True, text=True, timeout=60).stdout
        lines = out.splitlines()
    else:
        lines = []
        files = [str(start)] if start.is_file() else _walk_files(ctx, start)
        for f in files:
            if file_glob and not fnmatch.fnmatch(os.path.basename(f), file_glob):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if regex.search(line):
                            lines.append(f"{os.path.relpath(f, ctx.root).replace(os.sep, '/')}:{n}:{line.rstrip()}")
            except (UnicodeDecodeError, OSError):
                continue
            if len(lines) >= MAX_RESULTS:
                break

    if len(lines) > MAX_RESULTS:
        lines = lines[:MAX_RESULTS] + [f"... (stopped at {MAX_RESULTS} results)"]
    return "\n".join(lines) or "No matches"


TOOLS = [
    Tool(
        "list_dir",
        "List files and folders in a directory (folders end with /).",
        params([], path={"type": "string", "description": "Directory relative to the project root (default .)"}),
        list_dir,
        read_only=True,
    ),
    Tool(
        "glob",
        "Find files by name pattern, e.g. '*.py' or 'src/**/*.ts'.",
        params(["pattern"], pattern={"type": "string", "description": "Glob pattern"}),
        glob,
        read_only=True,
    ),
    Tool(
        "grep",
        "Search file contents with a regular expression. Returns path:line:text.",
        params(
            ["pattern"],
            pattern={"type": "string", "description": "Regular expression"},
            path={"type": "string", "description": "File or directory to search (default .)"},
            glob={"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
        ),
        grep,
        read_only=True,
    ),
]
