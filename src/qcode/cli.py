"""Entry point: `qcode`."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import sessions
from .agent import Agent
from .client import LLMClient
from .config import load_config, parse_mode
from .permissions import Permissions
from .repl import Repl
from .servers import Server, ServerStore, normalize_url
from .ui import TerminalUI

app = typer.Typer(add_completion=False, help="A terminal coding agent for your self-hosted Qwen2.5-Coder.")


@app.command()
def main(
    prompt: Optional[str] = typer.Option(None, "-p", "--prompt", help="Run one prompt and exit."),
    model: Optional[str] = typer.Option(None, "-m", "--model", help="Model to use (overrides config)."),
    server_name: Optional[str] = typer.Option(None, "-s", "--server", help="Saved server to use (see /servers)."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="One-off server address, e.g. 192.168.1.50 or http://host:11434/v1"),
    resume: bool = typer.Option(False, "-r", "--resume", help="Resume the latest session for this folder."),
    mode: Optional[str] = typer.Option(None, "--mode", help="Permission mode: ask, auto-edit, auto or plan (default from config)."),
    yolo: bool = typer.Option(False, "--yolo", help="Same as --mode auto: approve every tool call without asking."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Project root (default: current directory)."),
) -> None:
    config = load_config()
    if model:
        config.model = model
    store = ServerStore(seed=Server("home", normalize_url(config.base_url), config.api_key))

    if base_url:
        server = Server("custom", normalize_url(base_url), config.api_key)
    elif server_name:
        server = store.get(server_name)
        if server is None:
            raise typer.BadParameter(f"no server named {server_name!r}; known: {', '.join(sorted(store.servers))}", param_hint="--server")
    else:
        server = store.current

    root = cwd.resolve()
    ui = TerminalUI()
    try:
        start_mode = "auto" if yolo else parse_mode(mode) if mode else config.default_mode
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--mode") from None
    permissions = Permissions(root, config.allow, ui.ask_permission, mode=start_mode)
    agent = Agent(LLMClient(server.base_url, server.api_key), config, root, permissions, ui)

    if resume:
        found = sessions.list_sessions(root, limit=1)
        if found:
            agent.load_session(found[0].path)
            if model:
                agent.model = model
            ui.ok(f"Resumed: {found[0].first_prompt}")
        else:
            ui.info("No previous session for this folder; starting fresh.")

    repl = Repl(agent, store, server, mode=start_mode)
    if prompt:
        repl.ask(prompt)
        return
    repl.run()


if __name__ == "__main__":
    app()
