"""Runs benchmark tasks against models, prints a scoreboard and saves the results as JSON."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from openai import APIConnectionError, APIError
from rich.text import Text

from .. import __version__, ui
from ..agent import Agent
from ..config import DATA_DIR, Config
from ..permissions import Permissions
from .tasks import Task

RESULTS_DIR = DATA_DIR / "bench"

# Models run without approval prompts inside a throwaway folder, but some commands are never allowed.
UNSAFE_SHELL = re.compile(
    r"\bsudo\b|\bsu\s|\.\./|(?:^|[\s;&|=\"'])(?:~|\$HOME)|(?:^|[\s;&|=\"'])/(?:etc|usr|bin|sbin|var|opt|System|Library|Users|home|root|private)\b"
    r"|\b(?:curl|wget|ssh|scp|rsync|shutdown|reboot|mkfs|dd|chown|kill|pkill|killall|launchctl|systemctl|osascript)\b"
    r"|\brm\s+-\w*\s+/",
    re.I,
)


def bench_approve(tool: str, args: dict, preview) -> str:
    if tool == "bash" and UNSAFE_SHELL.search(args.get("command", "")):
        return "n"
    return "y"


class BenchTimeout(Exception):
    pass


class BenchUI:
    """Silent UI that counts what happens during one task and enforces its time limit."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.tool_calls = self.tool_errors = self.denied = 0
        self.interrupted = False

    def _tick(self) -> None:
        if time.monotonic() > self.deadline:
            raise BenchTimeout()

    def stream_start(self): self._tick()
    def stream_text(self, text): self._tick()
    def stream_end(self): pass
    def tool_call(self, name, args):
        self.tool_calls += 1
        self._tick()
    def tool_result(self, name, args, result, is_error, diff=None):
        self.tool_errors += bool(is_error)
    def tool_denied(self, name): self.denied += 1
    def ask_permission(self, name, args, preview): return bench_approve(name, args, preview)
    def turn_done(self, stats): pass
    def info(self, msg): pass
    def ok(self, msg): pass
    def error(self, msg): pass
    def spinner(self, msg): return contextlib.nullcontext()

    def warn(self, msg):
        if msg.startswith("Interrupted"):
            self.interrupted = True


@dataclass
class TaskResult:
    model: str
    task: str
    passed: bool
    detail: str
    seconds: float
    outcome: str = "done"  # done | timeout | error
    tool_calls: int = 0
    tool_errors: int = 0
    repairs: int = 0
    nudges: int = 0
    denied: int = 0
    tokens: int = 0
    gen_seconds: float = 0.0


@dataclass
class ModelRun:
    model: str
    context: int | None = None
    gpu_percent: int | None = None
    load_seconds: float = 0.0
    results: list[TaskResult] = field(default_factory=list)

    def summary(self) -> dict:
        r = self.results
        gen = sum(x.gen_seconds for x in r)
        return {
            "passed": sum(x.passed for x in r),
            "total": len(r),
            "avg_seconds": sum(x.seconds for x in r) / len(r) if r else 0.0,
            "tokens_per_second": sum(x.tokens for x in r) / gen if gen else 0.0,
            "tool_errors": sum(x.tool_errors for x in r),
            "repairs": sum(x.repairs for x in r),
            "nudges": sum(x.nudges for x in r),
            "timeouts": sum(x.outcome == "timeout" for x in r),
        }


def run_task(client, config: Config, model: str, task: Task, timeout: float, max_steps: int) -> TaskResult:
    with tempfile.TemporaryDirectory(prefix="qcode-bench-") as work, tempfile.TemporaryDirectory(prefix="qcode-bench-s-") as scratch:
        root = Path(work).resolve()
        for name, text in task.files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        cfg = dataclasses.replace(config, model=model, max_steps=max_steps, auto_continue=True, read_before_write=True,
                                  allow=[], models=dict(config.models))
        bui = BenchUI(time.monotonic() + timeout)
        agent = Agent(client, cfg, root, Permissions(root, [], bench_approve, mode="ask"), bui,
                      session_path=Path(scratch) / "session.json")
        agent.model = model
        started = time.monotonic()
        outcome, detail = "done", ""
        try:
            agent.run(task.prompt)
        except BenchTimeout:
            outcome, detail = "timeout", f"no answer within {timeout:.0f}s"
        except APIConnectionError:
            raise
        except APIError as e:
            outcome, detail = "error", f"server error: {getattr(e, 'message', e)}"
        if bui.interrupted:
            raise KeyboardInterrupt
        seconds = time.monotonic() - started
        passed = False
        if outcome == "done":
            try:
                passed, detail = task.check(root, agent.last_reply())
            except Exception as e:  # a checker bug shouldn't end the run
                passed, detail = False, f"checker crashed: {type(e).__name__}: {e}"
        stats = agent.last_stats
        return TaskResult(
            model=model, task=task.name, passed=bool(passed), detail=detail, seconds=round(seconds, 2), outcome=outcome,
            tool_calls=bui.tool_calls, tool_errors=bui.tool_errors, denied=bui.denied,
            repairs=stats.repairs if stats else 0, nudges=stats.nudges if stats else 0,
            tokens=stats.tokens if stats else 0, gen_seconds=round(stats.gen_seconds, 2) if stats else 0.0,
        )


def warm_up(client, model: str) -> ModelRun:
    """Load the model before timing anything, and note its context size and GPU fit."""
    run = ModelRun(model)
    info = getattr(client, "context_info", None)
    if info:
        run.context = info(model).effective
    started = time.monotonic()
    fit = client.gpu_fit(model) if hasattr(client, "gpu_fit") else None
    run.load_seconds = round(time.monotonic() - started, 1)
    run.gpu_percent = fit[0] if fit else None
    return run


def run_bench(client, config: Config, models: list[str], tasks: list[Task], repeat: int = 1, timeout: float = 240,
              max_steps: int = 15, on_model: Callable[[ModelRun], None] | None = None,
              on_result: Callable[[TaskResult], None] | None = None) -> list[ModelRun]:
    runs: list[ModelRun] = []
    for model in models:
        run = warm_up(client, model)
        runs.append(run)
        if on_model:
            on_model(run)
        for task in tasks:
            for _ in range(repeat):
                result = run_task(client, config, model, task, timeout, max_steps)
                run.results.append(result)
                if on_result:
                    on_result(result)
    return runs


def save_results(runs: list[ModelRun], server: str, tasks: list[Task], repeat: int) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
    data = {
        "qcode": __version__,
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "server": server,
        "tasks": [t.name for t in tasks],
        "repeat": repeat,
        "models": [{**{k: v for k, v in asdict(r).items() if k != "results"}, "summary": r.summary(),
                    "results": [asdict(x) for x in r.results]} for r in runs],
    }
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path


# --- printing -------------------------------------------------------------------------------
def print_result(r: TaskResult) -> None:
    icon, color = ("✓", ui.OK) if r.passed else ("⏱", ui.WARN) if r.outcome == "timeout" else ("✗", ui.ERR)
    extras = []
    if r.tool_errors:
        extras.append(f"{r.tool_errors} tool error{'s' if r.tool_errors != 1 else ''}")
    if r.repairs:
        extras.append(f"{r.repairs} bad call{'s' if r.repairs != 1 else ''}")
    if r.nudges:
        extras.append("nudged")
    if r.denied:
        extras.append(f"{r.denied} unsafe command{'s' if r.denied != 1 else ''} blocked")
    ui.console.print(Text.assemble(
        (f"    {icon} ", f"bold {color}"), (f"{r.task:<16}", "bold"), (f"{r.seconds:>6.1f}s  ", ui.MUTED),
        (f"{r.tool_calls} tool{'s' if r.tool_calls != 1 else ''}  ", ui.MUTED), (r.detail[:70], "" if r.passed else color),
        (("  · " + ", ".join(extras)) if extras else "", ui.MUTED),
    ))


def print_model_header(run: ModelRun) -> None:
    bits = []
    if run.context:
        bits.append(f"{run.context:,} ctx")
    if run.gpu_percent is not None:
        bits.append(f"{run.gpu_percent}% on GPU" if run.gpu_percent < 100 else "100% GPU")
    if run.load_seconds:
        bits.append(f"loaded in {run.load_seconds:.0f}s")
    ui.console.print()
    ui.console.print(Text.assemble(("  ◆ ", ui.ACCENT), (run.model, f"bold {ui.ACCENT}"), ("  " + " · ".join(bits), ui.MUTED)))
    if run.gpu_percent is not None and run.gpu_percent < 100:
        ui.console.print(Text("    part of this model runs on the CPU, so expect it to be slow", style=ui.WARN))


def print_scoreboard(runs: list[ModelRun], tasks: list[Task], repeat: int) -> None:
    ranked = sorted(runs, key=lambda r: (-r.summary()["passed"], r.summary()["avg_seconds"]))
    rows = []
    for i, run in enumerate(ranked):
        s = run.summary()
        pct = s["passed"] * 100 // max(s["total"], 1)
        color = ui.OK if pct >= 80 else ui.WARN if pct >= 50 else ui.ERR
        bar = "█" * round(pct / 12.5) + "░" * (8 - round(pct / 12.5))
        rows.append((
            Text(("🏆 " if i == 0 and len(ranked) > 1 else "   ") + run.model, style="bold"),
            Text(f"{s['passed']}/{s['total']}", style=f"bold {color}"),
            Text(bar, style=color),
            Text(f"{s['avg_seconds']:.1f}s", style=ui.MUTED),
            Text(f"{s['tokens_per_second']:.0f}", style=ui.MUTED),
            Text(str(s["tool_errors"]), style=ui.WARN if s["tool_errors"] else ui.MUTED),
            Text(str(s["repairs"]), style=ui.WARN if s["repairs"] else ui.MUTED),
            Text(str(s["nudges"]), style=ui.WARN if s["nudges"] else ui.MUTED),
            Text(f"{run.gpu_percent}%" if run.gpu_percent is not None else "–", style=ui.WARN if (run.gpu_percent or 100) < 100 else ui.MUTED),
        ))
    ui.console.print()
    ui.console.print(Text("  Scoreboard", style=f"bold {ui.ACCENT2}"))
    ui.print_table(
        [("   model", {"no_wrap": True}), ("score", {"justify": "right", "no_wrap": True}), ("", {"no_wrap": True}),
         ("time", {"justify": "right", "no_wrap": True}), ("tok/s", {"justify": "right", "no_wrap": True}),
         ("errors", {"justify": "right", "no_wrap": True}), ("bad JSON", {"justify": "right", "no_wrap": True}),
         ("nudges", {"justify": "right", "no_wrap": True}), ("GPU", {"justify": "right", "no_wrap": True})],
        rows,
        footer="score = tasks passed · time = average per task · errors = failed tool calls · "
               "bad JSON = unreadable tool calls · nudges = said it would act but didn't",
        padding=1,
    )
    if len(runs) > 1:
        cols = [("task", {"no_wrap": True})] + [(r.model, {"justify": "center"}) for r in ranked]
        grid = []
        for task in tasks:
            cells = [Text(task.name)]
            for run in ranked:
                mine = [x for x in run.results if x.task == task.name]
                ok = sum(x.passed for x in mine)
                if repeat > 1:
                    cells.append(Text(f"{ok}/{len(mine)}", style=ui.OK if ok == len(mine) else ui.WARN if ok else ui.ERR))
                elif mine and mine[0].passed:
                    cells.append(Text("✓", style=f"bold {ui.OK}"))
                else:
                    cells.append(Text("⏱" if mine and mine[0].outcome == "timeout" else "✗", style=f"bold {ui.ERR}"))
            grid.append(tuple(cells))
        ui.console.print()
        ui.console.print(Text("  Per task", style=f"bold {ui.ACCENT2}"))
        ui.print_table(cols, grid)


def load_history(limit: int = 10) -> list[dict]:
    if not RESULTS_DIR.exists():
        return []
    out = []
    for path in sorted(RESULTS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append({"path": path, **json.loads(path.read_text(encoding="utf-8"))})
        except (OSError, ValueError):
            continue
    return out
