"""User configuration (~/.config/qcode/config.toml) and per-project settings (.qcode/settings.json)."""

from __future__ import annotations

import json
import os
import re

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("QCODE_CONFIG_DIR", Path.home() / ".config" / "qcode"))
DATA_DIR = Path(os.environ.get("QCODE_DATA_DIR", Path.home() / ".local" / "share" / "qcode"))
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULT_TOML = """\
# qcode configuration
# Easiest way to change things: /settings inside qcode (or /config set <key> <value>).
# Everything here is optional; a missing setting uses the default shown.

# ── Server & model ─────────────────────────────────────────────────────────────
# Servers are saved in servers.json. qcode asks for the first one when it starts; after that use
# /servers, /server <name>, /add_api_server and /remove_api_server. (There is no default address.)
api_key = "ollama"            # Ollama ignores it, but the OpenAI client requires one
model = "qcoder-14b"          # default model for new sessions
temperature = "default"       # 0 = focused … 2 = creative, or "default" for the model's own
thinking = "default"          # default | off: turn off the hidden reasoning of thinking models like qwen3 (much faster)
tool_examples = false         # add a worked example of correct tool use to the instructions (helps small models)

# ── Behaviour ──────────────────────────────────────────────────────────────────
default_mode = "ask"          # ask | auto-edit | auto | plan   (Shift+Tab switches while running)
                              #   ask:       approve every file edit and command
                              #   auto-edit: file edits are automatic, commands still ask
                              #   auto:      approve everything (careful)
                              #   plan:      read-only; the model can look around and plan, not change anything
auto_continue = true          # nudge the model once when it says "I'll do X" but doesn't do it
read_before_write = true      # refuse to overwrite a file the model hasn't read this session
max_steps = 25                # max model<->tool round trips per prompt
tool_output_limit = 4000      # characters of tool output sent back to the model
compact_at = 80               # summarize the history when the context is this % full

# ── Updates ────────────────────────────────────────────────────────────────────
auto_update = "notify"        # notify | auto | off: checks GitHub for new releases once a day

# ── Web ────────────────────────────────────────────────────────────────────────
web_access = "when-asked"     # when-asked: web tools only for messages that ask for something online
                              #   ("search…", "look up…", "weather today", a link, or /web <question>)
                              # always: every message · off: never
location = ""                 # your city, e.g. "Bangkok", used for weather questions (only sent on web requests)

# ── Approvals ──────────────────────────────────────────────────────────────────
# Rules that never need approval. Match "<tool>" or "bash:<command>" (glob syntax).
# Read-only tools (read_file, list_dir, glob, grep) are always allowed.
# Bash commands containing ; & | ` $( < > or newlines are never auto-allowed.
allow = ["bash:git status", "bash:git diff", "bash:git diff *", "bash:git log *", "bash:ls", "bash:ls *"]

# ── Context size per model ─────────────────────────────────────────────────────
# qcode asks Ollama for each model's real context automatically. Use /context set <tokens>
# to change it (on Ollama this updates the model on the server). Entries here are used for
# servers that don't report it, e.g. LM Studio or vLLM:
#   [models."my-model"]
#   num_ctx = 32768
[models.qcoder-14b]
num_ctx = 14336

[models.qcoder-7b]
num_ctx = 32768
"""

DEFAULT_NUM_CTX = 8192
AUTO_UPDATE_MODES = ("notify", "auto", "off")
WEB_ACCESS_MODES = ("when-asked", "always", "off")
PERMISSION_MODES = ("ask", "auto-edit", "auto", "plan")
MODE_ALIASES = {"yolo": "auto", "autoedit": "auto-edit", "edit": "auto-edit", "default": "ask", "normal": "ask", "readonly": "plan", "read-only": "plan"}


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str  # "int" | "bool" | "choice" | "str" (non-empty) | "text" (may be empty) | "temperature"
    label: str
    help: str
    choices: tuple = ()
    lo: float | None = None
    hi: float | None = None


SETTINGS: list[Setting] = [
    Setting("default_mode", "choice", "Permission mode at startup",
            "ask: approve edits & commands · auto-edit: edits automatic, commands ask · auto: everything automatic · plan: read-only",
            choices=PERMISSION_MODES),
    Setting("model", "str", "Default model", "model used when qcode starts (use /model to switch right now)"),
    Setting("temperature", "temperature", "Temperature", "0 = focused and repeatable … 2 = creative; 'default' uses the model's own", lo=0, hi=2),
    Setting("thinking", "choice", "Thinking", "off: skip the hidden reasoning of models like qwen3 (much faster); default: the model decides",
            choices=("default", "off")),
    Setting("tool_examples", "bool", "Tool-use example", "show the model a worked example of correct tool calls (helps small models)"),
    Setting("auto_continue", "bool", "Auto-continue", "nudge the model once when it says it'll do something but doesn't call a tool"),
    Setting("read_before_write", "bool", "Read before overwrite", "refuse to overwrite an existing file the model hasn't read this session"),
    Setting("compact_at", "int", "Compact history at (%)", "summarize older messages when the context window is this full", lo=50, hi=95),
    Setting("max_steps", "int", "Max steps per prompt", "how many tool round trips the model may take for one request", lo=1, hi=200),
    Setting("tool_output_limit", "int", "Tool output limit", "characters of each tool result sent to the model", lo=500, hi=200000),
    Setting("web_access", "choice", "Web access",
            "when-asked: search the web only when your message asks for it · always · off", choices=WEB_ACCESS_MODES),
    Setting("location", "text", "Location", "your city for weather and local questions, e.g. Bangkok (empty = ask when needed)"),
    Setting("auto_update", "choice", "Updates", "notify: show when a release is out · auto: install it at startup · off: never check",
            choices=AUTO_UPDATE_MODES),
]
SETTABLE: dict[str, Setting] = {s.key: s for s in SETTINGS}


@dataclass
class Config:
    base_url: str = ""  # legacy: older configs named the first server here; it's moved into servers.json
    api_key: str = "ollama"
    model: str = "qcoder-14b"
    temperature: object = "default"  # "default" or a float
    thinking: str = "default"        # default | off
    tool_examples: bool = False
    default_mode: str = "ask"
    auto_continue: bool = True
    read_before_write: bool = True
    max_steps: int = 25
    tool_output_limit: int = 4000
    compact_at: int = 80
    auto_update: str = "notify"
    web_access: str = "when-asked"
    location: str = ""
    allow: list[str] = field(default_factory=list)
    models: dict[str, dict] = field(default_factory=dict)

    def num_ctx(self, model: str | None = None) -> int:
        name = (model or self.model).removesuffix(":latest")
        return int(self.models.get(name, {}).get("num_ctx", DEFAULT_NUM_CTX))

    def has_model_ctx(self, model: str) -> bool:
        return "num_ctx" in self.models.get(model.removesuffix(":latest"), {})

    @property
    def reasoning_effort(self) -> str | None:
        return "none" if self.thinking == "off" else None

    @property
    def temperature_value(self) -> float | None:
        return None if self.temperature in ("default", None, "") else float(self.temperature)


def load_config(path: Path = CONFIG_FILE) -> Config:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TOML, encoding="utf-8")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {k: v for k, v in data.items() if k in Config.__dataclass_fields__}
    cfg = Config(**known)
    cfg.base_url = os.environ.get("QCODE_BASE_URL", cfg.base_url)
    cfg.model = os.environ.get("QCODE_MODEL", cfg.model)
    # Anything out of range falls back to its default rather than breaking qcode.
    defaults = Config()
    for s in SETTINGS:
        try:
            setattr(cfg, s.key, parse_setting(s.key, str(getattr(cfg, s.key)).lower() if s.kind == "bool" else str(getattr(cfg, s.key))))
        except ValueError:
            setattr(cfg, s.key, getattr(defaults, s.key))
    return cfg


def parse_mode(raw: str) -> str:
    mode = MODE_ALIASES.get(raw.strip().lower(), raw.strip().lower())
    if mode not in PERMISSION_MODES:
        raise ValueError(f"mode must be one of: {', '.join(PERMISSION_MODES)}")
    return mode


def parse_tokens(raw: str) -> int:
    """'32768', '32k', '32K', '1m' -> tokens."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", raw)
    if not m:
        raise ValueError("give a number of tokens, e.g. 16384 or 16k")
    n = float(m.group(1)) * {"": 1, "k": 1024, "m": 1024 * 1024}[m.group(2).lower()]
    return int(n)


def parse_setting(key: str, raw: str):
    """Validate and convert a setting value from text. Raises ValueError with a readable message."""
    if key not in SETTABLE:
        raise ValueError(f"unknown setting {key!r}; settable: {', '.join(SETTABLE)}")
    s = SETTABLE[key]
    raw = raw.strip()
    if s.kind == "int":
        if not raw.isdigit():
            raise ValueError(f"{key} must be a whole number")
        n = int(raw)
        if (s.lo is not None and n < s.lo) or (s.hi is not None and n > s.hi):
            raise ValueError(f"{key} must be between {int(s.lo)} and {int(s.hi)}")
        return n
    if s.kind == "bool":
        v = raw.lower()
        if v in ("true", "on", "yes", "1", "y"):
            return True
        if v in ("false", "off", "no", "0", "n"):
            return False
        raise ValueError(f"{key} must be on or off")
    if s.kind == "choice":
        if key == "default_mode":
            return parse_mode(raw)
        if raw.lower() not in s.choices:
            raise ValueError(f"{key} must be one of: {', '.join(s.choices)}")
        return raw.lower()
    if s.kind == "temperature":
        if raw.lower() in ("default", "none", "auto", ""):
            return "default"
        try:
            t = float(raw)
        except ValueError:
            raise ValueError("temperature must be a number like 0.2, or 'default'") from None
        if not s.lo <= t <= s.hi:
            raise ValueError(f"temperature must be between {s.lo:g} and {s.hi:g}")
        return t
    if s.kind == "text":
        return raw  # may be empty
    if not raw:
        raise ValueError(f"{key} can't be empty")
    return raw


def _toml_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)


def set_config_value(key: str, value, path: Path = CONFIG_FILE) -> None:
    """Write a top-level `key = value` into config.toml, keeping comments and other settings."""
    line = f"{key} = {_toml_literal(value)}"
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_TOML
    table = re.search(r"(?m)^\[", text)
    head, tail = (text[: table.start()], text[table.start() :]) if table else (text, "")
    pattern = re.compile(rf"(?m)^{re.escape(key)}[ \t]*=[^\n]*?(?=[ \t]*#|$)")
    if pattern.search(head):
        head = pattern.sub(lambda m: line.ljust(len(m.group(0))), head, count=1)  # keeps trailing comments aligned
    else:
        # Put new keys before the comment block that introduces the first table.
        intro = re.search(r"(?m)(?:^#[^\n]*\n)+\Z", head)
        at = intro.start() if intro else len(head)
        head = head[:at].rstrip("\n") + f"\n{line}\n\n" + head[at:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + tail, encoding="utf-8")


def set_model_context(model: str, tokens: int, path: Path = CONFIG_FILE) -> None:
    """Write [models."<model>"] num_ctx = tokens into config.toml."""
    name = model.removesuffix(":latest")
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_TOML
    header = re.compile(rf'(?m)^\[models\.(?:"{re.escape(name)}"|{re.escape(name)})\][ \t]*$')
    m = header.search(text)
    if m:
        nxt = re.search(r"(?m)^\[", text[m.end():])
        end = m.end() + nxt.start() if nxt else len(text)
        section = text[m.end():end]
        if re.search(r"(?m)^num_ctx[ \t]*=", section):
            section = re.sub(r"(?m)^num_ctx[ \t]*=[^\n]*", f"num_ctx = {tokens}", section, count=1)
        else:
            section = f"\nnum_ctx = {tokens}" + section
        text = text[:m.end()] + section + text[end:]
    else:
        text = text.rstrip("\n") + f'\n\n[models."{name}"]\nnum_ctx = {tokens}\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def project_settings_file(root: Path) -> Path:
    return root / ".qcode" / "settings.json"


def _read_project_settings(root: Path) -> dict:
    path = project_settings_file(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_project_allow(root: Path) -> list[str]:
    return list(_read_project_settings(root).get("allow", []))


def save_project_allow(root: Path, rules: list[str]) -> None:
    data = _read_project_settings(root)
    data["allow"] = list(rules)
    path = project_settings_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def add_project_allow(root: Path, rule: str) -> None:
    rules = load_project_allow(root)
    if rule not in rules:
        save_project_allow(root, rules + [rule])
