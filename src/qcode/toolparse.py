"""Fallback parser for tool calls the model wrote as text instead of native tool_calls.

Small local models often put the call in their reply text: inside `<tool_call>` tags, in a
fenced JSON block, as the whole reply, or as a JSON object after a sentence of prose, e.g.
    I'll create it instead. {"name": "write_file", "parameters": {"path": ..., "content": ...}}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
OPEN_TAG_RE = re.compile(r"<tool_call>(?!.*</tool_call>).*\Z", re.S)
FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
# Where a JSON tool call starts inside text: {"name": …  or  {"function": …
CALL_START_RE = re.compile(r'\{\s*"(?:name|function)"\s*:')
# An unfinished fence left behind once a streaming call is cut off, e.g. "Sure.\n```json"
TRAILING_FENCE_RE = re.compile(r"```(?:json)?\s*\Z")
# The first few streamed characters of a call, before it's recognizable: `{`, `{ "na`, "```js"…
PARTIAL_START_RE = re.compile(r'\A\s*(?:`{1,3}(?:j(?:s(?:o(?:n)?)?)?)?\s*)?(?:\{\s*(?:"[a-z]{0,8})?)?\Z')

# strict=False: models often put raw newlines inside JSON strings (e.g. a whole HTML file).
_decoder = json.JSONDecoder(strict=False)


@dataclass
class ParsedCall:
    name: str
    arguments: dict


def _call_from_obj(obj, known: set[str]) -> ParsedCall | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    name = obj.get("name")
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args, strict=False)
        except json.JSONDecodeError:
            return None
    if name not in known or not isinstance(args, dict):
        return None
    return ParsedCall(name, args)


def _decode(raw: str, known: set[str]) -> ParsedCall | None:
    try:
        return _call_from_obj(json.loads(raw, strict=False), known)
    except json.JSONDecodeError:
        return None


def _embedded(text: str):
    """Yield (start, end, obj) for each JSON object that looks like a call; obj is None if it's unfinished."""
    pos = 0
    while True:
        m = CALL_START_RE.search(text, pos)
        if not m:
            return
        try:
            obj, end = _decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            yield m.start(), len(text), None
            return
        yield m.start(), end, obj
        pos = end


def _looks_like_call(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    return isinstance(obj.get("name"), str) and ("arguments" in obj or "parameters" in obj)


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
    calls, pieces, last = [], [], 0
    for start, end, obj in _embedded(text):
        call = _call_from_obj(obj, known) if obj is not None else None
        if call:
            calls.append(call)
            pieces.append(text[last:start])
            last = end
    if calls:
        pieces.append(text[last:])
        return calls, "".join(pieces).strip()
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):  # e.g. {"arguments": {...}, "name": "..."}
        call = _decode(stripped, known)
        if call:
            return [call], ""
    return [], text


def broken_call_error(text: str, known: set[str]) -> str | None:
    """If text contains something that looks like a tool call but couldn't be used, say why."""
    for start, end, obj in _embedded(text):
        if obj is None:
            try:
                _decoder.raw_decode(text, start)
            except json.JSONDecodeError as e:
                return f"its JSON is invalid ({e.msg} at character {e.pos - start})"
        if _looks_like_call(obj):
            inner = obj["function"] if isinstance(obj.get("function"), dict) else obj
            if inner.get("name") not in known:
                return f"there is no tool named {inner.get('name')!r}; available: {', '.join(sorted(known))}"
            return "its arguments aren't a JSON object"
    return None


def parse_text_tool_calls(text: str, known: set[str]) -> list[ParsedCall]:
    return extract_text_tool_calls(text, known)[0]


def strip_tool_markup(text: str) -> str:
    """Hide tool-call text (tagged, fenced, bare or after prose; complete or still streaming) for display."""
    text = OPEN_TAG_RE.sub("", TAG_RE.sub("", text))
    pieces, last = [], 0
    for start, end, obj in _embedded(text):
        if obj is None or _looks_like_call(obj):  # unfinished objects are hidden until we know what they are
            pieces.append(text[last:start])
            last = end
    pieces.append(text[last:])
    text = re.sub(r"```(?:json)?\s*```", "", "".join(pieces))  # fence left empty by a removed call
    if text.count("```") % 2 == 1:  # an opening fence whose call was cut off mid-stream
        text = TRAILING_FENCE_RE.sub("", text)
    text = text.strip()
    return "" if PARTIAL_START_RE.match(text) else text
