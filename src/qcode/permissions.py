"""Approval of non-read-only tool calls."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Optional

from .config import add_project_allow, load_project_allow, save_project_allow

# Ask callback returns one of: "y" (once), "n" (deny), "a" (always this session), "p" (always in this project)
AskFn = Callable[[str, dict, Optional[str]], str]

# A rule like "bash:git status" must not approve "git status; rm -rf ~".
SHELL_CHAINING = re.compile(r"[;&|`<>\n]|\$\(")


def rule_key(tool: str, args: dict) -> str:
    if tool == "bash":
        return f"bash:{args.get('command', '').strip()}"
    return tool


class Permissions:
    def __init__(self, root: Path, config_allow: list[str], ask: AskFn, yolo: bool = False):
        self.root = root
        self.config_allow = list(config_allow)
        self.project_allow = load_project_allow(root)
        self.session_allow: list[str] = []
        self.ask = ask
        self.yolo = yolo
        self.last_asked = False  # whether the most recent check() prompted the user

    def is_allowed(self, tool: str, args: dict) -> bool:
        if self.yolo:
            return True
        key = rule_key(tool, args)
        if tool == "bash" and SHELL_CHAINING.search(key):
            return False
        return any(fnmatchcase(key, r) for r in (*self.config_allow, *self.project_allow, *self.session_allow))

    def check(self, tool: str, args: dict, preview: str | None) -> bool:
        self.last_asked = False
        if self.is_allowed(tool, args):
            return True
        self.last_asked = True
        choice = self.ask(tool, args, preview)
        rule = rule_key(tool, args)
        if choice == "a":
            self.session_allow.append(rule)
        elif choice == "p":
            self.project_allow.append(rule)
            add_project_allow(self.root, rule)
        return choice in ("y", "a", "p")

    def rules(self) -> list[tuple[str, str]]:
        """(source, rule) for every allow rule, in matching order."""
        return (
            [("config", r) for r in self.config_allow]
            + [("project", r) for r in self.project_allow]
            + [("session", r) for r in self.session_allow]
        )

    def reset(self) -> int:
        """Forget project and session approvals. Config rules stay (edit them in config.toml)."""
        n = len(self.project_allow) + len(self.session_allow)
        self.session_allow.clear()
        self.project_allow.clear()
        save_project_allow(self.root, [])
        return n

    def remove(self, rule: str) -> bool:
        removed = False
        if rule in self.session_allow:
            self.session_allow.remove(rule)
            removed = True
        if rule in self.project_allow:
            self.project_allow.remove(rule)
            save_project_allow(self.root, self.project_allow)
            removed = True
        return removed
