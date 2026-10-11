"""Fallback parser for tool calls the model wrote as text instead of native tool_calls.

Qwen2.5-Coder sometimes emits `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`
or a fenced JSON block in its content rather than a structured call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
OPEN_TAG_RE = re.compile(r"<tool_call>(?!.*</tool_call>).*\Z", re.S)
FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
# A reply that is (so far) only a JSON tool call, possibly fenced and still streaming.
BARE_START_RE = re.compile(r'\A\s*(?:```(?:json)?\s*)?\{\s*"(?:name|function)"')
# The first few streamed characters of such a call, before it's recognizable: `{`, `{ "na`, "```js"…
PARTIAL_START_RE = re.compile(r'\A\s*(?:`{1,3}(?:j(?:s(?:o(?:n)?)?)?)?\s*)?(?:\{\s*(?:"[a-z]{0,8})?)?\Z')


@dataclass
class ParsedCall:
    name: str
    arguments: dict


def _decode(raw: str, known: set[str]) -> ParsedCall | None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if "function" in obj and isinstance(obj["function"], dict):
        obj = obj["function"]
    name = obj.get("name")
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if name not in known or not isinstance(args, dict):
        return None
    return ParsedCall(name, args)


def extract_text_tool_calls(text: str, known: set[str]) -> tuple[list[ParsedCall], str]:
    """Return (calls, remaining prose) for tool calls written as text."""
    if not text:
        return [], text
    for pattern in (TAG_RE, FENCE_RE):
        calls: list[ParsedCall] = []

        def take(m: re.Match) -> str:
            call = _decode(m.group(1), known)
            if call:
                calls.append(call)
                return ""
            return m.group(0)

        remaining = pattern.sub(take, text)
        if calls:
            return calls, remaining.strip()
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        call = _decode(stripped, known)
        if call:
            return [call], ""
    return [], text


def parse_text_tool_calls(text: str, known: set[str]) -> list[ParsedCall]:
    return extract_text_tool_calls(text, known)[0]


def strip_tool_markup(text: str) -> str:
    """Hide tool-call text (tagged, fenced or bare JSON, complete or still streaming) for display."""
    text = OPEN_TAG_RE.sub("", TAG_RE.sub("", text)).strip()
    return "" if BARE_START_RE.match(text) or PARTIAL_START_RE.match(text) else text
