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
# qcode configuration. Change values here or with /config set <key> <value>.

# First API server, saved as "home" on first run. After that, manage servers inside qcode with
# /servers, /server <name>, /add_api_server and /remove_api_server (stored in servers.json).
base_url = "http://localhost:11434/v1"
api_key = "ollama"          # Ollama ignores it, but the OpenAI client requires one
model = "qcoder-14b"

max_steps = 25              # max model<->tool round trips per prompt
tool_output_limit = 4000    # characters of tool output sent back to the model
auto_update = "notify"      # notify | auto | off: checks GitHub for new releases once a day

# Rules that never need approval. Match "<tool>" or "bash:<command>" (glob syntax).
# Read-only tools (read_file, list_dir, glob, grep) are always allowed.
# Bash commands containing ; & | ` $( < > or newlines are never auto-allowed.
allow = ["bash:git status", "bash:git diff", "bash:git diff *", "bash:git log *", "bash:ls", "bash:ls *"]

# Fallback context window per model. qcode reads num_ctx from Ollama automatically;
# these are only used when the server doesn't report it.
[models.qcoder-14b]
num_ctx = 14336

[models.qcoder-7b]
num_ctx = 32768
"""

DEFAULT_NUM_CTX = 8192
AUTO_UPDATE_MODES = ("notify", "auto", "off")

# Keys /config set may change: key -> (type or allowed values, description)
SETTABLE: dict[str, tuple[object, str]] = {
    "model": (str, "default model for new sessions"),
    "max_steps": (int, "max model<->tool round trips per prompt"),
    "tool_output_limit": (int, "characters of tool output sent to the model"),
    "auto_update": (AUTO_UPDATE_MODES, "update check: notify, auto (install at startup) or off"),
}


@dataclass
class Config:
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qcoder-14b"
    max_steps: int = 25
    tool_output_limit: int = 4000
    auto_update: str = "notify"
    allow: list[str] = field(default_factory=list)
    models: dict[str, dict] = field(default_factory=dict)

    def num_ctx(self, model: str | None = None) -> int:
        name = (model or self.model).removesuffix(":latest")
        return int(self.models.get(name, {}).get("num_ctx", DEFAULT_NUM_CTX))


def load_config(path: Path = CONFIG_FILE) -> Config:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TOML, encoding="utf-8")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {k: v for k, v in data.items() if k in Config.__dataclass_fields__}
    cfg = Config(**known)
    cfg.base_url = os.environ.get("QCODE_BASE_URL", cfg.base_url)
    cfg.model = os.environ.get("QCODE_MODEL", cfg.model)
    if cfg.auto_update not in AUTO_UPDATE_MODES:
        cfg.auto_update = "notify"
    return cfg


def parse_setting(key: str, raw: str):
    """Validate and convert a /config set value. Raises ValueError with a readable message."""
    if key not in SETTABLE:
        raise ValueError(f"unknown setting {key!r}; settable: {', '.join(SETTABLE)}")
    kind, _ = SETTABLE[key]
    raw = raw.strip()
    if kind is int:
        if not raw.isdigit() or int(raw) < 1:
            raise ValueError(f"{key} must be a positive whole number")
        return int(raw)
    if isinstance(kind, tuple):
        if raw not in kind:
            raise ValueError(f"{key} must be one of: {', '.join(kind)}")
        return raw
    if not raw:
        raise ValueError(f"{key} can't be empty")
    return raw


def set_config_value(key: str, value, path: Path = CONFIG_FILE) -> None:
    """Write a top-level `key = value` into config.toml, keeping comments and other settings."""
    literal = str(value) if isinstance(value, int) else json.dumps(value)
    line = f"{key} = {literal}"
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_TOML
    table = re.search(r"(?m)^\[", text)
    head, tail = (text[: table.start()], text[table.start() :]) if table else (text, "")
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=[^\n]*$")
    if pattern.search(head):
        head = pattern.sub(lambda _: line, head, count=1)
    else:
        head = head.rstrip("\n") + f"\n{line}\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + tail, encoding="utf-8")


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
