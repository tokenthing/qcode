"""Thin wrapper over the OpenAI SDK pointed at an OpenAI-compatible server (Ollama's /v1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import httpx
from openai import OpenAI


OLLAMA_DEFAULT_CTX = 4096  # what Ollama uses when neither the model nor OLLAMA_CONTEXT_LENGTH sets num_ctx


@dataclass
class ContextInfo:
    effective: int | None  # tokens the server will actually use
    maximum: int | None    # what the model was trained for
    source: str            # "model" (num_ctx set), "loaded" (running now), "ollama-default", or "" (unknown server)


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

    def context_info(self, model: str) -> ContextInfo:
        """Ask Ollama how much context `model` really gets. Non-Ollama servers return an unknown ContextInfo."""
        root = self.base_url.rstrip("/").removesuffix("/v1")
        try:
            r = httpx.post(f"{root}/api/show", json={"model": model}, timeout=3)
            if r.status_code != 200:
                return ContextInfo(None, None, "")
            show = r.json()
        except (httpx.HTTPError, ValueError):
            return ContextInfo(None, None, "")
        maximum = next((int(v) for k, v in (show.get("model_info") or {}).items() if k.endswith(".context_length")), None)
        m = re.search(r"\bnum_ctx\s+(\d+)", show.get("parameters") or "")
        if m:
            return ContextInfo(int(m.group(1)), maximum, "model")
        # Not set on the model: if it's loaded, Ollama tells us what it's running with (covers OLLAMA_CONTEXT_LENGTH).
        try:
            loaded = httpx.get(f"{root}/api/ps", timeout=3).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            loaded = []
        names = {model, f"{model}:latest"}
        for entry in loaded:
            if (entry.get("name") in names or entry.get("model") in names) and entry.get("context_length"):
                return ContextInfo(int(entry["context_length"]), maximum, "loaded")
        return ContextInfo(OLLAMA_DEFAULT_CTX, maximum, "ollama-default")

    @property
    def _ollama_root(self) -> str:
        return self.base_url.rstrip("/").removesuffix("/v1")

    def set_context(self, model: str, tokens: int) -> None:
        """Bake num_ctx into an Ollama model on the server (same as a Modelfile with PARAMETER num_ctx).
        Ollama keeps the model's other settings (template, stop tokens). Raises RuntimeError on failure."""
        try:
            r = httpx.post(
                f"{self._ollama_root}/api/create",
                json={"model": model, "from": model, "parameters": {"num_ctx": tokens}, "stream": False},
                timeout=600,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"couldn't reach the server: {e}") from None
        if r.status_code != 200:
            try:
                detail = r.json().get("error") or r.text
            except ValueError:
                detail = r.text
            raise RuntimeError(detail.strip() or f"HTTP {r.status_code}")

    def gpu_fit(self, model: str) -> tuple[int, int] | None:
        """Load the model and report (percent of it on the GPU, total size in MB), or None if unknown."""
        root = self._ollama_root
        try:
            httpx.post(f"{root}/api/generate", json={"model": model, "prompt": ""}, timeout=600)
            loaded = httpx.get(f"{root}/api/ps", timeout=10).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return None
        names = {model, f"{model}:latest"}
        for m in loaded:
            if m.get("name") in names or m.get("model") in names:
                size = max(int(m.get("size") or 0), 1)
                return min(100, int(m.get("size_vram") or 0) * 100 // size), size // (1024 * 1024)
        return None

    def complete(self, model: str, messages: list[dict], reasoning_effort: str | None = None) -> str:
        extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        resp = self._client.chat.completions.create(model=model, messages=messages, **extra)
        return resp.choices[0].message.content or ""

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        on_text: Callable[[str], None],
        temperature: float | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, list[ToolCall]]:
        kwargs = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if reasoning_effort is not None:  # "none" turns off thinking models' hidden reasoning (much faster)
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs["tools"] = tools
        content: list[str] = []
        slots: dict[int, dict] = {}
        chunks = 0
        usage_tokens = 0
        stream = self._client.chat.completions.create(**kwargs)
        try:
            for chunk in stream:
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
        finally:
            stream.close()  # on Ctrl+C or a timeout, closing the connection makes Ollama stop generating
        self.last_completion_tokens = usage_tokens or chunks
        calls = [
            ToolCall(s["id"] or f"call_{i}", s["name"], s["arguments"] or "{}")
            for i, s in sorted(slots.items())
            if s["name"]
        ]
        return "".join(content), calls
