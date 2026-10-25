"""First-run setup: ask for the model server (there is no built-in default) and save it."""

from __future__ import annotations

import sys

from rich.text import Text

from . import ui
from .servers import NAME_RE, Server, ServerStore, normalize_url, ping


def can_ask() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def first_run_setup(store: ServerStore, api_key: str = "ollama") -> Server | None:
    """Ask for a server until one is saved. Returns it, or None if the user gives up (Ctrl+C / empty answer)."""
    ui.console.print()
    ui.console.print(Text.assemble(("  Welcome to qcode! ", f"bold {ui.ACCENT}"), ("Let's connect it to your model server.", "")))
    ui.console.print(Text("  This is the machine running Ollama (or another OpenAI-compatible server). You can use an IP\n"
                          "  like 192.168.1.50, host:port like gpu-box:11434, or a full URL. It's saved, so you only do this once.",
                          style=ui.MUTED))
    while True:
        address = ui.text_input("Server address", "")
        if not address:
            return None
        try:
            url = normalize_url(address)
        except ValueError as e:
            ui.console.print(Text(f"  ✗ {e}", style=ui.ERR))
            continue
        password = ui.text_input("Password or API key (optional, press Enter for none)", "")
        if password is None:
            return None
        key = password.strip() if password.strip() else api_key
        with ui.console.status(Text(f"Checking {url}…", style=ui.MUTED), spinner="dots", spinner_style=ui.ACCENT):
            status = ping(Server("new", url, key), timeout=5)
        if status.ok:
            models = ", ".join(m.removesuffix(":latest") for m in status.models[:6]) + ("…" if len(status.models) > 6 else "")
            ui.console.print(Text.assemble(("  ✓ ", ui.OK), (f"Connected · {status.latency_ms}ms · {len(status.models)} models", ""),
                                           (f": {models}" if models else "", ui.MUTED)))
        else:
            ui.console.print(Text(f"  ✗ {url} isn't responding ({status.error}).", style=ui.ERR))
            choice = ui.select("What now?", [("retry", "Type a different address or password"), ("save", "Save it anyway (I'll start the server later)")],
                               cancel="retry")
            if choice != "save":
                continue
        name = ui.text_input("Name for this server", "home")
        if name is None:
            return None
        name = name or "home"
        while not NAME_RE.match(name):
            name = ui.text_input("Use letters, digits, '.', '_' or '-'", "home")
            if name is None:
                return None
            name = name or "home"
        server = store.add(name, url, key)
        store.use(name)
        ui.console.print(Text(f"  ✓ Saved as {name!r}. Change or add servers any time with /servers and /add_api_server.", style=ui.OK))
        ui.console.print()
        return server
