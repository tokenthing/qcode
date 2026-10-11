#!/usr/bin/env python3
"""Build dist/install_qcode.py: the installer with the qcode source embedded, as one shareable file.

Optional installer/defaults.json (not committed) pre-fills your own server for people you share it with:
    {"server": "192.168.1.50", "server_name": "home", "zerotier_network": "0123456789abcdef"}
Pass --generic to ignore it (CI release builds do this).
"""

import base64
import io
import json
import sys
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "installer" / "install_qcode.py"
DEFAULTS = ROOT / "installer" / "defaults.json"
OUT = ROOT / "dist" / "install_qcode.py"
DEFAULT_KEYS = {"server": "DEFAULT_SERVER", "server_name": "DEFAULT_SERVER_NAME", "zerotier_network": "DEFAULT_ZT_NETWORK"}


def source_files():
    yield ROOT / "pyproject.toml"
    yield ROOT / "README.md"
    yield from sorted((ROOT / "src").rglob("*.py"))


def replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"expected exactly one {old!r} in the installer template"
    return text.replace(old, new)


def main() -> None:
    generic = "--generic" in sys.argv
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in source_files():
            zf.write(path, path.relative_to(ROOT).as_posix())
    encoded = "\n".join(textwrap.wrap(base64.b64encode(buf.getvalue()).decode(), 100))
    text = replace_once(TEMPLATE.read_text(), "PAYLOAD = None\n", f'PAYLOAD = """\n{encoded}\n"""\n')

    defaults = {} if generic or not DEFAULTS.exists() else json.loads(DEFAULTS.read_text())
    for key, const in DEFAULT_KEYS.items():
        if defaults.get(key):
            default_line = next(l for l in text.splitlines() if l.startswith(f"{const} = "))
            text = replace_once(text, default_line + "\n", f"{const} = {json.dumps(defaults[key])}\n")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text)
    which = "generic" if not defaults else f"defaults from {DEFAULTS.relative_to(ROOT)}"
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB, {which})")


if __name__ == "__main__":
    main()
