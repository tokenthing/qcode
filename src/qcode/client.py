"""Thin wrapper over the OpenAI SDK pointed at an OpenAI-compatible server (Ollama's /v1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import httpx
from openai import OpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, as in the OpenAI API


class LLMClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=600)
        self.last_completion_tokens = 0

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self._client.models.list())

    def context_length(self, model: str) -> int | None:
        """The num_ctx an Ollama model was built with, or None if the server doesn't say."""
        root = self.base_url.rstrip("/").removesuffix("/v1")
        try:
            r = httpx.post(f"{root}/api/show", json={"model": model}, timeout=3)
            if r.status_code != 200:
                return None
            m = re.search(r"\bnum_ctx\s+(\d+)", r.json().get("parameters") or "")
        except (httpx.HTTPError, ValueError):
            return None
        return int(m.group(1)) if m else None

    def complete(self, model: str, messages: list[dict]) -> str:
        resp = self._client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        on_text: Callable[[str], None],
    ) -> tuple[str, list[ToolCall]]:
        kwargs = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            kwargs["tools"] = tools
        content: list[str] = []
        slots: dict[int, dict] = {}
        chunks = 0
        usage_tokens = 0
        for chunk in self._client.chat.completions.create(**kwargs):
            if chunk.usage:
                usage_tokens = chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            chunks += 1
            delta = chunk.choices[0].delta
            if delta.content:
                content.append(delta.content)
                on_text(delta.content)
            for tc in delta.tool_calls or []:
                key = tc.index if tc.index is not None else len(slots)
                # Some servers send each complete call with index 0; a new id means a new call.
                if key in slots and tc.id and slots[key]["id"] and slots[key]["id"] != tc.id:
                    key = max(slots) + 1
                slot = slots.setdefault(key, {"id": None, "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    slot["name"] += tc.function.name or ""
                    slot["arguments"] += tc.function.arguments or ""
        self.last_completion_tokens = usage_tokens or chunks
        calls = [
            ToolCall(s["id"] or f"call_{i}", s["name"], s["arguments"] or "{}")
            for i, s in sorted(slots.items())
            if s["name"]
        ]
        return "".join(content), calls
