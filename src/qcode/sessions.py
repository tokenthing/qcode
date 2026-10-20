"""Session persistence: every turn is autosaved; --resume / /resume reload it."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR

SESSIONS_DIR = DATA_DIR / "sessions"


@dataclass
class SessionInfo:
    path: Path
    root: str
    model: str
    updated: str
    first_prompt: str


def new_session_path() -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return SESSIONS_DIR / f"{stamp}.json"


MAX_SAVED_CHECKPOINTS = 20
MAX_CHECKPOINT_FILE_CHARS = 512_000


def save(path: Path, root: Path, model: str, messages: list[dict], checkpoints: list[dict] | None = None,
         seen: list[str] | None = None) -> None:
    # Skip the system prompt; it's rebuilt on resume.
    body = [m for m in messages if m["role"] != "system"]
    if not body:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Undo history survives restarts; very large files aren't kept (their undo entry is dropped).
    kept = [
        c for c in (checkpoints or [])[-MAX_SAVED_CHECKPOINTS:]
        if all(old is None or len(old) <= MAX_CHECKPOINT_FILE_CHARS for old in c["files"].values())
    ]
    data = {
        "root": str(root),
        "model": model,
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
        "messages": body,
        "checkpoints": kept,
        "seen": sorted(seen or []),  # files qcode read or wrote, so it can keep editing them after a resume
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_sessions(root: Path | None = None, limit: int = 10) -> list[SessionInfo]:
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = load(p)
        except (json.JSONDecodeError, OSError):
            continue
        if root is not None and data.get("root") != str(root):
            continue
        first = next((m.get("content") or "" for m in data["messages"] if m["role"] == "user"), "")
        out.append(SessionInfo(p, data["root"], data.get("model", ""), data.get("updated", ""), first.splitlines()[0][:70] if first else ""))
        if len(out) >= limit:
            break
    return out
