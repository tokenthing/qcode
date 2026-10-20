"""System prompt and canned prompts."""

from __future__ import annotations

import datetime
import platform
from pathlib import Path

SYSTEM_TEMPLATE = """\
You are qcode, a coding agent working in the user's terminal. You help with software tasks by \
reading, searching and editing files and running shell commands through your tools.

Environment:
- Project root (all paths are relative to it): {root}
- OS: {os}
- Date: {date}

How to work:
- Look before you change: use list_dir, glob, grep and read_file to find the relevant code first.
- Showing code in your reply does NOT change any file. To change a file you must call a tool.
- Never describe a file's contents unless you read it with read_file in this conversation.
- To change an existing file, use edit_file with an exact, unique old_string copied from read_file output \
(without the line-number prefix). Use write_file for new files; to rewrite an existing file completely, \
read it first.
- After changing code, run the relevant tests or the program with bash when it makes sense.
- Make one tool call at a time when later steps depend on the result.
- Keep answers short. Use Markdown. Do not repeat file contents back unless asked.
- If a tool returns an error, read it and fix your call instead of repeating it.
- If a tool call is denied by the user, stop and ask what they want instead.
- Never ask the user for permission or confirmation to use a tool. Just make the call; qcode asks the user itself
  when approval is needed.
- If you cannot call tools natively, write exactly one call as:
<tool_call>{{"name": "tool_name", "arguments": {{...}}}}</tool_call>
{mode}{memory}"""

PLAN_MODE_NOTE = """
PLAN MODE IS ON: you may only use read-only tools (read_file, list_dir, glob, grep). Investigate, then \
reply with a clear numbered plan of the changes you would make. Don't try to edit files or run commands.
"""

CONTINUE_NUDGE = (
    "You said you would take an action but didn't call a tool, so nothing happened. If you still want to do it, "
    "call the tool now. If you're finished, reply with a one-line summary."
)

_NUDGE_EXAMPLES = {
    "web_fetch": '{"name": "web_fetch", "arguments": {"url": "https://..."}}',
    "web_search": '{"name": "web_search", "arguments": {"query": "..."}}',
    "weather": '{"name": "weather", "arguments": {"location": "..."}}',
    "write_file": '{"name": "write_file", "arguments": {"path": "...", "content": "..."}}',
    "edit_file": '{"name": "edit_file", "arguments": {"path": "...", "old_string": "...", "new_string": "..."}}',
    "read_file": '{"name": "read_file", "arguments": {"path": "..."}}',
    "bash": '{"name": "bash", "arguments": {"command": "..."}}',
}


def build_nudge(reason: str, tool: str = "") -> str:
    """The reminder sent when a reply talked about acting but made no tool call."""
    lead = {
        "named": f"You described a {tool} call but didn't make it, so nothing happened.",
        "permission": "Don't ask for permission or confirmation; qcode already asks the user when approval is needed.",
        "claimed": "You said you changed something, but you didn't call any tool, so no file was changed.",
        "no_access": "You DO have web tools for this message (web_search, web_fetch, weather). Use them instead of "
                     "saying you can't; if a name is unfamiliar, search for it.",
        "announced": "You said you would take an action but didn't call a tool, so nothing happened.",
    }.get(reason, "You didn't call a tool, so nothing happened.")
    example = _NUDGE_EXAMPLES.get(tool, '{"name": "<tool>", "arguments": {...}}')
    return (f"{lead} If it's still needed, reply now with only the tool call, like: {example}. "
            "If you're actually finished, give the user the final answer.")

INIT_PROMPT = """\
Explore this repository (list_dir, glob, read the README and build files) and write a concise QCODE.md \
at the project root for future sessions. Include: what the project is, how to build/run/test it, the main \
directories and what lives in them, and any conventions you notice. Keep it under 60 lines."""

COMPACT_PROMPT = """\
Summarize the conversation transcript below so the work can continue with less context. Keep: the user's \
goals and instructions, decisions made, files touched and what changed, important facts discovered, and \
anything still unfinished. Be concise; use bullet points.

TRANSCRIPT:
{transcript}"""


def build_system_prompt(root: Path, memory: str, mode: str = "ask") -> str:
    return SYSTEM_TEMPLATE.format(
        root=root,
        os=f"{platform.system()} {platform.release()}",
        date=datetime.date.today().isoformat(),
        mode=PLAN_MODE_NOTE if mode == "plan" else "",
        memory=f"\n# Memory\n{memory}\n" if memory else "",
    )
