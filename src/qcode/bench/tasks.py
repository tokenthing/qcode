"""Benchmark tasks: small, self-checking coding jobs that exercise what an agent needs to do well.

Each task starts from `files` in an empty folder, gives the model `prompt`, then `check(folder, reply)`
decides pass/fail. `reference(folder)` applies a correct solution (and returns a correct reply), which
the test suite uses to prove every checker accepts a right answer and rejects the untouched start.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

Check = Callable[[Path, str], "tuple[bool, str]"]


@dataclass
class Task:
    name: str
    skill: str
    prompt: str
    check: Check
    reference: Callable[[Path], str]
    files: dict[str, str] = field(default_factory=dict)


def run_py(folder: Path, *args: str, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run([sys.executable, *args], cwd=folder, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    return p.returncode, (p.stdout + p.stderr).strip()


def _read(folder: Path, name: str) -> str | None:
    p = folder / name
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _write(folder: Path, name: str, text: str) -> None:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _norm(text: str | None) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


# --- 1. create a script ------------------------------------------------------------------
def _check_create_script(folder: Path, reply: str):
    if not (folder / "hello.py").is_file():
        return False, "hello.py wasn't created"
    code, out = run_py(folder, "hello.py")
    return (code == 0 and out == "Hello, bench!"), f"printed {out!r}"


def _ref_create_script(folder: Path) -> str:
    _write(folder, "hello.py", 'print("Hello, bench!")\n')
    return "Created hello.py."


# --- 2. fix a bug so the tests pass --------------------------------------------------------
CALC = '''def average(numbers):
    """Return the mean of a non-empty list of numbers."""
    total = 0
    for n in numbers:
        total += n
    return total / (len(numbers) + 1)


def clamp(value, low, high):
    """Limit value to the range [low, high]."""
    return max(low, min(value, high))
'''
TEST_CALC = '''from calc import average, clamp

assert average([2, 4, 6]) == 4, average([2, 4, 6])
assert average([5]) == 5
assert clamp(15, 0, 10) == 10
assert clamp(-3, 0, 10) == 0
print("all tests passed")
'''


def _check_fix_bug(folder: Path, reply: str):
    if _norm(_read(folder, "test_calc.py")) != _norm(TEST_CALC):
        return False, "test_calc.py was changed"
    code, out = run_py(folder, "test_calc.py")
    return code == 0, out.splitlines()[-1] if out else f"exit {code}"


def _ref_fix_bug(folder: Path) -> str:
    _write(folder, "calc.py", CALC.replace("(len(numbers) + 1)", "len(numbers)"))
    return "Fixed the divisor in average()."


# --- 3. add a function --------------------------------------------------------------------
UTILS = '''def add(a, b):
    """Return a plus b."""
    return a + b


def subtract(a, b):
    """Return a minus b."""
    return a - b
'''


def _check_add_function(folder: Path, reply: str):
    code, out = run_py(folder, "-c", "import utils; assert utils.multiply(3, 4) == 12; assert utils.multiply(-2, 5) == -10; "
                                     "assert utils.add(2, 3) == 5; assert utils.subtract(5, 3) == 2; print('ok')")
    return code == 0, "multiply works and add/subtract kept" if code == 0 else out.splitlines()[-1] if out else f"exit {code}"


def _ref_add_function(folder: Path) -> str:
    _write(folder, "utils.py", UTILS + '\n\ndef multiply(a, b):\n    """Return a times b."""\n    return a * b\n')
    return "Added multiply()."


# --- 4. a small edit in a big file, without wiping the rest --------------------------------
PAGE = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n  <title>Old Title</title>\n"
    "  <style>\n    body { font-family: sans-serif; }\n  </style>\n</head>\n<body>\n  <h1>Inventory</h1>\n  <ul>\n"
    + "".join(f"    <li data-id=\"{i}\">Item {i}: part number P-{1000 + i * 7}</li>\n" for i in range(1, 106))
    + "  </ul>\n  <!-- end of inventory -->\n</body>\n</html>\n"
)


def _check_surgical_edit(folder: Path, reply: str):
    now = _read(folder, "index.html")
    if now is None:
        return False, "index.html is gone"
    if "<title>Bench Page</title>" not in now:
        return False, "title not changed"
    expected = PAGE.replace("<title>Old Title</title>", "<title>Bench Page</title>")
    if _norm(now) != _norm(expected):
        lost = len(PAGE.splitlines()) - len(now.splitlines())
        return False, f"other content changed ({lost} lines lost)" if lost > 0 else "other content changed"
    return True, "only the title changed"


def _ref_surgical_edit(folder: Path) -> str:
    _write(folder, "index.html", PAGE.replace("<title>Old Title</title>", "<title>Bench Page</title>"))
    return "Changed the title."


# --- 5. find where something is defined ----------------------------------------------------
FIND_FILES = {
    "README.md": "# Shop\n\nOrders are parsed and then priced. See the app package.\n",
    "app/__init__.py": "",
    "app/main.py": "from app.orders import parse_order\nfrom app.users import load_user\n\n\ndef run(raw):\n    return parse_order(raw), load_user(1)\n",
    "app/users.py": "def load_user(user_id):\n    return {'id': user_id}\n",
    "app/orders.py": "def parse_order(raw):\n    \"\"\"Turn 'sku:qty' into a dict.\"\"\"\n    sku, qty = raw.split(':')\n    return {'sku': sku, 'qty': int(qty)}\n",
    "app/util/__init__.py": "",
    "app/util/format.py": "def format_order(order):\n    return f\"{order['qty']} x {order['sku']}\"\n",
}


def _check_find_definition(folder: Path, reply: str):
    ok = "app/orders.py" in reply.replace("\\", "/")
    return ok, "named app/orders.py" if ok else f"answered {reply.strip()[:60]!r}"


# --- 6. rename across files -----------------------------------------------------------------
RENAME_FILES = {
    "shop/__init__.py": "",
    "shop/prices.py": "def calc_total(items):\n    \"\"\"Sum price * qty for each item.\"\"\"\n    return sum(price * qty for price, qty in items)\n",
    "shop/cart.py": "from shop.prices import calc_total\n\n\nclass Cart:\n    def __init__(self):\n        self.items = []\n\n    def add(self, price, qty=1):\n        self.items.append((price, qty))\n\n    def total(self):\n        return calc_total(self.items)\n",
    "main.py": "from shop.cart import Cart\nfrom shop.prices import calc_total\n\ncart = Cart()\ncart.add(10.0, 3)\ncart.add(12.5)\nprint(f\"Total: {cart.total()}\")\nassert cart.total() == calc_total(cart.items)\n",
}


def _check_rename(folder: Path, reply: str):
    leftovers = [p.relative_to(folder).as_posix() for p in folder.rglob("*.py") if "calc_total" in p.read_text(encoding="utf-8")]
    if leftovers:
        return False, f"calc_total still in {', '.join(leftovers)}"
    if "def compute_total" not in (_read(folder, "shop/prices.py") or ""):
        return False, "compute_total isn't defined in shop/prices.py"
    code, out = run_py(folder, "main.py")
    return (code == 0 and out == "Total: 42.5"), f"main.py printed {out!r}"


def _ref_rename(folder: Path) -> str:
    for name, text in RENAME_FILES.items():
        _write(folder, name, text.replace("calc_total", "compute_total"))
    return "Renamed."


# --- 7. run a script and report -------------------------------------------------------------
COMPUTE = "def primes_below(n):\n    sieve = [True] * n\n    sieve[0:2] = [False, False]\n    for i in range(2, int(n ** 0.5) + 1):\n        if sieve[i]:\n            sieve[i * i::i] = [False] * len(sieve[i * i::i])\n    return [i for i, p in enumerate(sieve) if p]\n\n\nprint(sum(primes_below(2000)))\n"


def _check_run_report(folder: Path, reply: str):
    ok = "277050" in reply.replace(",", "").replace(" ", "").replace("_", "")
    return ok, "reported 277050" if ok else f"answered {reply.strip()[:60]!r}"


# --- 8. create an HTML page -----------------------------------------------------------------
class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.h1 = ""
        self.li_in_ul = 0

    def handle_starttag(self, tag, attrs):
        if tag == "li" and "ul" in self.stack:
            self.li_in_ul += 1
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if "h1" in self.stack:
            self.h1 += data


def _check_create_html(folder: Path, reply: str):
    text = _read(folder, "fruits.html")
    if text is None:
        return False, "fruits.html wasn't created"
    parser = _PageParser()
    parser.feed(text)
    if "fruits" not in parser.h1.lower():
        return False, f"h1 is {parser.h1.strip()!r}"
    return parser.li_in_ul == 3, f"{parser.li_in_ul} list items"


def _ref_create_html(folder: Path) -> str:
    _write(folder, "fruits.html", "<!DOCTYPE html>\n<html>\n<head><title>Fruits</title></head>\n<body>\n<h1>Fruits</h1>\n"
                                  "<ul>\n<li>Apple</li>\n<li>Banana</li>\n<li>Cherry</li>\n</ul>\n</body>\n</html>\n")
    return "Created fruits.html."


# --- 9. edit JSON, keep the rest ------------------------------------------------------------
SETTINGS = {"name": "demo", "debug": False, "retries": 3, "servers": ["alpha", "beta"], "limits": {"cpu": 2, "memory": "1G"}}


def _check_edit_json(folder: Path, reply: str):
    try:
        data = json.loads(_read(folder, "settings.json") or "")
    except json.JSONDecodeError as e:
        return False, f"invalid JSON ({e.msg})"
    if data.get("debug") is not True:
        return False, f"debug is {data.get('debug')!r}"
    if data.get("version") != "2.0":
        return False, f"version is {data.get('version')!r}"
    lost = [k for k, v in SETTINGS.items() if k != "debug" and data.get(k) != v]
    return not lost, "other keys kept" if not lost else f"changed or lost: {', '.join(lost)}"


def _ref_edit_json(folder: Path) -> str:
    _write(folder, "settings.json", json.dumps({**SETTINGS, "debug": True, "version": "2.0"}, indent=2) + "\n")
    return "Updated settings.json."


# --- 10. read instead of guessing -----------------------------------------------------------
NOTES = ("Meeting notes\n=============\n- Ship the bench harness\n- Ask Dad about the server\n\n"
         "The secret word is: marmalade-42\n\n- Order more coffee\n")


def _check_read_secret(folder: Path, reply: str):
    ok = re.search(r"marmalade[-\s]?42", reply, re.I) is not None
    return ok, "found it" if ok else f"answered {reply.strip()[:60]!r}"


TASKS: list[Task] = [
    Task("create_script", "create a file", "Create a Python script hello.py that prints exactly: Hello, bench!",
         _check_create_script, _ref_create_script),
    Task("fix_bug", "debug", "Running `python3 test_calc.py` fails. Find the bug in calc.py and fix it. Don't change test_calc.py.",
         _check_fix_bug, _ref_fix_bug, {"calc.py": CALC, "test_calc.py": TEST_CALC}),
    Task("add_function", "edit code", "Add a function multiply(a, b) to utils.py that returns a times b. Keep the existing functions.",
         _check_add_function, _ref_add_function, {"utils.py": UTILS}),
    Task("surgical_edit", "edit without clobbering", 'In index.html, change the page title (the <title> tag) to "Bench Page". Change nothing else.',
         _check_surgical_edit, _ref_surgical_edit, {"index.html": PAGE}),
    Task("find_definition", "search", "Which file defines the function parse_order? Reply with just the file path.",
         _check_find_definition, lambda folder: "app/orders.py", FIND_FILES),
    Task("rename_symbol", "multi-file edit", "Rename the function calc_total to compute_total everywhere in this project. main.py must still run.",
         _check_rename, _ref_rename, RENAME_FILES),
    Task("run_and_report", "run commands", "Run compute.py and tell me the number it prints.",
         _check_run_report, lambda folder: "It prints 277050.", {"compute.py": COMPUTE}),
    Task("create_html", "create a file", "Create fruits.html: a valid HTML page with an <h1> heading that says Fruits and an unordered list of exactly three fruits.",
         _check_create_html, _ref_create_html),
    Task("edit_json", "edit data", 'In settings.json, set "debug" to true and add a key "version" with the value "2.0". Keep all other keys unchanged.',
         _check_edit_json, _ref_edit_json, {"settings.json": json.dumps(SETTINGS, indent=2) + "\n"}),
    Task("read_not_guess", "read before answering", "What is the secret word in notes.txt? Answer with just the word.",
         _check_read_secret, lambda folder: "marmalade-42", {"notes.txt": NOTES}),
]
TASKS_BY_NAME = {t.name: t for t in TASKS}
