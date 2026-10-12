"""The agent loop: model → tool calls → results → model, until the model stops calling tools."""

from __future__ import annotations

import datetime
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import sessions
from .client import ToolCall
from .config import Config
from .context import estimate_tokens, transcript, truncate
from .memory import load_memory
from .permissions import Permissions
from .prompts import COMPACT_PROMPT, CONTINUE_NUDGE, build_system_prompt
from .toolparse import broken_call_error, extract_text_tool_calls
from .tools import ALL_TOOLS, ToolContext, ToolError, normalize_args

DENIED = "The user denied this tool call. Stop and wait for their instructions."
MAX_REPAIRS = 2  # times per turn we ask the model to rewrite a tool call we couldn't read
FILE_TOOLS = ("write_file", "edit_file")

# "I'll write this to the file.", "Here's the command:" … announced but not done.
_INTENT_RE = re.compile(
    r"\b(?:i'll|i will|i'm going to|let me|now i'll|next,? i'll|i'll now)\b.*\b(?:write|save|create|update|apply|add|"
    r"edit|change|run|read|make|fix|implement|modify|replace|polish|style)\b",
    re.I,
)


def looks_unfinished(text: str) -> bool:
    """True when a reply ends by announcing an action (and didn't ask the user anything)."""
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip() and not l.strip().startswith("```")]
    if not lines:
        return False
    last = lines[-1]
    if "?" in last:
        return False
    return last.endswith(":") or bool(_INTENT_RE.search(last))


@dataclass
class TurnStats:
    model: str
    tools: int = 0
    seconds: float = 0.0
    gen_seconds: float = 0.0
    tokens: int = 0
    ctx_used: int = 0
    ctx_limit: int = 0


@dataclass
class Checkpoint:
    """File contents before one turn's edits, so /undo can put them back."""

    prompt: str
    files: dict[str, Optional[str]] = field(default_factory=dict)  # path -> old text (None = file didn't exist)


class Agent:
    def __init__(self, client, config: Config, root: Path, permissions: Permissions, ui, session_path: Path | None = None):
        self.client = client
        self.config = config
        self.model = config.model
        self.root = root
        self.ctx = ToolContext(root, guard_overwrite=config.read_before_write)
        self.permissions = permissions
        self.ui = ui
        self.tools = ALL_TOOLS
        self.session_path = session_path or sessions.new_session_path()
        self.messages: list[dict] = [self._system_message()]
        self._ctx_cache: dict[tuple[str, str], int] = {}
        self.checkpoints: list[Checkpoint] = []
        self._checkpoint: Checkpoint | None = None

    # --- setup / state ------------------------------------------------------------
    def _system_message(self) -> dict:
        mode = getattr(self.permissions, "mode", "ask")
        return {"role": "system", "content": build_system_prompt(self.root, load_memory(self.root), mode)}

    def refresh_system(self) -> None:
        self.messages[0] = self._system_message()

    def reset(self) -> None:
        self.messages = [self._system_message()]
        self.session_path = sessions.new_session_path()
        self.checkpoints = []
        self.ctx.seen.clear()

    def load_session(self, path: Path) -> None:
        data = sessions.load(path)
        self.messages = [self._system_message(), *data["messages"]]
        self.session_path = path
        self.checkpoints = [Checkpoint(c["prompt"], dict(c["files"])) for c in data.get("checkpoints", [])]
        if data.get("model"):
            self.model = data["model"]

    def save(self) -> None:
        sessions.save(self.session_path, self.root, self.model, self.messages,
                      [{"prompt": c.prompt, "files": c.files} for c in self.checkpoints])

    def forget_context_sizes(self) -> None:
        """Drop cached context sizes (after /context set or a server change)."""
        self._ctx_cache.clear()

    def context_for(self, model: str) -> int:
        """Context window, best source first: the model's num_ctx, what Ollama is running it with,
        [models."<name>"] in config.toml, Ollama's default, and finally a generic guess."""
        key = (getattr(self.client, "base_url", ""), model)
        if key in self._ctx_cache:
            return self._ctx_cache[key]
        probe = getattr(self.client, "context_info", None)
        info = probe(model) if probe else None
        configured = model.removesuffix(":latest") in self.config.models
        if info and info.source in ("model", "loaded"):
            n = info.effective
        elif configured:
            n = self.config.num_ctx(model)
        elif info and info.source == "ollama-default":
            n = info.effective
        else:
            n = self.config.num_ctx(model)
        if info is None or info.source in ("model", ""):
            self._ctx_cache[key] = n  # fixed for this model; the others can change once it loads
        return n

    @property
    def num_ctx(self) -> int:
        return self.context_for(self.model)

    def tokens_used(self) -> int:
        return estimate_tokens(self.messages) + estimate_tokens([t.schema() for t in self.tools.values()])

    # --- main loop ----------------------------------------------------------------
    def run(self, user_text: str, label: str | None = None) -> None:
        """Run one user turn. `label` is what /undo shows for it (defaults to user_text)."""
        start_len = len(self.messages)
        stats = TurnStats(self.model)
        started = time.monotonic()
        self._checkpoint = Checkpoint(label or user_text)
        self.messages.append({"role": "user", "content": user_text})
        try:
            self._loop(stats)
        except KeyboardInterrupt:
            # Drop the partial turn so history never has tool_calls without results.
            del self.messages[start_len:]
            self.ui.warn("Interrupted · this turn was discarded")
            return
        except Exception:
            del self.messages[start_len:]
            raise
        finally:
            # Files may have changed even if the turn failed, so keep the checkpoint either way.
            if self._checkpoint and self._checkpoint.files:
                self.checkpoints.append(self._checkpoint)
            self._checkpoint = None
            self.save()
        stats.seconds = time.monotonic() - started
        stats.ctx_used, stats.ctx_limit = self.tokens_used(), self.num_ctx
        self.ui.turn_done(stats)

    def _loop(self, stats: TurnStats) -> None:
        schemas = [t.schema() for t in self.tools.values()]
        repairs = nudged = 0
        extra = {} if self.config.temperature_value is None else {"temperature": self.config.temperature_value}
        for _ in range(self.config.max_steps):
            self.maybe_compact()
            self.ui.stream_start()
            gen_start = time.monotonic()
            try:
                content, calls = self.client.stream_chat(self.model, self.messages, schemas, self.ui.stream_text, **extra)
            finally:
                self.ui.stream_end()
            stats.gen_seconds += time.monotonic() - gen_start
            stats.tokens += getattr(self.client, "last_completion_tokens", 0) or 0

            if not calls:
                parsed, remaining = extract_text_tool_calls(content, set(self.tools))
                if parsed:
                    calls = [ToolCall(f"call_text_{i}", c.name, json.dumps(c.arguments)) for i, c in enumerate(parsed)]
                    content = remaining

            msg: dict = {"role": "assistant", "content": content}
            if calls:
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls
                ]
            self.messages.append(msg)
            if not calls:
                problem = broken_call_error(content, set(self.tools))
                if problem and repairs < MAX_REPAIRS:
                    repairs += 1
                    self.ui.warn(f"The model's tool call couldn't be used: {problem}. Asking it to try again…")
                    self.messages.append({"role": "user", "content": (
                        f"Your tool call couldn't be used: {problem}. Send it again as one valid JSON object, "
                        'exactly like {"name": "write_file", "arguments": {"path": "page.html", "content": "..."}} '
                        "with quotes and newlines inside strings escaped (\\n, \\\")."
                    )})
                    continue
                if self.config.auto_continue and not nudged and looks_unfinished(content):
                    nudged = 1
                    self.ui.info("The model said it would do something but didn't; nudging it to continue…")
                    self.messages.append({"role": "user", "content": CONTINUE_NUDGE})
                    continue
                return

            for i, call in enumerate(calls):
                stats.tools += 1
                result, stop = self._execute(call)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result})
                if stop:
                    # Every tool_call needs a result, even the ones we skip.
                    for skipped in calls[i + 1 :]:
                        self.messages.append({"role": "tool", "tool_call_id": skipped.id, "name": skipped.name, "content": "Skipped."})
                    return
        self.ui.warn(f"Stopped after {self.config.max_steps} steps. Say 'continue' to keep going.")

    def _execute(self, call: ToolCall) -> tuple[str, bool]:
        """Run one tool call. Returns (result text, stop_turn)."""
        tool = self.tools.get(call.name)
        if tool is None:
            return f"Error: unknown tool '{call.name}'. Available: {', '.join(self.tools)}", False
        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return f"Error: invalid arguments JSON ({e})", False
        args = normalize_args(tool, args)

        self.ui.tool_call(call.name, args)
        blocked = None if tool.read_only else self.permissions.blocked(call.name)
        if blocked:
            self.ui.tool_result(call.name, args, "Not run: plan mode is on (Shift+Tab to change mode)", True)
            return blocked, False
        preview, asked = None, False
        try:
            preview = tool.preview(self.ctx, args) if tool.preview else None
            if not tool.read_only:
                allowed = self.permissions.check(call.name, args, preview)
                asked = self.permissions.last_asked
                if not allowed:
                    self.ui.tool_denied(call.name)
                    return DENIED, True
            if call.name in FILE_TOOLS:
                self._remember_file(args.get("path", ""))
            result, is_error = tool.run(self.ctx, args), False
        except ToolError as e:
            result, is_error = f"Error: {e}", True
        except KeyError as e:
            result, is_error = f"Error: missing required argument {e}", True
        except Exception as e:  # tool bugs shouldn't kill the session
            result, is_error = f"Error: {type(e).__name__}: {e}", True
        # The diff was already on screen if the user was asked; otherwise show it with the result.
        self.ui.tool_result(call.name, args, result, is_error, None if asked or is_error else preview)
        return truncate(result, self.config.tool_output_limit), False

    # --- undo / retry / introspection ---------------------------------------------
    def _remember_file(self, path: str) -> None:
        if self._checkpoint is None:
            return
        try:
            p = self.ctx.resolve(path)
        except ToolError:
            return  # the tool itself will report the bad path
        key = str(p)
        if key not in self._checkpoint.files:
            try:
                self._checkpoint.files[key] = p.read_text(encoding="utf-8") if p.is_file() else None
            except (OSError, UnicodeDecodeError):
                pass

    def undo(self) -> Checkpoint | None:
        """Restore files changed by the most recent turn that changed files."""
        if not self.checkpoints:
            return None
        cp = self.checkpoints.pop()
        for path, old in cp.files.items():
            p = Path(path)
            if old is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(old, encoding="utf-8")
        return cp

    def last_user_index(self) -> int | None:
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m["role"] == "user" and not str(m.get("content", "")).startswith("[Summary of the earlier conversation]"):
                return i
        return None

    def pop_last_turn(self) -> str | None:
        """Remove the last user message and everything after it; returns its text (for /retry)."""
        i = self.last_user_index()
        if i is None:
            return None
        text = self.messages[i]["content"]
        del self.messages[i:]
        return text

    def last_reply(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant" and (m.get("content") or "").strip():
                return m["content"]
        return ""

    def context_breakdown(self) -> list[tuple[str, int]]:
        groups = {"system prompt + memory": [self.messages[0]], "your messages": [], "replies": [], "tool results": []}
        for m in self.messages[1:]:
            key = {"user": "your messages", "assistant": "replies", "tool": "tool results"}.get(m["role"], "replies")
            groups[key].append(m)
        rows = [(name, estimate_tokens(msgs)) for name, msgs in groups.items()]
        rows.insert(1, ("tool definitions", estimate_tokens([t.schema() for t in self.tools.values()])))
        return rows

    def export_markdown(self) -> str:
        out = [f"# qcode session", "", f"_{datetime.datetime.now():%Y-%m-%d %H:%M} · model `{self.model}` · {self.root}_", ""]
        for m in self.messages[1:]:
            role, content = m["role"], (m.get("content") or "").strip()
            if role == "user":
                out += ["## You", "", content, ""]
            elif role == "assistant":
                if content:
                    out += ["## qcode", "", content, ""]
                for tc in m.get("tool_calls") or []:
                    fn = tc["function"]
                    out += [f"**🔧 {fn['name']}** `{fn['arguments']}`", ""]
            elif role == "tool":
                lines = content.splitlines()
                body = "\n".join(lines[:40]) + (f"\n… ({len(lines) - 40} more lines)" if len(lines) > 40 else "")
                out += ["<details><summary>result</summary>", "", "```", body, "```", "", "</details>", ""]
        return "\n".join(out)

    # --- context management -------------------------------------------------------
    def maybe_compact(self) -> None:
        if self.tokens_used() >= self.config.compact_at / 100 * self.num_ctx:
            self.ui.info("Context nearly full; compacting history…")
            self.compact(keep_current_turn=True)

    def compact(self, keep_current_turn: bool = False) -> None:
        split = len(self.messages)
        if keep_current_turn:
            split = max(i for i, m in enumerate(self.messages) if m["role"] == "user")
        head, tail = self.messages[1:split], self.messages[split:]
        if head:
            text = transcript(head, max_chars=int(self.num_ctx * 2.5))
            summary = self.client.complete(self.model, [{"role": "user", "content": COMPACT_PROMPT.format(transcript=text)}])
            self.messages = [
                self.messages[0],
                {"role": "user", "content": f"[Summary of the earlier conversation]\n{summary}"},
                {"role": "assistant", "content": "Understood. Continuing from that summary."},
                *tail,
            ]
        # Still too big (one very long turn): elide older tool outputs, keeping the latest two.
        if self.tokens_used() >= self.config.compact_at / 100 * self.num_ctx:
            tool_idx = [i for i, m in enumerate(self.messages) if m["role"] == "tool"]
            for i in tool_idx[:-2]:
                self.messages[i]["content"] = "[old tool output removed to save context]"
