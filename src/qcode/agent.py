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
from .prompts import COMPACT_PROMPT, build_nudge, build_system_prompt
from .toolparse import broken_call_error, extract_text_tool_calls
from .tools import ALL_TOOLS, WEB_TOOLS, ToolContext, ToolError, normalize_args

DENIED = "The user denied this tool call. Stop and wait for their instructions."
MAX_REPAIRS = 2  # times per turn we ask the model to rewrite a tool call we couldn't read
MAX_NUDGES = 2   # times per turn we remind it to actually make the call it talked about
FILE_TOOLS = ("write_file", "edit_file")

# "I'll write this to the file.", "Here's the command:" … announced but not done.
_INTENT_RE = re.compile(
    r"\b(?:i'll|i will|i'm going to|let me(?!\s+know)|let's|let us|now i'll|next,? i'll|i'll now)\b.*\b(?:write|save|create|update|apply|add|"
    r"edit|change|run|read|make|fix|implement|modify|replace|polish|style|fetch|check|search|look|get|use|call|try|open|"
    r"download|query|find)\b",
    re.I,
)


# Messages that ask for something online. This only decides whether web tools are *offered*;
# the model still chooses whether to use them.
_WEB_INTENT_RE = re.compile(
    r"https?://|\b(?:search(?:\s+(?:the\s+)?(?:web|internet|online|for))?|look\s*(?:it\s*)?up|google|duckduckgo|browse|"
    r"on\s+the\s+(?:web|internet)|online|website|news|headlines|latest|newest|weather|forecast|temperature\s+(?:in|today|outside)|"
    r"rain(?:ing)?\s+(?:today|tomorrow)|today|tonight|tomorrow|this\s+(?:week|weekend)|right\s+now|"
    r"price\s+of|exchange\s+rate|stock\s+price|who\s+won|score\s+of|release\s+date|changelog)\b",
    re.I,
)


def wants_web(text: str) -> bool:
    return bool(_WEB_INTENT_RE.search(text or ""))


WEB_NOTE = (
    "[Web access is on for this message (the user asked about something online, or this follows up on it). Use web_search, then call "
    "web_fetch yourself on the most relevant result (don't ask the user which one to open), or use weather for weather "
    "questions. If this is a follow-up (e.g. 'what about <another place>?'), look the new thing up the same way as "
    "before; unfamiliar names may be misspelled places, so search for the spelling rather than guessing. If you end up using "
    "a different name than the user typed, say so (\"I assumed you meant X\"). Answer directly and say which sources you "
    "used.{location}]\n\n"
)


_PERMISSION_RE = re.compile(
    r"\b(?:please\s+confirm|confirm\s+(?:if|whether|that)|shall\s+i\s+(?:proceed|go\s+ahead|continue)|"
    r"should\s+i\s+(?:proceed|go\s+ahead)|(?:would|do)\s+you\s+(?:like|want)\s+me\s+to\s+(?:proceed|go\s+ahead|continue)|"
    r"let\s+me\s+know\s+if\s+(?:you'd|you\s+would)\s+like\s+me\s+to\s+proceed)\b",
    re.I,
)
_CLAIM_RE = re.compile(r"\bI(?:'ve|\s+have)\s+(?:now\s+)?(?:added|updated|created|fixed|changed|modified|written|saved|renamed|replaced|edited)\b", re.I)


_NO_ACCESS_RE = re.compile(
    r"\b(?:(?:do\s*n[o']?t|does\s*n[o']?t)\s+have\s+(?:any\s+)?(?:specific\s+|real-time\s+|current\s+|up-to-date\s+|live\s+)?"
    r"(?:information|data|access|details)|(?:can(?:no|')t|unable\s+to|not\s+able\s+to)\s+(?:access|browse|search|look\s+up|find|check)|"
    r"no\s+(?:real-time|internet|web|online)\s+access|without\s+(?:internet|web)\s+access)",
    re.I,
)


def _mentions_tool(body: str, name: str) -> bool:
    """Tool names like web_fetch are unambiguous; plain words like "weather" only count when written as a call."""
    if "_" in name:
        return re.search(rf"\b{re.escape(name)}\b", body) is not None
    n = re.escape(name)
    return re.search(rf"`{n}`|\b{n}\s*\(|\b(?:use|call|run|using|calling)\s+(?:the\s+)?{n}\b|\b{n}\s+(?:tool|call)\b", body, re.I) is not None


def stall_reason(text: str, tool_names, acted: bool, web_turn: bool = False) -> tuple[str, str] | None:
    """Why a reply without a tool call looks like the model meant to act: (reason, tool it named), or None."""
    body = (text or "").strip()
    if not body:
        return None
    named = next((t for t in tool_names if _mentions_tool(body, t)), "")
    if named:
        return "named", named
    if web_turn and not acted and _NO_ACCESS_RE.search(body):
        return "no_access", "web_search"
    if _PERMISSION_RE.search(body):
        return "permission", ""
    if not acted and _CLAIM_RE.search(body):
        return "claimed", ""
    prose = re.sub(r"```.*?(?:```|\Z)", "", body, flags=re.S)  # judge the last sentence, not a trailing code block
    lines = [l.strip() for l in prose.splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    if last and "?" not in last and (last.endswith(":") or _INTENT_RE.search(last)):
        return "announced", ""
    return None


def looks_unfinished(text: str) -> bool:
    """True when a reply ends by announcing an action (and didn't ask the user anything)."""
    return stall_reason(text, (), acted=True) is not None and stall_reason(text, (), acted=True)[0] == "announced"


@dataclass
class TurnStats:
    model: str
    tools: int = 0
    seconds: float = 0.0
    gen_seconds: float = 0.0
    tokens: int = 0
    ctx_used: int = 0
    ctx_limit: int = 0
    repairs: int = 0  # malformed tool calls the model had to resend
    nudges: int = 0   # times it announced an action without doing it


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
        self.ctx = ToolContext(root, guard_overwrite=config.read_before_write, location=config.location)
        self.permissions = permissions
        self.ui = ui
        self.tools = ALL_TOOLS        # always available
        self.turn_tools = ALL_TOOLS   # what this turn offers (adds WEB_TOOLS when the user asked for the web)
        self.web_followup = False     # the last turn used a web tool, so a follow-up question keeps web access
        self._web_used = False
        self.session_path = session_path or sessions.new_session_path()
        self.messages: list[dict] = [self._system_message()]
        self._ctx_cache: dict[tuple[str, str], int] = {}
        self.checkpoints: list[Checkpoint] = []
        self._checkpoint: Checkpoint | None = None
        self.last_stats: TurnStats | None = None

    # --- setup / state ------------------------------------------------------------
    def _system_message(self) -> dict:
        mode = getattr(self.permissions, "mode", "ask")
        examples = bool(getattr(self.config, "tool_examples", False))
        return {"role": "system", "content": build_system_prompt(self.root, load_memory(self.root), mode, examples)}

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
    def web_enabled_for(self, text: str, force: bool | None = None) -> bool:
        mode = getattr(self.config, "web_access", "when-asked")
        if mode == "off":
            return False
        if force is not None:
            return force
        return mode == "always" or wants_web(text)

    def run(self, user_text: str, label: str | None = None, web: bool | None = None) -> None:
        """Run one user turn. `label` is what /undo shows for it (defaults to user_text).
        `web` forces web tools on/off for this turn; None decides from the message (see web_access)."""
        if user_text.startswith("[Web access is on") and "]\n\n" in user_text:  # e.g. /retry of a web turn
            user_text = user_text.split("]\n\n", 1)[1]
        use_web = self.web_enabled_for(label or user_text, web)
        if not use_web and self.web_followup and web is None and getattr(self.config, "web_access", "when-asked") != "off":
            use_web = True  # "what about Phatthalung?" right after a weather question
        self._web_used = False
        self.turn_tools = {**self.tools, **WEB_TOOLS} if use_web else self.tools
        self.ctx.location = self.config.location
        if use_web:
            where = f" The user's location: {self.config.location}." if self.config.location else ""
            user_text = WEB_NOTE.format(location=where) + user_text
        start_len = len(self.messages)
        stats = TurnStats(self.model)
        self.last_stats = stats  # readable even if the turn ends in an exception
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
            self.web_followup = self._web_used
            self.save()
        stats.seconds = time.monotonic() - started
        stats.ctx_used, stats.ctx_limit = self.tokens_used(), self.num_ctx
        self.ui.turn_done(stats)

    def _loop(self, stats: TurnStats) -> None:
        schemas = [t.schema() for t in self.turn_tools.values()]
        repairs = nudges = 0
        self._failed_calls: set[tuple[str, str]] = set()
        extra = {} if self.config.temperature_value is None else {"temperature": self.config.temperature_value}
        if getattr(self.config, "reasoning_effort", None):
            extra["reasoning_effort"] = self.config.reasoning_effort
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
                parsed, remaining = extract_text_tool_calls(content, set(self.turn_tools) | set(WEB_TOOLS))
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
                problem = broken_call_error(content, set(self.turn_tools))
                if problem and repairs < MAX_REPAIRS:
                    repairs += 1
                    stats.repairs += 1
                    self.ui.warn(f"The model's tool call couldn't be used: {problem}. Asking it to try again…")
                    self.messages.append({"role": "user", "content": (
                        f"Your tool call couldn't be used: {problem}. Send it again as one valid JSON object, "
                        'exactly like {"name": "write_file", "arguments": {"path": "page.html", "content": "..."}} '
                        "with quotes and newlines inside strings escaped (\\n, \\\")."
                    )})
                    continue
                stall = stall_reason(content, list(self.turn_tools), acted=stats.tools > 0, web_turn="web_search" in self.turn_tools)
                if self.config.auto_continue and stall and not problem and nudges < MAX_NUDGES:
                    nudges += 1
                    stats.nudges += 1
                    self.ui.info("The model talked about doing something but didn't do it; nudging it to continue…")
                    self.messages.append({"role": "user", "content": build_nudge(*stall)})
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
        tool = self.turn_tools.get(call.name)
        if tool is None and call.name in WEB_TOOLS:
            return ("Error: web tools are only available when the user asks for something online. Answer from what "
                    "you know, or tell the user they can ask you to search the web."), False
        if tool is None:
            return f"Error: unknown tool '{call.name}'. Available: {', '.join(self.turn_tools)}", False
        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return f"Error: invalid arguments JSON ({e})", False
        args = normalize_args(tool, args)

        if call.name in WEB_TOOLS:
            self._web_used = True
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
        if is_error:
            signature = (call.name, json.dumps(args, sort_keys=True))
            if signature in self._failed_calls:
                result += ("\nYou already made exactly this call and it failed the same way. Don't repeat it: change "
                           "the arguments (e.g. copy the text from read_file exactly) or use a different approach, such "
                           "as write_file with the whole corrected file.")
            self._failed_calls.add(signature)
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
