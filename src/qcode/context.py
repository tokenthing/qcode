"""Context-window bookkeeping: token estimates, output truncation, transcripts for compaction."""

from __future__ import annotations

import json

CHARS_PER_TOKEN = 3.5
COMPACT_THRESHOLD = 0.8


def estimate_tokens(messages: list[dict]) -> int:
    return int(sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) / CHARS_PER_TOKEN)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 3 // 4], text[-(limit // 4) :]
    return f"{head}\n... [{len(text) - limit} characters truncated; narrow the query or read by line range] ...\n{tail}"


def transcript(messages: list[dict], max_chars: int) -> str:
    lines = []
    for m in messages:
        role = m["role"]
        if m.get("content"):
            lines.append(f"{role.upper()}: {m['content']}")
        for tc in m.get("tool_calls") or []:
            fn = tc["function"]
            lines.append(f"ASSISTANT called {fn['name']}({fn['arguments']})")
    text = "\n\n".join(lines)
    return text if len(text) <= max_chars else "...\n" + text[-max_chars:]
