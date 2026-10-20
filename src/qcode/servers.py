"""Named API servers (~/.config/qcode/servers.json): add, remove, switch, ping."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import CONFIG_DIR

SERVERS_FILE = CONFIG_DIR / "servers.json"
DEFAULT_PORT = 11434
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$|^[0-9a-fA-F:]+$")


@dataclass
class Server:
    name: str
    base_url: str
    api_key: str = "ollama"

    @property
    def address(self) -> str:
        return urlparse(self.base_url).netloc or self.base_url


@dataclass
class Ping:
    ok: bool
    latency_ms: int = 0
    models: list[str] = field(default_factory=list)
    error: str = ""


def normalize_url(raw: str) -> str:
    """'192.168.1.50' -> 'http://192.168.1.50:11434/v1'; full URLs are kept (a missing path becomes /v1)."""
    raw = raw.strip().rstrip("/")
    if not raw:
        raise ValueError("address is empty")
    had_scheme = "://" in raw
    u = urlparse(raw if had_scheme else f"http://{raw}")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"can't parse address {raw!r}")
    if not HOST_RE.match(u.hostname):
        raise ValueError(f"{u.hostname!r} isn't a valid host name or IP address")
    netloc = u.netloc
    try:
        if not had_scheme and u.port is None:
            netloc += f":{DEFAULT_PORT}"
    except ValueError:
        raise ValueError(f"invalid port in {raw!r}") from None
    return f"{u.scheme}://{netloc}{u.path or '/v1'}"


def _describe(e: Exception) -> str:
    if isinstance(e, httpx.TimeoutException):
        return "timed out"
    if isinstance(e, httpx.ConnectError):
        return "unreachable"
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def ping(server: Server, timeout: float = 2.5) -> Ping:
    start = time.monotonic()
    try:
        r = httpx.get(
            f"{server.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {server.api_key}"},
            timeout=timeout,
        )
        r.raise_for_status()
        models = sorted(m["id"] for m in r.json().get("data", []))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        return Ping(False, error=_describe(e))
    return Ping(True, int((time.monotonic() - start) * 1000), models)


class ServerStore:
    def __init__(self, path: Path = SERVERS_FILE, seed: Server | None = None):
        self.path = path
        self.servers: dict[str, Server] = {}
        self.active = ""
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.servers = {n: Server(n, **v) for n, v in data.get("servers", {}).items()}
            self.active = data.get("active", "")
        if not self.servers and seed:
            self.servers[seed.name] = seed
            self.active = seed.name
            self.save()
        if self.active not in self.servers and self.servers:
            self.active = next(iter(self.servers))

    @property
    def current(self) -> Server:
        return self.servers[self.active]

    def get(self, name: str) -> Server | None:
        return self.servers.get(name)

    def add(self, name: str, address: str, api_key: str = "ollama") -> Server:
        if not NAME_RE.match(name):
            raise ValueError("name must be letters, digits, '.', '_' or '-' (max 32 chars)")
        server = Server(name, normalize_url(address), api_key)
        self.servers[name] = server
        if not self.active:
            self.active = name
        self.save()
        return server

    def remove(self, name: str) -> None:
        if name not in self.servers:
            raise KeyError(name)
        if len(self.servers) == 1:
            raise ValueError("can't remove the only server; add another first")
        del self.servers[name]
        if self.active == name:
            self.active = next(iter(self.servers))
        self.save()

    def use(self, name: str) -> Server:
        if name not in self.servers:
            raise KeyError(name)
        self.active = name
        self.save()
        return self.servers[name]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active": self.active,
            "servers": {n: {"base_url": s.base_url, "api_key": s.api_key} for n, s in self.servers.items()},
        }
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
