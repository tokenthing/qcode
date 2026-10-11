"""Self-update from GitHub releases (https://github.com/tokenthing/qcode/releases)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from . import __version__
from .config import DATA_DIR

REPO = os.environ.get("QCODE_UPDATE_REPO", "tokenthing/qcode")
CHECK_FILE = DATA_DIR / "update-check.json"
CHECK_EVERY = 24 * 3600


@dataclass
class Release:
    version: str  # "0.3.1"
    tag: str      # "v0.3.1"
    notes: str
    url: str      # release page

    @property
    def archive_url(self) -> str:
        return f"https://github.com/{REPO}/archive/refs/tags/{self.tag}.zip"


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3); ignores suffixes like '-rc1'."""
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts)


def is_newer(release: Release | None, current: str = __version__) -> bool:
    return bool(release) and parse_version(release.version) > parse_version(current)


def fetch_latest(timeout: float = 5.0) -> Release | None:
    """Latest published release, or None (offline, rate limited, no releases)."""
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"qcode/{__version__}"},
            timeout=timeout,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        tag = data["tag_name"]
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    return Release(tag.lstrip("vV"), tag, (data.get("body") or "").strip(), data.get("html_url", ""))


def cached_latest(max_age: float = CHECK_EVERY, timeout: float = 2.5) -> Release | None:
    """Like fetch_latest, but asks GitHub at most once per max_age seconds."""
    try:
        cached = json.loads(CHECK_FILE.read_text(encoding="utf-8"))
        if time.time() - cached["checked_at"] < max_age:
            return Release(**cached["release"]) if cached.get("release") else None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    release = fetch_latest(timeout=timeout)
    try:
        CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHECK_FILE.write_text(json.dumps({"checked_at": time.time(), "release": asdict(release) if release else None}), encoding="utf-8")
    except OSError:
        pass
    return release


def install_kind() -> tuple[str, str]:
    """('editable', repo path) for a dev checkout, else ('pip', site-packages path)."""
    try:
        from importlib.metadata import distribution

        direct = distribution("qcode").read_text("direct_url.json")
        info = json.loads(direct) if direct else {}
        if info.get("dir_info", {}).get("editable"):
            return "editable", info.get("url", "").removeprefix("file://")
    except Exception:
        pass
    return "pip", str(Path(__file__).resolve().parent)


def install(release: Release) -> tuple[bool, str]:
    """pip-install the release into the Python running qcode. Returns (ok, output tail)."""
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
    proc = subprocess.run(pip + ["--upgrade", release.archive_url], capture_output=True, text=True)
    if proc.returncode == 0:
        # pip skips packages whose version it already has; always lay down this release's code.
        proc = subprocess.run(pip + ["--force-reinstall", "--no-deps", release.archive_url], capture_output=True, text=True)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-12:])
    if proc.returncode == 0:
        CHECK_FILE.unlink(missing_ok=True)
    return proc.returncode == 0, tail


def restart(extra_args: list[str] | None = None) -> None:
    """Replace this process with a fresh qcode (same arguments plus extra_args)."""
    args = [a for a in sys.argv[1:] if a not in (extra_args or [])] + list(extra_args or [])
    env = {**os.environ, "QCODE_JUST_UPDATED": "1"}
    os.execve(sys.executable, [sys.executable, "-m", "qcode", *args], env)
