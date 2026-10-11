"""bash."""

from __future__ import annotations

import subprocess

from .registry import Tool, ToolContext, params

DEFAULT_TIMEOUT = 120


def bash(ctx: ToolContext, args: dict) -> str:
    timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
    try:
        proc = subprocess.run(
            args["command"],
            shell=True,
            cwd=ctx.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    out = (proc.stdout + proc.stderr).rstrip()
    return f"exit code {proc.returncode}\n{out}" if out else f"exit code {proc.returncode} (no output)"


TOOLS = [
    Tool(
        "bash",
        "Run a shell command in the project root. Non-interactive: no stdin. Use for tests, builds, git, etc.",
        params(
            ["command"],
            command={"type": "string", "description": "The command to run"},
            timeout={"type": "integer", "description": f"Seconds before it is killed (default {DEFAULT_TIMEOUT})"},
        ),
        bash,
    ),
]
