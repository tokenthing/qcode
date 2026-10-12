"""Terminal rendering: theme, banner, streaming replies, tool calls, diffs, permission menu, tables."""

from __future__ import annotations

import json
import random
import re
import sys
import time
from dataclasses import dataclass

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .toolparse import strip_tool_markup

ACCENT = "#c084fc"   # violet
ACCENT2 = "#7dd3fc"  # sky
OK = "#4ade80"
WARN = "#fbbf24"
ERR = "#f87171"
MUTED = "#7f849c"
ADD_BG = "#16341f"
DEL_BG = "#3d1a1e"
CODE_THEME = "monokai"

console = Console(
    theme=Theme({
        "markdown.code": ACCENT2,
        "markdown.link": ACCENT2,
        "markdown.h1": f"bold {ACCENT}",
        "markdown.h2": f"bold {ACCENT}",
        "markdown.h3": f"bold {ACCENT2}",
        "markdown.item.bullet": ACCENT,
        "markdown.item.number": ACCENT,
        "markdown.block_quote": MUTED,
    }),
    highlight=False,
)

LOGO = [
    "█▀▀█ █▀▀ █▀▀█ █▀▀▄ █▀▀",
    "█▄▄█ █   █  █ █  █ █▀▀",
    "   █ ▀▀▀ ▀▀▀▀ ▀▀▀  ▀▀▀",
]

TOOL_LABELS = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Update",
    "list_dir": "List",
    "glob": "Glob",
    "grep": "Search",
    "bash": "Bash",
}

VERBS = ["Thinking", "Pondering", "Cooking", "Brewing", "Tinkering", "Crunching", "Scheming", "Noodling", "Mulling", "Conjuring"]


# --- helpers ----------------------------------------------------------------------
def _hex(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def gradient(line: str, start: str = ACCENT, end: str = ACCENT2, width: int | None = None) -> Text:
    a, b = _hex(start), _hex(end)
    width = width or max(len(line), 1)
    text = Text()
    for i, ch in enumerate(line):
        t = i / max(width - 1, 1)
        r, g, bl = (round(a[k] + (b[k] - a[k]) * t) for k in range(3))
        text.append(ch, style=f"bold #{r:02x}{g:02x}{bl:02x}")
    return text


def tool_target(name: str, args: dict) -> str:
    if name == "bash":
        cmd = " ".join(args.get("command", "").split())
        return cmd if len(cmd) <= 90 else cmd[:87] + "…"
    if "path" in args:
        extra = f", from line {args['offset']}" if args.get("offset") else ""
        return f"{args['path']}{extra}"
    if "pattern" in args:
        where = f" in {args['path']}" if args.get("path") else ""
        return f"{args['pattern']}{where}"
    return json.dumps(args)[:90] if args else ""


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def diff_stats(diff: str) -> tuple[int, int]:
    lines = diff.splitlines()
    adds = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return adds, dels


def render_diff(diff: str, max_lines: int = 60, indent: int = 5) -> RenderableType:
    width = max(console.width - indent - 2, 40)
    rows: list[Text] = []
    old = new = 0
    body = [l for l in diff.splitlines() if not l.startswith(("---", "+++"))]
    for idx, line in enumerate(body):
        if len(rows) >= max_lines:
            rows.append(Text(f"     … {len(body) - idx} more lines", style=MUTED))
            break
        m = HUNK_RE.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            if rows:
                rows.append(Text("     ⋮", style=MUTED))
            continue
        if line.startswith("+"):
            rows.append(Text(f"{new:>4} + {line[1:]}".ljust(width), style=f"#d6f5dd on {ADD_BG}"))
            new += 1
        elif line.startswith("-"):
            rows.append(Text(f"{old:>4} - {line[1:]}".ljust(width), style=f"#f8d7da on {DEL_BG}"))
            old += 1
        elif line.startswith("\\"):
            continue
        else:
            rows.append(Text(f"{new:>4}   {line[1:]}", style=MUTED))
            old += 1
            new += 1
    return Group(*rows) if rows else Text("(no changes)", style=MUTED)


def _assistant(md: str) -> Table:
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text("⏺", style="bold white"), Markdown(md, code_theme=CODE_THEME))
    return grid


def _lexer_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"py": "python", "js": "javascript", "ts": "typescript", "html": "html", "htm": "html", "css": "css",
            "json": "json", "md": "markdown", "sh": "bash", "toml": "toml", "yml": "yaml", "yaml": "yaml",
            "java": "java", "rs": "rust", "go": "go", "c": "c", "cpp": "cpp", "kt": "kotlin"}.get(ext, "text")


MODE_LABELS = {
    "ask": ("⏵ ask", MUTED),
    "auto-edit": ("⏵⏵ auto-edit", WARN),
    "auto": ("⚡ auto", ERR),
    "plan": ("⏸ plan", ACCENT2),
}
MODE_HELP = {
    "ask": "asks before every file edit and command",
    "auto-edit": "file edits happen automatically; commands still ask",
    "auto": "everything happens automatically, no questions",
    "plan": "read-only: qcode looks around and proposes a plan, changes nothing",
}


class _Status:
    """Animated 'Thinking… (4s · ↓ 120 tokens)' line, re-rendered by Live."""

    def __init__(self) -> None:
        self.verb = random.choice(VERBS)
        self.start = time.monotonic()
        self.tokens = 0
        self.spinner = Spinner("dots", style=ACCENT)

    def __rich__(self) -> RenderableType:
        elapsed = int(time.monotonic() - self.start)
        detail = f"{elapsed}s" + (f" · ↓ {self.tokens} tokens" if self.tokens else "") + " · ctrl+c to interrupt"
        self.spinner.text = Text.assemble((f"{self.verb}…", f"bold {ACCENT}"), (f"  ({detail})", MUTED))
        return self.spinner


# --- interactive selection ----------------------------------------------------------
def select(question: str, options: list[tuple[str, str]], cancel: str) -> str:
    """Arrow-key menu (numbers also work). Falls back to typed input when not on a TTY."""
    console.print(Text(question, style="bold"))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        for i, (_, label) in enumerate(options, 1):
            console.print(f"  {i}. {label}")
        try:
            raw = console.input("  choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return cancel
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        return raw if raw in dict(options) else cancel

    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style

    state = {"i": 0}
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        state["i"] = (state["i"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    @kb.add("tab")
    def _down(event):
        state["i"] = (state["i"] + 1) % len(options)

    @kb.add("enter")
    def _enter(event):
        event.app.exit(result=options[state["i"]][0])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=cancel)

    for n in range(len(options)):
        kb.add(str(n + 1))(lambda event, n=n: event.app.exit(result=options[n][0]))

    def render():
        out = []
        for i, (_, label) in enumerate(options):
            if i == state["i"]:
                out.append(("class:sel", f"  ❯ {i + 1}. {label}\n"))
            else:
                out.append(("class:opt", f"    {i + 1}. {label}\n"))
        out.append(("class:hint", "  ↑/↓ move · enter select · esc cancel"))
        return out

    app = Application(
        layout=Layout(Window(FormattedTextControl(render), always_hide_cursor=True)),
        key_bindings=kb,
        style=Style.from_dict({"sel": f"bold {ACCENT}", "opt": "#cdd6f4", "hint": MUTED}),
        full_screen=False,
        erase_when_done=True,
    )
    choice = app.run()
    label = dict(options)[choice]
    console.print(Text.assemble(("  ❯ ", ACCENT), (label, WARN if choice == cancel else ACCENT)))
    return choice


def text_input(label: str, default: str = "") -> str | None:
    """One-line input with a pre-filled default. None if cancelled."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            raw = console.input(f"  {label} [{default}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return raw or default
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.styles import Style

    try:
        return pt_prompt([("class:label", f"  {label}: ")], default=default,
                         style=Style.from_dict({"label": f"bold {ACCENT}"})).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def settings_menu(rows: list[tuple[str, str, str, str]], start: int = 0) -> str | None:
    """Arrow-key list of (key, label, value, help). Returns the chosen key, or None for Esc / Done."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        for i, (_, label, value, _h) in enumerate(rows, 1):
            console.print(f"  {i}. {label}: {value}")
        try:
            raw = console.input("  number to change (Enter to finish): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return rows[int(raw) - 1][0] if raw.isdigit() and 1 <= int(raw) <= len(rows) else None

    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style

    state = {"i": max(0, min(start, len(rows) - 1))}
    width = max(len(r[1]) for r in rows) + 2
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        state["i"] = (state["i"] - 1) % len(rows)

    @kb.add("down")
    @kb.add("j")
    @kb.add("tab")
    def _down(event):
        state["i"] = (state["i"] + 1) % len(rows)

    @kb.add("enter")
    @kb.add("space")
    def _pick(event):
        event.app.exit(result=rows[state["i"]][0])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("q")
    def _quit(event):
        event.app.exit(result=None)

    def render():
        out = [("class:title", "  Settings  "), ("class:hint", "↑/↓ move · enter change · esc done\n\n")]
        for i, (key, label, value, _h) in enumerate(rows):
            current = i == state["i"]
            out.append(("class:sel" if current else "class:opt", f"  {'❯' if current else ' '} {label.ljust(width)}"))
            out.append(("class:valsel" if current else "class:val", f"{value}\n"))
        out.append(("class:help", f"\n  {rows[state['i']][3]}"))
        return out

    app = Application(
        layout=Layout(Window(FormattedTextControl(render), always_hide_cursor=True)),
        key_bindings=kb,
        style=Style.from_dict({
            "title": f"bold {ACCENT}", "hint": MUTED, "sel": f"bold {ACCENT}", "opt": "#cdd6f4",
            "val": ACCENT2, "valsel": f"bold {ACCENT2}", "help": MUTED,
        }),
        full_screen=False,
        erase_when_done=True,
    )
    return app.run()


# --- the UI the agent talks to -------------------------------------------------------
@dataclass
class BannerInfo:
    model: str
    num_ctx: int
    server_name: str
    server_address: str
    online: bool
    latency_ms: int
    error: str
    project: str
    branch: str | None
    version: str
    notice: str | None = None
    mode: str = "ask"


class TerminalUI:
    def __init__(self) -> None:
        self._live: Live | None = None
        self._buf = ""
        self._status: _Status | None = None

    # streaming assistant text
    def stream_start(self) -> None:
        self._buf = ""
        self._status = _Status()
        self._live = Live(self._status, console=console, refresh_per_second=12, vertical_overflow="visible")
        self._live.start()

    def stream_text(self, text: str) -> None:
        self._buf += text
        if not (self._live and self._status):
            return
        self._status.tokens += 1
        shown = strip_tool_markup(self._buf)
        self._live.update(Group(Text(""), _assistant(shown), Text(""), self._status) if shown else self._status)

    def stream_end(self) -> None:
        if not self._live:
            return
        shown = strip_tool_markup(self._buf)
        if shown:
            self._live.update(Group(Text(""), _assistant(shown)), refresh=True)
        else:
            self._live.transient = True
        self._live.stop()
        self._live = None

    # tools
    def tool_call(self, name: str, args: dict) -> None:
        console.print()
        console.print(Text.assemble(
            ("⏺ ", ACCENT2),
            (TOOL_LABELS.get(name, name), "bold"),
            ("(", MUTED), (tool_target(name, args), "#cdd6f4"), (")", MUTED),
        ))

    def tool_result(self, name: str, args: dict, result: str, is_error: bool, diff: str | None = None) -> None:
        extra: RenderableType | None = None
        lines = result.splitlines()
        if is_error:
            summary = Text(result.removeprefix("Error: ").strip().split("\n")[0][:200], style=ERR)
        elif name == "read_file":
            n = sum(1 for l in lines if re.match(r"^\s*\d+\t", l))
            summary = Text(f"Read {n} line{'s' if n != 1 else ''}", style=MUTED)
        elif name in ("write_file", "edit_file"):
            summary = Text(result, style=MUTED)
            if diff:
                adds, dels = diff_stats(diff)
                summary.append(f"  +{adds}", style=OK)
                summary.append(f" −{dels}", style=ERR)
                extra = render_diff(diff, max_lines=30)
        elif name == "bash":
            first, rest = (lines[0], lines[1:]) if lines else ("", [])
            summary = Text(first, style=OK if first.startswith("exit code 0") else WARN)
            if rest:
                shown = "\n".join(rest[:6])
                more = f"\n… +{len(rest) - 6} lines" if len(rest) > 6 else ""
                extra = Text(shown + more, style=MUTED)
        elif name in ("grep", "glob"):
            n = 0 if result in ("No matches", "No files matched") else sum(1 for l in lines if not l.startswith("..."))
            noun = "match" if name == "grep" else "file"
            summary = Text(f"Found {n} {noun}{'es' if noun == 'match' and n != 1 else 's' if n != 1 else ''}", style=MUTED)
        elif name == "list_dir":
            summary = Text(f"Listed {len(lines)} entries", style=MUTED)
        else:
            summary = Text(lines[0] if lines else "", style=MUTED)
        console.print(Text.assemble(("  ⎿  ", MUTED), summary))
        if extra is not None:
            console.print(Padding(extra, (0, 0, 0, 5)))

    def tool_denied(self, name: str) -> None:
        console.print(Text.assemble(("  ⎿  ", MUTED), ("Denied. Tell qcode what to do instead.", WARN)))

    def ask_permission(self, name: str, args: dict, preview: str | None) -> str:
        label = TOOL_LABELS.get(name, name)
        title = {"bash": "Bash command", "write_file": "Write file", "edit_file": "Edit file"}.get(name, label)
        if name == "bash":
            body: RenderableType = Syntax(args.get("command", ""), "bash", theme=CODE_THEME, word_wrap=True, background_color="default")
        elif preview is not None:
            adds, dels = diff_stats(preview)
            head = Text.assemble((args.get("path", ""), "bold"), ("  +" + str(adds), OK), (" −" + str(dels), ERR))
            if name == "write_file" and dels >= 10 and dels > 2 * adds:
                new = args.get("content", "")
                new_lines = new.splitlines()
                shown = "\n".join(new_lines[:40]) + (f"\n… {len(new_lines) - 40} more lines" if len(new_lines) > 40 else "")
                body = Group(
                    head,
                    Text(""),
                    Text(f" ⚠  This REPLACES {dels} existing lines with {len(new_lines)}. Most of this file will be deleted. ",
                         style=f"bold #ffffff on {DEL_BG}"),
                    Text(""),
                    Text("New content:", style="bold"),
                    Syntax(shown or "(empty)", _lexer_for(args.get("path", "")), theme=CODE_THEME, background_color="default", word_wrap=True),
                    Text("Tip: choose No, then ask for an edit instead of a rewrite. /undo can also revert it later.", style=MUTED),
                )
            else:
                body = Group(head, Text(""), render_diff(preview, max_lines=80, indent=4))
        else:
            body = Text(json.dumps(args, indent=2))
        console.print(Panel(body, title=f"[bold {WARN}]{title}[/]", title_align="left", border_style=WARN, box=box.ROUNDED, padding=(0, 1)))
        always = "for this exact command" if name == "bash" else f"for all {label} calls"
        return select(
            "Do you want to proceed?",
            [
                ("y", "Yes"),
                ("a", f"Yes, and don't ask again {always} this session"),
                ("p", f"Yes, and don't ask again {always} in this project"),
                ("n", "No, and tell qcode what to do differently"),
            ],
            cancel="n",
        )

    # turn summary
    def turn_done(self, stats) -> None:
        parts = []
        if stats.tools:
            parts.append(f"{stats.tools} tool{'s' if stats.tools != 1 else ''}")
        parts.append(f"{stats.seconds:.1f}s")
        if stats.tokens and stats.gen_seconds > 0:
            parts.append(f"{stats.tokens / stats.gen_seconds:.0f} tok/s")
        pct = stats.ctx_used * 100 // max(stats.ctx_limit, 1)
        parts.append(f"ctx {pct}%")
        color = ERR if pct >= 80 else WARN if pct >= 60 else MUTED
        console.print(Text("✻ " + " · ".join(parts), style=color), justify="right")

    # messages
    def info(self, msg: str) -> None:
        console.print(Text(f"  {msg}", style=MUTED))

    def ok(self, msg: str) -> None:
        console.print(Text.assemble(("  ✓ ", OK), (msg, "")))

    def warn(self, msg: str) -> None:
        console.print(Text.assemble(("  ⚠ ", WARN), (msg, WARN)))

    def error(self, msg: str) -> None:
        console.print(Text.assemble(("  ✗ ", ERR), (msg, ERR)))

    def spinner(self, msg: str):
        return console.status(Text(msg, style=MUTED), spinner="dots", spinner_style=ACCENT)


# --- screens -------------------------------------------------------------------------
def banner(info: BannerInfo) -> None:
    width = max(len(l) for l in LOGO)
    logo = Text("\n").join(gradient(l, width=width) for l in LOGO)
    head = Table.grid(padding=(0, 3))
    head.add_column()
    head.add_column(vertical="middle")
    head.add_row(logo, Text.assemble(("local coding agent\n", "bold #cdd6f4"), (f"v{info.version}", MUTED)))

    status = (
        Text.assemble(("● ", OK), (f"online · {info.latency_ms}ms", OK))
        if info.online
        else Text.assemble(("○ ", ERR), (info.error or "offline", ERR))
    )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=MUTED, no_wrap=True)
    grid.add_column()
    grid.add_row("model", Text.assemble((info.model, f"bold {ACCENT}"), (f"  {info.num_ctx:,} ctx", MUTED)))
    grid.add_row("server", Text.assemble((info.server_name, "bold"), ("  ", ""), (info.server_address, ACCENT2), ("  ", ""), status))
    grid.add_row("project", Text.assemble((info.project, ""), (f"  ⎇ {info.branch}" if info.branch else "", MUTED)))
    mode_text, mode_color = MODE_LABELS.get(info.mode, MODE_LABELS["ask"])
    grid.add_row("mode", Text.assemble((mode_text, f"bold {mode_color}"), (f"  {MODE_HELP.get(info.mode, '')} · shift+tab to change", MUTED)))

    console.print(Panel(Group(head, Text(""), grid), box=box.ROUNDED, border_style=ACCENT, padding=(1, 2), expand=False))
    console.print(Text("  /help commands · !cmd runs a shell command · esc+enter newline · ctrl+c interrupt · ctrl+d quit", style=MUTED))
    if info.notice:
        console.print(Text.assemble(("  ⬆ ", f"bold {ACCENT2}"), (info.notice, ACCENT2)))
    console.print()


def print_help(groups: list[tuple[str, list[tuple[str, str]]]]) -> None:
    console.print()
    for title, cmds in groups:
        console.print(Text(f"  {title}", style=f"bold {ACCENT2}"))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=f"bold {ACCENT}", no_wrap=True)
        grid.add_column(style="#cdd6f4")
        for cmd, desc in cmds:
            grid.add_row(Text(cmd), Text(desc))
        console.print(Padding(grid, (0, 0, 1, 4)))
    console.print(Text("  Keys: tab completes · esc+enter newline · ↑ history · ctrl+c interrupts · ctrl+d quits", style=MUTED))


def _table() -> Table:
    return Table(box=box.SIMPLE_HEAD, header_style=f"bold {MUTED}", show_edge=False, pad_edge=False, padding=(0, 2))


def print_servers(rows: list[tuple[bool, object, object]]) -> None:
    t = _table()
    for col in ("", "name", "url", "status", "models"):
        t.add_column(col, justify="right" if col == "models" else "left", no_wrap=True)
    for active, s, p in rows:
        status = Text(f"● online {p.latency_ms}ms", style=OK) if p.ok else Text(f"○ {p.error}", style=ERR)
        t.add_row(
            Text("❯", style=ACCENT) if active else "",
            Text(s.name, style=f"bold {ACCENT}" if active else "bold"),
            Text(s.base_url, style=ACCENT2 if active else MUTED),
            status,
            str(len(p.models)) if p.ok else "–",
        )
    console.print(Padding(t, (1, 0, 0, 2)))
    console.print(Text("  /server <name> to switch · /add_api_server <name> <ip> to add · /remove_api_server <name>", style=MUTED))


CTX_SOURCES = {"model": "set on model", "loaded": "running now", "ollama-default": "Ollama default"}


def print_models(rows: list[tuple[bool, str, int, int | None, str]], server: str) -> None:
    t = _table()
    for col, justify in (("", "left"), ("model", "left"), ("context", "right"), ("max", "right"), ("", "left")):
        t.add_column(col, justify=justify, no_wrap=True)
    limited = False
    for current, name, ctx, maximum, source in rows:
        below_max = bool(maximum and maximum > ctx)
        limited |= below_max and source != "model"
        t.add_row(
            Text("❯", style=ACCENT) if current else "",
            Text(name, style=f"bold {ACCENT}" if current else ""),
            Text(f"{ctx:,}", style=WARN if below_max and source != "model" else ""),
            Text(f"{maximum:,}" if maximum else "–", style=MUTED),
            Text(CTX_SOURCES.get(source, "config/guess"), style=MUTED),
        )
    console.print(Padding(t, (1, 0, 0, 2)))
    console.print(Text(f"  on {server} · /model <name> to switch", style=MUTED))
    if limited:
        console.print(Text("  Yellow = Ollama is using less context than the model supports. Raise it with a Modelfile "
                           "(PARAMETER num_ctx) or OLLAMA_CONTEXT_LENGTH on the server.", style=MUTED))


def print_status(rows: list[tuple[str, RenderableType]]) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=MUTED, no_wrap=True)
    grid.add_column()
    for k, v in rows:
        grid.add_row(k, v)
    console.print(Panel(grid, title=f"[bold {ACCENT}]status[/]", title_align="left", box=box.ROUNDED, border_style=ACCENT, expand=False, padding=(0, 2)))


def _cell(value) -> RenderableType:
    return Text(value) if isinstance(value, str) else value


def print_table(columns: list[tuple[str, dict]], rows: list[tuple], footer: str | None = None) -> None:
    t = _table()
    for name, opts in columns:
        t.add_column(name, **opts)
    for row in rows:
        t.add_row(*(_cell(v) for v in row))
    console.print(Padding(t, (1, 0, 0, 2)))
    if footer:
        console.print(Text(f"  {footer}", style=MUTED))


CHECK_ICONS = {"ok": ("✓", OK), "warn": ("!", WARN), "bad": ("✗", ERR), "info": ("·", MUTED)}


def print_checks(rows: list[tuple[str, str, str]]) -> None:
    console.print()
    for state, name, detail in rows:
        icon, color = CHECK_ICONS[state]
        console.print(Text.assemble((f"  {icon} ", f"bold {color}"), (f"{name:<12}", "bold"), (detail, MUTED if state in ("ok", "info") else color)))


def print_release(version_from: str, version_to: str, notes: str) -> None:
    body = Group(
        Text.assemble((f"v{version_from}", MUTED), ("  →  ", MUTED), (f"v{version_to}", f"bold {OK}")),
        Text(""),
        Markdown(notes or "_No release notes._", code_theme=CODE_THEME),
    )
    console.print(Panel(body, title=f"[bold {ACCENT2}]Update available[/]", title_align="left", box=box.ROUNDED, border_style=ACCENT2, padding=(0, 2), expand=False))


def print_context(rows: list[tuple[str, int]], limit: int) -> None:
    used = sum(n for _, n in rows)
    t = _table()
    t.add_column("", no_wrap=True)
    t.add_column("tokens", justify="right")
    t.add_column("", no_wrap=True)
    for name, n in rows:
        cells = max(0, round(n * 30 / max(limit, 1)))
        t.add_row(Text(name), Text(f"{n:,}", style=MUTED), Text("█" * cells, style=ACCENT))
    pct = used * 100 // max(limit, 1)
    color = ERR if pct >= 80 else WARN if pct >= 60 else OK
    t.add_row(Text("total", style="bold"), Text(f"{used:,}", style="bold"), Text(f"{pct}% of {limit:,}", style=color))
    console.print(Padding(t, (1, 0, 0, 2)))
    console.print(Text(f"  ~{max(limit - used, 0):,} tokens free · history is summarized automatically at 80% · /compact to do it now", style=MUTED))


class NullUI:
    """Silent UI used by tests."""

    def __init__(self, permission: str = "y"):
        self.permission = permission
        self.text = ""
        self.calls: list[tuple[str, dict]] = []
        self.turns: list = []

    def stream_start(self): pass
    def stream_text(self, text): self.text += text
    def stream_end(self): pass
    def tool_call(self, name, args): self.calls.append((name, args))
    def tool_result(self, name, args, result, is_error, diff=None): pass
    def tool_denied(self, name): pass
    def ask_permission(self, name, args, preview): return self.permission
    def turn_done(self, stats): self.turns.append(stats)
    def info(self, msg): pass
    def ok(self, msg): pass
    def warn(self, msg): pass
    def error(self, msg): pass

    def spinner(self, msg):
        import contextlib
        return contextlib.nullcontext()
