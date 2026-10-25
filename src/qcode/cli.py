"""Entry point: `qcode` (interactive) and `qcode bench`."""

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

app = typer.Typer(add_completion=False, help="A terminal coding agent for your self-hosted models.")


def pick_server(config, server_name: Optional[str], base_url: Optional[str], api_key: Optional[str] = None) -> tuple[ServerStore, Server]:
    # Older configs had base_url; it becomes the first saved server. New installs start with none.
    key = api_key or config.api_key or "ollama"
    seed = Server("home", normalize_url(config.base_url), key) if config.base_url else None
    store = ServerStore(seed=seed)
    if base_url:
        return store, Server("custom", normalize_url(base_url), key)
    if not store.servers:
        from .onboarding import can_ask, first_run_setup

        if not can_ask():
            raise typer.BadParameter("no model server saved yet. Run `qcode` once in a terminal to set one up, "
                                     "or pass --base-url <ip>.", param_hint="server")
        server = first_run_setup(store, key)
        if server is None:
            from . import ui

            ui.console.print("  No server set up yet. Run qcode again when you're ready.")
            raise typer.Exit(0)
        return store, server
    if server_name:
        server = store.get(server_name)
        if server is None:
            raise typer.BadParameter(f"no server named {server_name!r}; known: {', '.join(sorted(store.servers))}", param_hint="--server")
        return store, server
    return store, store.current


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    prompt: Optional[str] = typer.Option(None, "-p", "--prompt", help="Run one prompt and exit."),
    model: Optional[str] = typer.Option(None, "-m", "--model", help="Model to use (overrides config)."),
    server_name: Optional[str] = typer.Option(None, "-s", "--server", help="Saved server to use (see /servers)."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="One-off server address, e.g. 192.168.1.50 or http://host:11434/v1"),
    api_key: Optional[str] = typer.Option(None, "--api-key", "--password", help="API key or password for the server."),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens", help="Max completion tokens (default from config: 4096)."),
    temperature: Optional[str] = typer.Option(None, "--temperature", help="Sampling temperature, e.g. 0.8 or 'default'."),
    compress_input: Optional[bool] = typer.Option(None, "--compress-input/--no-compress-input", help="Compress input tokens to fit smaller context."),
    resume: bool = typer.Option(False, "-r", "--resume", help="Resume the latest session for this folder."),
    mode: Optional[str] = typer.Option(None, "--mode", help="Permission mode: ask, auto-edit, auto or plan (default from config)."),
    yolo: bool = typer.Option(False, "--yolo", help="Same as --mode auto: approve every tool call without asking."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Project root (default: current directory)."),
) -> None:
    if ctx.invoked_subcommand:
        return
    config = load_config()
    if model:
        config.model = model
    if max_tokens is not None:
        config.max_tokens = max_tokens
    if temperature is not None:
        from .config import parse_setting
        config.temperature = parse_setting("temperature", temperature)
    if compress_input is not None:
        config.compress_input = compress_input
    store, server = pick_server(config, server_name, base_url, api_key)

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
        else:
            ui.info("No previous session for this folder; starting fresh.")

    repl = Repl(agent, store, server, mode=start_mode)
    repl.replay_on_start = resume and len(agent.messages) > 1
    if prompt:
        repl.ask(prompt)
        return
    repl.run()


@app.command()
def bench(
    models: str = typer.Option("", "--models", "-m", help="Comma-separated models, or 'all'. Default: your configured model."),
    tasks: str = typer.Option("", "--tasks", "-t", help="Comma-separated task names (see --list). Default: all."),
    repeat: int = typer.Option(1, "--repeat", "-n", min=1, max=10, help="Run each task this many times (more = fairer, slower)."),
    timeout: int = typer.Option(240, "--timeout", min=30, help="Seconds allowed per task."),
    max_steps: int = typer.Option(15, "--max-steps", min=1, help="Tool round trips allowed per task."),
    server_name: Optional[str] = typer.Option(None, "-s", "--server", help="Saved server to use."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="One-off server address."),
    list_tasks: bool = typer.Option(False, "--list", help="List the tasks and exit."),
    history: bool = typer.Option(False, "--history", help="Show scores from earlier runs and exit."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask before starting."),
    temperature: Optional[str] = typer.Option(None, "--temperature", help="Override temperature, e.g. 0.2 or default."),
    thinking: Optional[str] = typer.Option(None, "--thinking", help="default or off (skip qwen3's hidden reasoning)."),
    examples: Optional[bool] = typer.Option(None, "--examples/--no-examples", help="Add the worked tool-use example to the prompt."),
    label: str = typer.Option("", "--label", help="Name this run in --history, e.g. 'no-think'."),
) -> None:
    """Score models on small, self-checking coding tasks."""
    from openai import APIConnectionError
    from rich.text import Text

    from . import ui
    from .bench import TASKS, TASKS_BY_NAME
    from .bench import runner

    if list_tasks:
        ui.print_table([("task", {"style": "bold", "no_wrap": True}), ("tests", {"style": ui.ACCENT2, "no_wrap": True}), ("prompt", {})],
                       [(t.name, t.skill, t.prompt) for t in TASKS])
        return
    if history:
        past = runner.load_history()
        if not past:
            ui.console.print(Text("  No benchmark runs yet. Try: qcode bench", style=ui.MUTED))
            return
        rows = []
        for run in past:
            for m in run["models"]:
                s = m["summary"]
                st = run.get("settings", {})
                tweaks = st.get("label") or ", ".join(f"{k}={v}" for k, v in st.items() if k != "label" and v not in ("default", False, None))
                rows.append((run["date"].replace("T", " ")[:16], m["model"], tweaks or "-", f"{s['passed']}/{s['total']}",
                             f"{s['avg_seconds']:.1f}s", f"{s['tokens_per_second']:.0f}", run.get("qcode", "")))
        ui.print_table([("date", {"style": ui.MUTED}), ("model", {"style": "bold"}), ("settings", {"style": ui.ACCENT2}),
                        ("passed", {"justify": "right"}), ("avg time", {"justify": "right"}), ("tok/s", {"justify": "right"}),
                        ("qcode", {"style": ui.MUTED})], rows,
                       footer=f"Full results: {runner.RESULTS_DIR}")
        return

    from .servers import ping

    from .config import parse_setting

    config = load_config()
    try:
        if temperature is not None:
            config.temperature = parse_setting("temperature", temperature)
        if thinking is not None:
            config.thinking = parse_setting("thinking", thinking)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    if examples is not None:
        config.tool_examples = examples
    settings = {"temperature": config.temperature, "thinking": config.thinking, "tool_examples": config.tool_examples, "label": label}
    _, server = pick_server(config, server_name, base_url)
    client = LLMClient(server.base_url, server.api_key)
    status = ping(server, timeout=5)
    if not status.ok:
        ui.console.print(Text(f"  ✗ Can't reach {server.name} ({server.address}): {status.error}. Is the server on and ZeroTier up?", style=ui.ERR))
        raise typer.Exit(1)
    available = [m.removesuffix(":latest") for m in status.models]

    if models.strip().lower() == "all":
        chosen = [m for m in available if "embed" not in m.lower()]
    elif models:
        chosen = [m.strip().removesuffix(":latest") for m in models.split(",") if m.strip()]
    else:
        chosen = [config.model.removesuffix(":latest")]
    missing = [m for m in chosen if m not in available]
    if missing:
        raise typer.BadParameter(f"not on {server.name}: {', '.join(missing)}. Available: {', '.join(available)}", param_hint="--models")

    picked = list(TASKS)
    if tasks:
        names = [t.strip() for t in tasks.split(",") if t.strip()]
        unknown = [n for n in names if n not in TASKS_BY_NAME]
        if unknown:
            raise typer.BadParameter(f"unknown task(s): {', '.join(unknown)}. See qcode bench --list", param_hint="--tasks")
        picked = [TASKS_BY_NAME[n] for n in names]

    runs_total = len(chosen) * len(picked) * repeat
    ui.console.print(Text.assemble(
        ("\n  qcode bench  ", f"bold {ui.ACCENT}"),
        (f"{len(picked)} task{'s' if len(picked) != 1 else ''} × {len(chosen)} model{'s' if len(chosen) != 1 else ''}"
         + (f" × {repeat}" if repeat > 1 else "") + f" = {runs_total} runs on {server.name} ({server.address})", ui.MUTED),
    ))
    ui.console.print(Text("  Each task runs in a throwaway folder with approvals on auto; unsafe shell commands are blocked.", style=ui.MUTED))
    ui.console.print(Text(f"  Usually 10–60s per run, so roughly {max(1, runs_total * 25 // 60)}–{max(2, runs_total)} minutes. Ctrl+C stops.", style=ui.MUTED))
    if not yes and runs_total > 10 and ui.select("Start?", [("y", "Yes"), ("n", "No")], cancel="n") != "y":
        return

    runs: list = []
    try:
        runner.run_bench(
            client, config, chosen, picked, repeat=repeat, timeout=timeout, max_steps=max_steps,
            on_model=lambda run: (runs.append(run), runner.print_model_header(run)),
            on_result=runner.print_result,
        )
    except KeyboardInterrupt:
        ui.console.print(Text("\n  Stopped. Showing what finished.", style=ui.WARN))
    except APIConnectionError:
        ui.console.print(Text(f"\n  ✗ Lost the connection to {server.name}. Showing what finished.", style=ui.ERR))
    runs = [r for r in runs if r.results]
    if not runs:
        return
    runner.print_scoreboard(runs, picked, repeat)
    path = runner.save_results(runs, server.base_url, picked, repeat, settings)
    ui.console.print(Text(f"\n  Saved to {path} · qcode bench --history to compare runs", style=ui.MUTED))


@app.command("install-compressor")
def install_compressor_cli() -> None:
    """Install the optional neural input compressor (llmlingua) into qcode's environment."""
    from .compressor import install_compressor
    ok = install_compressor()
    if not ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
