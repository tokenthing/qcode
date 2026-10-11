"""Interactive prompt, bottom status bar, slash commands and !shell passthrough."""

from __future__ import annotations

import base64
import datetime
import os
import platform
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from openai import APIConnectionError, APIError
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.syntax import Syntax
from rich.text import Text

from . import __version__, sessions, ui, updater
from .agent import Agent
from .client import LLMClient
from .config import CONFIG_FILE, DATA_DIR, SETTABLE, load_config, parse_setting, project_settings_file, set_config_value
from .context import truncate
from .memory import project_memory_path
from .prompts import INIT_PROMPT
from .servers import SERVERS_FILE, Ping, Server, ServerStore, ping

COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Conversation", [
        ("/help", "show this help"),
        ("/clear", "start a fresh conversation"),
        ("/retry", "drop the last answer and ask again"),
        ("/undo", "revert the file changes from qcode's last turn"),
        ("/compact", "summarize the history to free up context"),
        ("/context", "show what's using the context window"),
        ("/copy", "copy the last reply to the clipboard"),
        ("/export", "save the conversation as Markdown: /export [file]"),
        ("/resume", "list recent sessions in this folder, or /resume <n>"),
        ("/save", "save the session now (it also autosaves every turn)"),
    ]),
    ("Models & servers", [
        ("/model", "list models, or /model <name> to switch"),
        ("/servers", "list API servers with live status"),
        ("/server", "switch API server: /server <name>"),
        ("/add_api_server", "add a server: /add_api_server <name> <ip|host:port|url> [api_key]"),
        ("/remove_api_server", "remove a server: /remove_api_server <name>"),
    ]),
    ("Project", [
        ("/diff", "show uncommitted git changes"),
        ("/init", "have the model explore the repo and write QCODE.md"),
        ("/memory", "edit this project's QCODE.md in $EDITOR"),
        ("/cwd", "show the project root"),
    ]),
    ("Settings", [
        ("/config", "show settings · /config set <key> <value> · /config edit"),
        ("/permissions", "list saved approvals · /permissions reset · /permissions remove <n>"),
        ("/yolo", "toggle approving every tool call without asking"),
        ("/tools", "list the tools the model can use"),
    ]),
    ("App", [
        ("/status", "model, server, context and session details"),
        ("/doctor", "check Python, servers, model, ZeroTier, git and more"),
        ("/version", "show the qcode version"),
        ("/update", "update qcode from GitHub"),
        ("/exit", "quit"),
    ]),
]
COMMANDS = {cmd: desc for _, cmds in COMMAND_GROUPS for cmd, desc in cmds}
SUBCOMMANDS = {"/config": ["set", "edit", "path"], "/permissions": ["reset", "remove"]}

PT_STYLE = Style.from_dict({
    "prompt": f"bold {ui.ACCENT}",
    "placeholder": ui.MUTED,
    "bottom-toolbar": "noreverse bg:#1e1e2e #a6adc8",
    "tb.model": f"bold {ui.ACCENT} bg:#2a2140",
    "tb.sep": "#45475a",
    "tb.text": "#a6adc8",
    "tb.addr": ui.ACCENT2,
    "tb.ok": ui.OK,
    "tb.bad": ui.ERR,
    "tb.unk": ui.MUTED,
    "tb.ctx-lo": ui.OK,
    "tb.ctx-mid": ui.WARN,
    "tb.ctx-hi": f"bold {ui.ERR}",
    "tb.yolo": f"bold {ui.ERR}",
    "tb.update": f"bold {ui.ACCENT2}",
    "completion-menu.completion": "bg:#1e1e2e #cdd6f4",
    "completion-menu.completion.current": f"bg:{ui.ACCENT} #11111b bold",
    "completion-menu.meta.completion": "bg:#181825 #7f849c",
    "completion-menu.meta.completion.current": "bg:#2a2140 #cdd6f4",
    "scrollbar.background": "bg:#313244",
    "scrollbar.button": "bg:#7f849c",
})


def git_branch(root: Path) -> str | None:
    try:
        head = (root / ".git" / "HEAD").read_text().strip()
    except OSError:
        return None
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:7]


def short_path(p: Path) -> str:
    home = Path.home()
    if p == home:
        return "~"
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return str(p)


def strip_latest(name: str) -> str:
    return name.removesuffix(":latest")


def copy_to_clipboard(text: str) -> str:
    """Copy text; returns how it was copied."""
    if sys.platform == "darwin":
        tools = [["pbcopy"]]
    elif os.name == "nt":
        tools = [["clip"]]
    else:
        tools = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in tools:
        if shutil.which(cmd[0]):
            data = b"\xff\xfe" + text.encode("utf-16-le") if cmd[0] == "clip" else text.encode()
            if subprocess.run(cmd, input=data).returncode == 0:
                return cmd[0]
    # OSC 52: most modern terminals (and SSH sessions, e.g. to a Raspberry Pi) accept this.
    sys.stdout.write("\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a")
    sys.stdout.flush()
    return "terminal (OSC 52)"


def zerotier_cli() -> str | None:
    found = shutil.which("zerotier-cli")
    win = r"C:\ProgramData\ZeroTier\One\zerotier-cli.bat"
    return found or (win if os.name == "nt" and os.path.exists(win) else None)


class SlashCompleter(Completer):
    def __init__(self, repl: "Repl"):
        self.repl = repl

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for cmd, desc in COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)
            return
        cmd, _, rest = text.partition(" ")
        words = rest.split(" ")
        partial = words[-1]
        if len(words) == 1:
            if cmd in ("/server", "/remove_api_server"):
                names = self.repl.server_names()
            elif cmd == "/model":
                names = self.repl.cached_models
            else:
                names = SUBCOMMANDS.get(cmd, [])
        elif len(words) == 2 and cmd == "/config" and words[0] == "set":
            names = list(SETTABLE)
        else:
            return
        for name in names:
            if name.startswith(partial):
                yield Completion(name, start_position=-len(partial))


def _key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    return kb


class Repl:
    def __init__(self, agent: Agent, store: ServerStore, server: Server, yolo: bool = False):
        self.agent = agent
        self.ui = agent.ui
        self.store = store
        self.server = server
        self.server_ok: bool | None = None
        self.cached_models: list[str] = []
        self.branch = git_branch(agent.root)
        self.update_available: updater.Release | None = None
        self.pending_context: list[str] = []
        self._ctx = (0, 1)
        self.session: PromptSession | None = None
        agent.permissions.yolo = yolo
        self.handlers: dict[str, Callable[[str], bool | None]] = {
            "/help": self.cmd_help,
            "/clear": self.cmd_clear,
            "/retry": self.cmd_retry,
            "/undo": self.cmd_undo,
            "/compact": self.cmd_compact,
            "/context": self.cmd_context,
            "/copy": self.cmd_copy,
            "/export": self.cmd_export,
            "/resume": self.cmd_resume,
            "/save": self.cmd_save,
            "/model": self.cmd_model,
            "/models": self.cmd_model,
            "/servers": self.cmd_servers,
            "/server": self.cmd_server,
            "/add_api_server": self.cmd_add_api_server,
            "/remove_api_server": self.cmd_remove_api_server,
            "/diff": self.cmd_diff,
            "/init": self.cmd_init,
            "/memory": self.cmd_memory,
            "/cwd": self.cmd_cwd,
            "/config": self.cmd_config,
            "/permissions": self.cmd_permissions,
            "/yolo": self.cmd_yolo,
            "/tools": self.cmd_tools,
            "/status": self.cmd_status,
            "/doctor": self.cmd_doctor,
            "/version": self.cmd_version,
            "/update": self.cmd_update,
            "/exit": self.cmd_exit,
            "/quit": self.cmd_exit,
        }

    @property
    def yolo(self) -> bool:
        return self.agent.permissions.yolo

    # --- helpers ------------------------------------------------------------------
    def server_names(self) -> list[str]:
        return sorted(self.store.servers)

    def refresh_ctx(self) -> None:
        self._ctx = (self.agent.tokens_used(), self.agent.num_ctx)

    def check_server(self, quiet: bool = False) -> Ping:
        if quiet:
            p = ping(self.server)
        else:
            with self.ui.spinner(f"Connecting to {self.server.name} ({self.server.address})…"):
                p = ping(self.server)
        self.server_ok = p.ok
        if p.ok:
            self.cached_models = [strip_latest(m) for m in p.models]
        return p

    def _ask_text(self, label: str) -> str:
        try:
            return ui.console.input(f"  [bold]{label}[/]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def _confirm(self, question: str) -> bool:
        return ui.select(question, [("y", "Yes"), ("n", "No")], cancel="n") == "y"

    def _open_editor(self, path: Path) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
        subprocess.run([*shlex.split(editor, posix=os.name != "nt"), str(path)])

    def toolbar(self) -> FormattedText:
        used, limit = self._ctx
        pct = min(100, used * 100 // max(limit, 1))
        filled = min(10, round(pct / 10))
        level = "hi" if pct >= 80 else "mid" if pct >= 60 else "lo"
        dot = "ok" if self.server_ok else "bad" if self.server_ok is False else "unk"
        where = short_path(self.agent.root) + (f" ⎇ {self.branch}" if self.branch else "")
        parts = [
            ("class:tb.model", f" ◆ {self.agent.model} "),
            ("class:tb.sep", " │ "),
            (f"class:tb.{dot}", "● "),
            ("class:tb.text", f"{self.server.name} "),
            ("class:tb.addr", self.server.address),
            ("class:tb.sep", " │ "),
            (f"class:tb.ctx-{level}", f"{'▰' * filled}{'▱' * (10 - filled)} {pct}%"),
            ("class:tb.sep", " │ "),
            ("class:tb.text", where),
        ]
        if self.yolo:
            parts += [("class:tb.sep", " │ "), ("class:tb.yolo", "YOLO")]
        if self.update_available:
            parts += [("class:tb.sep", " │ "), ("class:tb.update", f"⬆ v{self.update_available.version}")]
        return FormattedText(parts)

    # --- startup / updates --------------------------------------------------------
    def check_for_update(self) -> str | None:
        """Startup update check. May install and restart when auto_update = "auto". Returns a banner notice."""
        if os.environ.pop("QCODE_JUST_UPDATED", None):
            return f"Updated to v{__version__}"
        mode = self.agent.config.auto_update
        if mode == "off":
            return None
        release = updater.cached_latest()
        if not updater.is_newer(release):
            return None
        kind, _ = updater.install_kind()
        if mode == "auto" and kind == "pip":
            with self.ui.spinner(f"Updating qcode to v{release.version}…"):
                ok, _out = updater.install(release)
            if ok:
                updater.restart()
            self.ui.warn(f"Automatic update to v{release.version} failed; try /update.")
        self.update_available = release
        return f"qcode v{release.version} is available (you have v{__version__}) · /update"

    def start(self) -> None:
        notice = self.check_for_update()
        p = self.check_server()
        self.refresh_ctx()
        ui.banner(ui.BannerInfo(
            model=self.agent.model,
            num_ctx=self.agent.num_ctx,
            server_name=self.server.name,
            server_address=self.server.address,
            online=p.ok,
            latency_ms=p.latency_ms,
            error=p.error,
            project=short_path(self.agent.root),
            branch=self.branch,
            version=__version__,
            notice=notice,
        ))
        if not p.ok:
            self.ui.warn(f"Can't reach {self.server.name} ({self.server.address}): {p.error}. Check ZeroTier, or use /servers.")
        elif strip_latest(self.agent.model) not in self.cached_models:
            self.ui.warn(f"{self.agent.model} isn't on {self.server.name}. Pick one with /model.")

    # --- main loop ----------------------------------------------------------------
    def run(self) -> None:
        self.start()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.session = PromptSession(
            history=FileHistory(str(DATA_DIR / "history")),
            completer=SlashCompleter(self),
            key_bindings=_key_bindings(),
            complete_while_typing=True,
            bottom_toolbar=self.toolbar,
            style=PT_STYLE,
            placeholder=FormattedText([("class:placeholder", "Ask anything · / for commands · ! for shell")]),
            reserve_space_for_menu=8,
        )
        while True:
            try:
                line = self.session.prompt(FormattedText([("class:prompt", "❯ ")])).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("!"):
                self.run_shell(line[1:].strip())
                continue
            if line.startswith("/"):
                cmd, _, arg = line.partition(" ")
                handler = self.handlers.get(cmd)
                if handler is None:
                    self.ui.warn(f"Unknown command {cmd}. Try /help.")
                elif handler(arg.strip()) is False:
                    break
                self.refresh_ctx()
                continue
            self.ask(line)
        ui.console.print(Text("  bye 👋", style=ui.MUTED))

    def ask(self, text: str) -> None:
        label = text
        if self.pending_context:
            text = "\n\n".join(self.pending_context) + "\n\n" + text
            self.pending_context.clear()
        try:
            self.agent.run(text, label=label)
            self.server_ok = True
        except APIConnectionError:
            self.server_ok = False
            self.ui.error(f"Can't reach {self.server.name} at {self.server.address}. Is ZeroTier up? Try /servers.")
        except APIError as e:
            self.ui.error(f"Server error: {getattr(e, 'message', e)}")
        finally:
            self.refresh_ctx()

    def run_shell(self, command: str) -> None:
        if not command:
            self.ui.warn("Type a command after !, e.g. !git status")
            return
        ui.console.print(Text.assemble(("  $ ", f"bold {ui.WARN}"), (command, "bold")))
        try:
            proc = subprocess.run(command, shell=True, cwd=self.agent.root, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            out, code = (proc.stdout + proc.stderr).rstrip(), proc.returncode
        except subprocess.TimeoutExpired:
            out, code = "(timed out after 300s)", -1
        except KeyboardInterrupt:
            self.ui.warn("Interrupted.")
            return
        lines = out.splitlines()
        shown = "\n".join(lines[:200]) + (f"\n… {len(lines) - 200} more lines" if len(lines) > 200 else "")
        if shown:
            ui.console.print(Text(shown, style="#cdd6f4"))
        if code != 0:
            ui.console.print(Text(f"  exit code {code}", style=ui.WARN))
        self.pending_context.append(
            f"I ran this shell command myself:\n$ {command}\n(exit code {code})\n{truncate(out, self.agent.config.tool_output_limit)}"
        )
        self.ui.info("Output will be included with your next message.")

    # --- conversation commands ----------------------------------------------------
    def cmd_help(self, _: str) -> None:
        ui.print_help(COMMAND_GROUPS)
        ui.console.print(Text("  !<command> runs a shell command here; its output goes along with your next message.", style=ui.MUTED))

    def cmd_clear(self, _: str) -> None:
        self.agent.reset()
        self.pending_context.clear()
        ui.console.clear()
        self.ui.ok("Started a new conversation.")

    def cmd_retry(self, _: str) -> None:
        text = self.agent.pop_last_turn()
        if text is None:
            self.ui.info("Nothing to retry yet.")
            return
        if self.agent.checkpoints and text.endswith(self.agent.checkpoints[-1].prompt):
            self.ui.info("Files changed by the previous attempt are kept. Use /undo first to revert them.")
        preview = text.strip().splitlines()[0][:80] if text.strip() else ""
        self.ui.info(f"Retrying: {preview}")
        self.ask(text)

    def cmd_undo(self, _: str) -> None:
        if not self.agent.checkpoints:
            self.ui.info("Nothing to undo: qcode hasn't changed any files in this session.")
            return
        cp = self.agent.checkpoints[-1]
        first = cp.prompt.strip().splitlines()[0][:80] if cp.prompt.strip() else ""
        ui.console.print(Text(f"\n  Undo the changes from: {first}", style="bold"))
        for path, old in cp.files.items():
            p = Path(path)
            rel = p.relative_to(self.agent.root).as_posix() if p.is_relative_to(self.agent.root) else path
            verb, color = ("delete", ui.ERR) if old is None else ("restore", ui.WARN)
            ui.console.print(Text.assemble((f"    {verb:<8}", color), (rel, "")))
        self.ui.info("Commands run with bash aren't undone.")
        if not self._confirm("Revert these files?"):
            return
        self.agent.undo()
        names = ", ".join(Path(p).name for p in cp.files)
        self.pending_context.append(f"(Note: I undid your file changes from my earlier request; these files were restored: {names}.)")
        self.ui.ok(f"Reverted {len(cp.files)} file{'s' if len(cp.files) != 1 else ''}.")

    def cmd_compact(self, _: str) -> None:
        before = self.agent.tokens_used()
        try:
            with self.ui.spinner("Compacting conversation…"):
                self.agent.compact()
        except APIError as e:
            self.ui.error(f"Compaction failed: {getattr(e, 'message', e)}")
            return
        self.agent.save()
        self.ui.ok(f"Compacted ~{before:,} → ~{self.agent.tokens_used():,} tokens.")

    def cmd_context(self, _: str) -> None:
        ui.print_context(self.agent.context_breakdown(), self.agent.num_ctx)

    def cmd_copy(self, _: str) -> None:
        text = self.agent.last_reply()
        if not text:
            self.ui.info("No reply to copy yet.")
            return
        how = copy_to_clipboard(text)
        self.ui.ok(f"Copied the last reply ({len(text):,} characters) via {how}.")

    def cmd_export(self, arg: str) -> None:
        if len(self.agent.messages) <= 1:
            self.ui.info("Nothing to export yet.")
            return
        name = arg or f"qcode-session-{datetime.datetime.now():%Y%m%d-%H%M}.md"
        path = Path(name).expanduser()
        path = path if path.is_absolute() else self.agent.root / path
        path.write_text(self.agent.export_markdown(), encoding="utf-8")
        self.ui.ok(f"Exported to {short_path(path)}")

    def cmd_save(self, _: str) -> None:
        self.agent.save()
        self.ui.ok(f"Saved to {short_path(self.agent.session_path)}")

    def cmd_resume(self, arg: str) -> None:
        found = sessions.list_sessions(self.agent.root)
        if not found:
            self.ui.info("No saved sessions for this folder.")
            return
        if not arg:
            ui.console.print()
            for i, s in enumerate(found, 1):
                ui.console.print(Text.assemble(
                    (f"  {i:>2}  ", f"bold {ui.ACCENT}"), (f"{s.updated.replace('T', ' ')}  ", ui.MUTED),
                    (f"{s.model:<12} ", ui.ACCENT2), (s.first_prompt, ""),
                ))
            self.ui.info("/resume <n> to load one")
            return
        if not arg.isdigit() or not 1 <= int(arg) <= len(found):
            self.ui.warn(f"Pick a number from 1 to {len(found)}.")
            return
        self.agent.load_session(found[int(arg) - 1].path)
        n = sum(1 for m in self.agent.messages if m["role"] == "user")
        self.ui.ok(f"Resumed session with {n} messages (model {self.agent.model}).")

    # --- model & server commands --------------------------------------------------
    def cmd_model(self, arg: str) -> None:
        try:
            with self.ui.spinner("Fetching models…"):
                available = [strip_latest(m) for m in self.agent.client.list_models()]
                rows = [(m == strip_latest(self.agent.model), m, self.agent.context_for(m)) for m in available]
        except APIError as e:
            self.ui.error(f"Couldn't list models on {self.server.name}: {getattr(e, 'message', e)}")
            return
        self.cached_models = available
        if not arg:
            ui.print_models(rows, f"{self.server.name} ({self.server.address})")
            return
        if strip_latest(arg) not in available:
            self.ui.warn(f"{arg} isn't on {self.server.name}. Available: {', '.join(available)}")
            return
        self.agent.model = strip_latest(arg)
        self.ui.ok(f"Switched to {self.agent.model} · {self.agent.num_ctx:,} context tokens")
        if self.agent.tokens_used() >= self.agent.num_ctx * 0.8:
            self.cmd_compact("")

    def cmd_servers(self, _: str) -> None:
        servers = list(self.store.servers.values())
        if self.server.name not in self.store.servers:  # temporary --base-url server
            servers.insert(0, self.server)
        with self.ui.spinner("Checking servers…"), ThreadPoolExecutor(max_workers=8) as pool:
            pings = list(pool.map(ping, servers))
        for s, p in zip(servers, pings):
            if s.name == self.server.name:
                self.server_ok = p.ok
        ui.print_servers([(s.name == self.server.name, s, p) for s, p in zip(servers, pings)])

    def cmd_server(self, arg: str) -> None:
        if not arg:
            self.cmd_servers("")
            return
        server = self.store.get(arg)
        if server is None:
            self.ui.warn(f"No server named {arg}. Known: {', '.join(self.server_names())}. Add one with /add_api_server.")
            return
        self.store.use(arg)
        self.switch_server(server)

    def switch_server(self, server: Server) -> None:
        self.server = server
        self.agent.client = LLMClient(server.base_url, server.api_key)
        p = self.check_server()
        if not p.ok:
            self.ui.warn(f"Switched to {server.name} ({server.address}), but it isn't responding ({p.error}).")
            return
        current = strip_latest(self.agent.model)
        if current not in self.cached_models and self.cached_models:
            preferred = strip_latest(self.agent.config.model)
            pick = preferred if preferred in self.cached_models else self.cached_models[0]
            self.agent.model = pick
            self.ui.info(f"{current} isn't on {server.name}; using {pick}.")
        self.ui.ok(f"Now using {server.name} ({server.address}) · {len(p.models)} models · {p.latency_ms}ms")

    def cmd_add_api_server(self, arg: str) -> None:
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            self.ui.error(str(e))
            return
        if len(parts) > 3:
            self.ui.warn("Usage: /add_api_server <name> <ip|host:port|url> [api_key]")
            return
        name = parts[0] if parts else self._ask_text("Name (e.g. home, laptop, gpu-box)")
        address = parts[1] if len(parts) > 1 else self._ask_text("Address (IP, host:port or full URL)")
        api_key = parts[2] if len(parts) > 2 else "ollama"
        if not name or not address:
            self.ui.info("Cancelled.")
            return
        existed = name in self.store.servers
        try:
            server = self.store.add(name, address, api_key)
        except ValueError as e:
            self.ui.error(str(e))
            return
        with self.ui.spinner(f"Checking {server.address}…"):
            p = ping(server)
        self.ui.ok(f"{'Updated' if existed else 'Added'} server {name} → {server.base_url}")
        if p.ok:
            self.ui.info(f"● online · {p.latency_ms}ms · {len(p.models)} models: {', '.join(strip_latest(m) for m in p.models[:6])}{'…' if len(p.models) > 6 else ''}")
        else:
            self.ui.warn(f"Saved, but it isn't responding ({p.error}). Is the server running and listening on 0.0.0.0?")
        if name == self.server.name:
            self.switch_server(server)  # the active server's address changed
        elif p.ok and self._confirm(f"Switch to {name} now?"):
            self.store.use(name)
            self.switch_server(server)

    def cmd_remove_api_server(self, arg: str) -> None:
        if not arg:
            self.ui.warn("Usage: /remove_api_server <name>")
            return
        try:
            self.store.remove(arg)
        except KeyError:
            self.ui.warn(f"No server named {arg}.")
            return
        except ValueError as e:
            self.ui.warn(str(e))
            return
        self.ui.ok(f"Removed {arg}.")
        if arg == self.server.name:
            self.switch_server(self.store.current)

    # --- project commands ---------------------------------------------------------
    def cmd_diff(self, _: str) -> None:
        if not shutil.which("git"):
            self.ui.warn("git isn't installed.")
            return
        root = str(self.agent.root)

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)

        if git("rev-parse", "--is-inside-work-tree").returncode != 0:
            self.ui.info("This folder isn't a git repository.")
            return
        has_head = git("rev-parse", "--verify", "HEAD").returncode == 0
        diff = git("diff", *(["HEAD"] if has_head else []), "--no-color").stdout
        untracked = git("ls-files", "--others", "--exclude-standard").stdout.split()
        if not diff.strip() and not untracked:
            self.ui.ok("No uncommitted changes.")
            return
        stat = git("diff", *(["HEAD"] if has_head else []), "--stat", "--no-color").stdout.strip()
        if stat:
            ui.console.print(Text("\n" + stat, style=ui.MUTED))
        if diff.strip():
            lines = diff.splitlines()
            ui.console.print(Syntax("\n".join(lines[:400]), "diff", theme=ui.CODE_THEME, background_color="default", word_wrap=True))
            if len(lines) > 400:
                self.ui.info(f"… {len(lines) - 400} more lines (run !git diff for everything)")
        if untracked:
            ui.console.print(Text("\n  New files (untracked):", style="bold"))
            for f in untracked[:30]:
                ui.console.print(Text(f"    + {f}", style=ui.OK))
            if len(untracked) > 30:
                self.ui.info(f"… and {len(untracked) - 30} more")

    def cmd_init(self, _: str) -> None:
        self.ask(INIT_PROMPT)
        self.agent.refresh_system()

    def cmd_memory(self, _: str) -> None:
        path = project_memory_path(self.agent.root)
        if not path.exists():
            path.write_text("# Project notes for qcode\n\n")
        self._open_editor(path)
        self.agent.refresh_system()
        self.ui.ok(f"Reloaded {path.name}.")

    def cmd_cwd(self, _: str) -> None:
        ui.console.print(Text(f"  {self.agent.root}"))

    # --- settings commands --------------------------------------------------------
    def cmd_config(self, arg: str) -> None:
        parts = arg.split(maxsplit=2)
        cfg = self.agent.config
        if not parts:
            rows = [(key, str(getattr(cfg, key)), desc) for key, (_, desc) in SETTABLE.items()]
            rows += [
                ("allow rules", f"{len(cfg.allow)} in config", "see /permissions"),
                ("servers", f"{len(self.store.servers)} saved", "see /servers"),
            ]
            ui.print_table(
                [("setting", {"style": f"bold {ui.ACCENT}", "no_wrap": True}), ("value", {"style": ui.ACCENT2}), ("", {"style": ui.MUTED})],
                rows,
                footer="/config set <key> <value> · /config edit opens config.toml · /config path lists files",
            )
            return
        sub = parts[0]
        if sub == "set":
            if len(parts) < 3:
                self.ui.warn(f"Usage: /config set <key> <value>   keys: {', '.join(SETTABLE)}")
                return
            key, raw = parts[1], parts[2]
            try:
                value = parse_setting(key, raw)
            except ValueError as e:
                self.ui.warn(str(e))
                return
            set_config_value(key, value)
            setattr(cfg, key, value)
            self.ui.ok(f"{key} = {value}  (saved to {short_path(CONFIG_FILE)})")
            if key == "model" and strip_latest(value) != strip_latest(self.agent.model):
                self.ui.info(f"That's the default for new sessions. Use /model {value} to switch right now.")
        elif sub == "edit":
            self._open_editor(CONFIG_FILE)
            try:
                new = load_config()
            except Exception as e:
                self.ui.error(f"config.toml has an error, so it wasn't reloaded: {e}")
                return
            current_model = self.agent.model
            self.agent.config = new
            self.agent.model = current_model
            self.agent.permissions.config_allow = list(new.allow)
            self.ui.ok("Reloaded config.toml.")
        elif sub == "path":
            for label, path in [
                ("config", CONFIG_FILE),
                ("servers", SERVERS_FILE),
                ("project settings", project_settings_file(self.agent.root)),
                ("project memory", project_memory_path(self.agent.root)),
                ("sessions", sessions.SESSIONS_DIR),
            ]:
                missing = "" if path.exists() else "  (not created yet)"
                ui.console.print(Text.assemble((f"  {label:<17}", ui.MUTED), (str(path), ""), (missing, ui.MUTED)))
        else:
            self.ui.warn("Usage: /config · /config set <key> <value> · /config edit · /config path")

    def cmd_permissions(self, arg: str) -> None:
        perms = self.agent.permissions
        rules = perms.rules()
        parts = arg.split()
        if not parts:
            if not rules:
                self.ui.info("No saved approvals. Read-only tools never ask; everything else asks first.")
            else:
                ui.print_table(
                    [("#", {"justify": "right", "style": ui.MUTED}), ("source", {"style": ui.ACCENT2}), ("allowed without asking", {})],
                    [(str(i), src, rule) for i, (src, rule) in enumerate(rules, 1)],
                    footer="/permissions remove <n> · /permissions reset (clears project + session) · config rules: /config edit",
                )
            if perms.yolo:
                self.ui.warn("YOLO is on: every tool call is approved automatically (/yolo to turn off).")
            return
        if parts[0] == "reset":
            if self._confirm("Forget all project and session approvals?"):
                n = perms.reset()
                self.ui.ok(f"Cleared {n} approval{'s' if n != 1 else ''}.")
        elif parts[0] == "remove" and len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(rules):
            src, rule = rules[int(parts[1]) - 1]
            if src == "config":
                self.ui.warn("That rule lives in config.toml; remove it with /config edit.")
            elif perms.remove(rule):
                self.ui.ok(f"Removed: {rule}")
        else:
            self.ui.warn("Usage: /permissions · /permissions remove <n> · /permissions reset")

    def cmd_yolo(self, _: str) -> None:
        perms = self.agent.permissions
        perms.yolo = not perms.yolo
        if perms.yolo:
            self.ui.warn("YOLO on: qcode will edit files and run commands without asking. /yolo again to turn it off.")
        else:
            self.ui.ok("YOLO off: qcode asks before edits and commands again.")

    def cmd_tools(self, _: str) -> None:
        rows = []
        for t in self.agent.tools.values():
            if t.read_only:
                access = Text("read-only", style=ui.OK)
            elif self.yolo:
                access = Text("auto (yolo)", style=ui.ERR)
            else:
                access = Text("asks first", style=ui.WARN)
            rows.append((Text(ui.TOOL_LABELS.get(t.name, t.name), style="bold"), Text(t.name, style=ui.MUTED), access, t.description))
        ui.print_table([("tool", {"no_wrap": True}), ("name", {"no_wrap": True}), ("access", {"no_wrap": True}), ("what it does", {})], rows)

    # --- app commands -------------------------------------------------------------
    def cmd_status(self, _: str) -> None:
        a = self.agent
        p = self.check_server(quiet=True)
        used, limit = a.tokens_used(), a.num_ctx
        state = Text(f"● online · {p.latency_ms}ms · {len(p.models)} models", style=ui.OK) if p.ok else Text(f"○ {p.error}", style=ui.ERR)
        ui.print_status([
            ("model", Text.assemble((a.model, f"bold {ui.ACCENT}"), (f"  {limit:,} ctx", ui.MUTED))),
            ("server", Text.assemble((self.server.name, "bold"), ("  ", ""), (self.server.base_url, ui.ACCENT2))),
            ("status", state),
            ("context", Text(f"~{used:,} / {limit:,} tokens ({used * 100 // max(limit, 1)}%)")),
            ("project", Text(short_path(a.root) + (f"  ⎇ {self.branch}" if self.branch else ""))),
            ("session", Text(short_path(a.session_path), style=ui.MUTED)),
            ("approvals", Text("YOLO, no prompts", style=ui.ERR) if self.yolo else Text("ask before writes and commands")),
            ("version", Text(f"v{__version__}")),
        ])

    def cmd_doctor(self, _: str) -> None:
        rows: list[tuple[str, str, str]] = []
        v = sys.version_info[:3]
        rows.append(("bad" if (3, 10, 0) <= v < (3, 10, 1) else "ok", "Python", f"{platform.python_version()} · {sys.executable}"))
        kind, where = updater.install_kind()
        rows.append(("ok", "qcode", f"v{__version__} · {kind} install · {where}"))
        with self.ui.spinner("Running checks…"):
            release = updater.fetch_latest(timeout=4)
            servers = list(self.store.servers.values())
            with ThreadPoolExecutor(max_workers=8) as pool:
                pings = dict(zip((s.name for s in servers), pool.map(ping, servers)))
            current = pings.get(self.server.name) or ping(self.server)
            probe = getattr(self.agent.client, "context_length", None)
            server_ctx = probe(self.agent.model) if (current.ok and probe) else None
        if release is None:
            rows.append(("warn", "updates", f"couldn't check GitHub ({updater.REPO})"))
        elif updater.is_newer(release):
            rows.append(("warn", "updates", f"v{release.version} is available · /update"))
        else:
            rows.append(("ok", "updates", "up to date"))
        try:
            load_config()
            rows.append(("ok", "config", short_path(CONFIG_FILE)))
        except Exception as e:
            rows.append(("bad", "config", f"{short_path(CONFIG_FILE)}: {e}"))
        for s in servers:
            p = pings[s.name]
            active = " (active)" if s.name == self.server.name else ""
            detail = f"online {p.latency_ms}ms, {len(p.models)} models" if p.ok else p.error
            rows.append(("ok" if p.ok else "bad", "server", f"{s.name}{active} · {s.base_url} · {detail}"))
        if current.ok:
            have = strip_latest(self.agent.model) in [strip_latest(m) for m in current.models]
            rows.append(("ok" if have else "bad", "model", self.agent.model + ("" if have else f" isn't on {self.server.name} · /model")))
            where_from = "(reported by the server)" if server_ctx else '(fallback; set [models."<name>"] num_ctx in config.toml)'
            rows.append(("ok" if server_ctx else "warn", "context", f"{self.agent.num_ctx:,} tokens {where_from}"))
        zt = zerotier_cli()
        rows.append(("ok" if zt else "info", "ZeroTier", "installed" if zt else "not installed (only needed for ZeroTier servers)"))
        git = shutil.which("git")
        rows.append(("ok" if git else "warn", "git", git or "not installed (/diff needs it)"))
        rg = shutil.which("rg")
        rows.append(("ok" if rg else "info", "ripgrep", rg or "not installed (search falls back to Python, slower on big repos)"))
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            probe_file = DATA_DIR / ".write-test"
            probe_file.write_text("ok")
            probe_file.unlink()
            rows.append(("ok", "data dir", short_path(DATA_DIR)))
        except OSError as e:
            rows.append(("bad", "data dir", f"{short_path(DATA_DIR)} isn't writable: {e}"))
        rows.append(("info", "terminal", f"{ui.console.color_system or 'no'} colors · {ui.console.width}×{ui.console.height}"))
        ui.print_checks(rows)

    def cmd_version(self, _: str) -> None:
        kind, where = updater.install_kind()
        ui.console.print(Text.assemble(("  qcode ", "bold"), (f"v{__version__}", f"bold {ui.ACCENT}"), (f"  ·  Python {platform.python_version()}  ·  {kind} install", ui.MUTED)))
        ui.console.print(Text(f"  {where}", style=ui.MUTED))
        if self.update_available:
            ui.console.print(Text(f"  v{self.update_available.version} is available · /update", style=ui.ACCENT2))

    def cmd_update(self, _: str) -> None:
        with self.ui.spinner(f"Checking github.com/{updater.REPO} for updates…"):
            release = updater.fetch_latest()
        if release is None:
            self.ui.warn("Couldn't reach GitHub, or there are no releases yet.")
            return
        if not updater.is_newer(release):
            self.update_available = None
            self.ui.ok(f"You're on the latest version (v{__version__}).")
            return
        self.update_available = release
        ui.print_release(__version__, release.version, release.notes)
        kind, where = updater.install_kind()
        if kind == "editable":
            self.ui.info(f"This is a development install. Update it with:  git -C {where} pull")
            return
        if not self._confirm(f"Install v{release.version} now?"):
            return
        with self.ui.spinner(f"Installing v{release.version}…"):
            ok, out = updater.install(release)
        if not ok:
            self.ui.error("Update failed:")
            ui.console.print(Text(out, style=ui.MUTED))
            return
        self.agent.save()
        self.update_available = None
        self.ui.ok(f"Installed v{release.version}.")
        if self._confirm("Restart qcode now? Your conversation is kept."):
            updater.restart(["--resume"])
        self.ui.info("Restart qcode to start using the new version.")

    def cmd_exit(self, _: str) -> bool:
        return False
