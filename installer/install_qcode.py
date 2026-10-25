#!/usr/bin/env python3
"""qcode installer — sets up the qcode coding CLI on macOS, Linux (incl. Raspberry Pi) and Windows.

Just run it:
    python3 install_qcode.py            (Windows: py install_qcode.py)

Options:
    --server ADDR     model server address (IP, host:port or URL)
    --name NAME       name for that server in qcode (default: home)
    --model NAME      default model
    --password KEY    API key or password for the server (default: ollama)
    --yes             accept all defaults, no questions
    --reconfigure     ask for server/model again even if qcode is already configured
    --uninstall       remove qcode (keeps your settings and sessions)
    --purge           with --uninstall: also delete settings and sessions

Everything is installed for the current user only (no admin needed):
    macOS/Linux:  ~/.local/share/qcode   + command in ~/.local/bin/qcode
    Windows:      %LOCALAPPDATA%\\qcode   + command in %LOCALAPPDATA%\\qcode\\bin\\qcode.cmd
Settings live in ~/.config/qcode (config.toml, servers.json).
"""

import sys

MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "qcode needs Python %d.%d or newer, but this is Python %d.%d.\n"
        "  macOS:          brew install python   (or download from https://www.python.org/downloads/)\n"
        "  Debian/Pi/Ubuntu: sudo apt install python3 python3-venv\n"
        "  Windows:        install from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')\n"
        % (MIN_PYTHON + sys.version_info[:2])
    )
    sys.exit(1)

import argparse
import base64
import io
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Filled in by scripts/build_installer.py with a base64 zip of the qcode source.
PAYLOAD = None

# Build-time defaults (scripts/build_installer.py fills these from installer/defaults.json).
DEFAULT_SERVER = ""
DEFAULT_SERVER_NAME = "home"
DEFAULT_ZT_NETWORK = ""
DEFAULT_API_KEY = ""
PREFERRED_MODELS = ["qcoder-14b", "qcoder-7b", "qwen2.5-coder:14b", "qwen2.5-coder:7b"]

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
PATH_MARKER = "# added by the qcode installer"

if IS_WIN:
    APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "qcode"
    BIN_DIR = APP_DIR / "bin"
else:
    APP_DIR = Path.home() / ".local" / "share" / "qcode"
    BIN_DIR = Path.home() / ".local" / "bin"
VENV_DIR = APP_DIR / "venv"
SRC_DIR = APP_DIR / "src"
CONFIG_DIR = Path.home() / ".config" / "qcode"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")
VENV_QCODE = VENV_DIR / ("Scripts/qcode.exe" if IS_WIN else "bin/qcode")

ARGS = None  # parsed command line


# --- pretty output ---------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def _supports_color():
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if IS_WIN:
        os.system("")  # enables ANSI escapes on Windows 10+
    return True


COLOR = _supports_color()


def c(code, text):
    return "\033[%sm%s\033[0m" % (code, text) if COLOR else text


def banner():
    print()
    print(c("1;35", "  ┌─────────────────────────────┐"))
    print(c("1;35", "  │   qcode installer           │"))
    print(c("1;35", "  │   local AI coding agent     │"))
    print(c("1;35", "  └─────────────────────────────┘"))
    print()


def step(n, total, msg):
    print("\n" + c("1;36", "[%d/%d] " % (n, total)) + c("1", msg))


def ok(msg):
    print("  " + c("32", "✓") + " " + msg)


def info(msg):
    print("  " + c("2", msg))


def warn(msg):
    print("  " + c("33", "! " + msg))


def die(msg, hint=None):
    print("\n  " + c("1;31", "✗ " + msg))
    if hint:
        for line in hint.strip().splitlines():
            print("    " + line)
    sys.exit(1)


def ask(question, default=""):
    if ARGS.yes or not sys.stdin.isatty():
        return default
    suffix = " [%s]" % default if default else ""
    try:
        answer = input("  " + c("1;35", "?") + " " + question + c("2", suffix) + " ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        die("Cancelled.")
    return answer or default


def confirm(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)"), "y" if default else "n")
    return answer.lower().startswith("y")


def run(cmd, **kw):
    return subprocess.run([str(x) for x in cmd], **kw)


# --- environment detection ----------------------------------------------------------
def describe_machine():
    system = "macOS" if IS_MAC else "Windows" if IS_WIN else "Linux"
    model = ""
    try:
        model = Path("/proc/device-tree/model").read_text(errors="ignore").strip("\x00 \n")
    except OSError:
        pass
    if "Raspberry Pi" in model:
        system = model
    elif not IS_WIN and not IS_MAC:
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("PRETTY_NAME="):
                    system = line.split("=", 1)[1].strip('"')
        except OSError:
            pass
    return "%s (%s), Python %s" % (system, platform.machine(), platform.python_version())


def is_raspberry_pi():
    try:
        return "Raspberry Pi" in Path("/proc/device-tree/model").read_text(errors="ignore")
    except OSError:
        return False


# --- install steps -------------------------------------------------------------------
def get_source():
    """Return a directory containing qcode's pyproject.toml."""
    if PAYLOAD:
        if SRC_DIR.exists():
            shutil.rmtree(SRC_DIR)
        SRC_DIR.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(PAYLOAD))) as zf:
            zf.extractall(SRC_DIR)
        return SRC_DIR
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent):
        pyproject = candidate / "pyproject.toml"
        if pyproject.exists() and 'name = "qcode"' in pyproject.read_text(encoding="utf-8"):
            return candidate
    die(
        "This installer doesn't contain the qcode source.",
        "Use the built installer (dist/install_qcode.py), or run this file from inside the qcode repo.",
    )


# Python releases with bugs that break qcode's dependencies.
# 3.10.0: typing.ForwardRef lacks is_class, which pydantic (used by openai) needs; fixed in 3.10.1.
BROKEN_PYTHONS = [((3, 10, 0), (3, 10, 1))]


def python_ok(version):
    if tuple(version[:2]) < MIN_PYTHON:
        return False
    return not any(lo <= tuple(version) < hi for lo, hi in BROKEN_PYTHONS)


def probe_python(cmd):
    """Return (major, minor, micro) for a Python command, or None if it doesn't run."""
    try:
        out = run(list(cmd) + ["-c", "import sys, venv; print(*sys.version_info[:3])"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return tuple(int(x) for x in out.stdout.split()) if out.returncode == 0 else None
    except ValueError:
        return None


def python_candidates():
    """Every Python on this machine worth trying, as command lists."""
    cands = [[sys.executable]]
    names = ["python3.%d" % m for m in range(15, MIN_PYTHON[1] - 1, -1)] + ["python3", "python"]
    cands += [[found] for found in (shutil.which(n) for n in names) if found]
    if IS_MAC:
        cands += [[str(p)] for p in sorted(Path("/Library/Frameworks/Python.framework/Versions").glob("3.*/bin/python3"))]
        cands += [[p] for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3") if os.path.exists(p)]
        # Apple's /usr/bin/python3 is a stub that pops an install dialog unless the Command Line Tools exist.
        if run(["xcode-select", "-p"], capture_output=True).returncode == 0:
            cands.append(["/usr/bin/python3"])
    elif IS_WIN and shutil.which("py"):
        listing = run(["py", "-0p"], capture_output=True, text=True).stdout
        cands += [[line.split()[-1]] for line in listing.splitlines() if line.strip().endswith(".exe")]
    elif not IS_WIN:
        cands += [[p] for p in ("/usr/bin/python3", "/usr/local/bin/python3") if os.path.exists(p)]
    seen, unique = set(), []
    for cmd in cands:
        key = os.path.realpath(cmd[0])
        if key not in seen:
            seen.add(key)
            unique.append(cmd)
    return unique


def find_python():
    """Pick the newest working Python for qcode's environment."""
    current = tuple(sys.version_info[:3])
    best = None
    for cmd in python_candidates():
        version = probe_python(cmd)
        if version and python_ok(version) and (best is None or version > best[1]):
            best = (cmd, version)
    if best is None:
        why = "Python %d.%d.%d has a bug that breaks qcode's packages." % current if not python_ok(current) else "No usable Python %d.%d+ found." % MIN_PYTHON
        die(
            why,
            "Install a current Python, then run this installer again:\n"
            "  macOS:   brew install python   or   https://www.python.org/downloads/ (3.12 or newer)\n"
            "  Linux:   sudo apt install python3 python3-venv\n"
            "  Windows: https://www.python.org/downloads/",
        )
    cmd, version = best
    if not python_ok(current):
        warn("Python %d.%d.%d has a bug that breaks qcode's packages, so the installer will use another Python." % current)
    ok("Using Python %d.%d.%d (%s)" % (version + (cmd[0],)))
    return cmd


def venv_works():
    if not VENV_PY.exists():
        return False
    version = probe_python([VENV_PY])
    return bool(version and python_ok(version))


def make_venv():
    if venv_works():
        ok("Reusing existing environment at %s" % VENV_DIR)
        return
    if VENV_DIR.exists():
        warn("Rebuilding the existing environment (it was made with an unsupported Python).")
        shutil.rmtree(VENV_DIR)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    python = find_python()
    result = run(python + ["-m", "venv", VENV_DIR], capture_output=True, text=True)
    if result.returncode != 0 or not VENV_PY.exists():
        output = (result.stdout + result.stderr).strip()
        if shutil.which("apt-get") and ("ensurepip" in output or "venv" in output):
            ver = "python%d.%d-venv" % (probe_python(python) or sys.version_info)[:2]
            warn("Python's venv module is missing (common on Debian, Ubuntu and Raspberry Pi OS).")
            if confirm("Install it now with 'sudo apt-get install -y python3-venv %s'?" % ver):
                run(["sudo", "apt-get", "update"])
                if run(["sudo", "apt-get", "install", "-y", "python3-venv", ver]).returncode != 0:
                    run(["sudo", "apt-get", "install", "-y", "python3-venv"])
                shutil.rmtree(VENV_DIR, ignore_errors=True)
                result = run(python + ["-m", "venv", VENV_DIR], capture_output=True, text=True)
        if result.returncode != 0 or not VENV_PY.exists():
            die("Couldn't create a Python virtual environment.", output + "\n\nOn Debian/Ubuntu/Pi: sudo apt install python3-venv")
    ok("Created environment at %s" % VENV_DIR)


def pip_install(source):
    if is_raspberry_pi():
        info("This can take a few minutes on a Raspberry Pi…")
    run([VENV_PY, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], capture_output=True)
    result = run(
        [VENV_PY, "-m", "pip", "install", "--quiet", "--upgrade", "--disable-pip-version-check", str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        # Same version number as before still has to pick up new code.
        result = run(
            [VENV_PY, "-m", "pip", "install", "--quiet", "--force-reinstall", "--no-deps", "--disable-pip-version-check", str(source)],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-15:])
        die("Installing qcode's packages failed (is this machine online?).", tail)
    version = run([VENV_PY, "-c", "import qcode; print(qcode.__version__)"], capture_output=True, text=True).stdout.strip()
    ok("Installed qcode %s and its packages" % version)


def install_launcher():
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        launcher = BIN_DIR / "qcode.cmd"
        launcher.write_text('@echo off\r\n"%s" %%*\r\n' % VENV_QCODE)
    else:
        launcher = BIN_DIR / "qcode"
        if launcher.is_symlink() or launcher.exists():
            launcher.unlink()
        launcher.symlink_to(VENV_QCODE)
    ok("Command installed: %s" % launcher)
    return add_to_path()


def _path_has_bin():
    entries = [os.path.normcase(os.path.abspath(p)) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return os.path.normcase(str(BIN_DIR)) in entries


def add_to_path():
    """Make sure BIN_DIR is on PATH for new terminals. Returns True if the user must open a new terminal."""
    if IS_WIN:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            parts = [p for p in current.split(";") if p]
            if any(os.path.normcase(p) == os.path.normcase(str(BIN_DIR)) for p in parts):
                return not _path_has_bin()
            winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts + [str(BIN_DIR)]))
        try:  # tell Explorer so new terminals see the change
            import ctypes

            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x2, 5000, None)
        except Exception:
            pass
        ok("Added %s to your user PATH" % BIN_DIR)
        return True

    if _path_has_bin():
        return False
    shell = Path(os.environ.get("SHELL", "")).name
    home = Path.home()
    if shell == "fish":
        rc_files = [home / ".config" / "fish" / "config.fish"]
        line = "fish_add_path %s" % BIN_DIR
    else:
        line = 'export PATH="%s:$PATH"' % BIN_DIR
        if shell == "zsh":
            rc_files = [home / ".zshrc"]
        elif shell == "bash":
            rc_files = [home / ".bashrc"] + ([home / ".bash_profile"] if IS_MAC else [])
        else:
            rc_files = [home / ".profile"]
    for rc in rc_files:
        existing = rc.read_text(encoding="utf-8", errors="replace") if rc.exists() else ""
        if PATH_MARKER in existing:
            continue
        rc.parent.mkdir(parents=True, exist_ok=True)
        with rc.open("a", encoding="utf-8") as f:
            f.write("\n%s\n%s\n" % (PATH_MARKER, line))
        ok("Added %s to PATH in %s" % (BIN_DIR, rc))
    return True


# --- server / ZeroTier --------------------------------------------------------------
def venv_python(code, *args):
    result = run([VENV_PY, "-c", code] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip().splitlines()[-1])
    return result.stdout.strip()


def normalize(address):
    return venv_python("import sys; from qcode.servers import normalize_url; print(normalize_url(sys.argv[1]))", address)


def fetch_models(url, api_key="ollama", timeout=4):
    try:
        with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/models", headers={"Authorization": "Bearer %s" % api_key}), timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return sorted(m["id"].replace(":latest", "") for m in data.get("data", []))
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def zerotier_cli():
    for candidate in ("zerotier-cli", r"C:\ProgramData\ZeroTier\One\zerotier-cli.bat"):
        found = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if found:
            return found
    return None


def offer_zerotier(url):
    """The server is unreachable: help get this machine onto the ZeroTier network."""
    warn("Can't reach the server at %s." % url)
    info("The model server is on a private ZeroTier network. This machine needs to join it.")
    cli = zerotier_cli()
    if not cli:
        if IS_WIN:
            info("Install ZeroTier from https://www.zerotier.com/download/ then run this installer again.")
            return False
        if IS_MAC and not shutil.which("brew"):
            info("Install ZeroTier from https://www.zerotier.com/download/ then run this installer again.")
            return False
        how = "brew install --cask zerotier-one" if IS_MAC else "curl -s https://install.zerotier.com | sudo bash"
        if not confirm("Install ZeroTier now? (runs: %s)" % how):
            return False
        if IS_MAC:
            run(["brew", "install", "--cask", "zerotier-one"])
        else:
            run(["bash", "-c", "curl -s https://install.zerotier.com | sudo bash"])
        cli = zerotier_cli()
        if not cli:
            warn("ZeroTier didn't install. Get it from https://www.zerotier.com/download/")
            return False
        ok("ZeroTier installed")
    network = ask("ZeroTier network ID to join (ask whoever runs the server)", DEFAULT_ZT_NETWORK)
    if not network:
        info("Skipped joining a network.")
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{16}", network):
        warn("That doesn't look like a 16-character network ID; skipping.")
        return False
    cmd = [cli, "join", network] if IS_WIN else ["sudo", cli, "join", network]
    if run(cmd).returncode != 0:
        warn("Joining failed. Try manually: %s" % " ".join(cmd))
        return False
    ok("Asked to join network %s" % network)
    print()
    print("  " + c("1;33", "Now the network owner must approve this device:"))
    print("    1. Open https://my.zerotier.com/network/%s" % network)
    print("    2. Under Members, tick 'Auth' for the new device")
    ask("Press Enter once it's approved (it can take ~30 seconds to connect)…", "")
    return True


def configure():
    config_file = CONFIG_DIR / "config.toml"
    if config_file.exists() and not ARGS.reconfigure and not ARGS.server:
        if confirm("qcode is already configured. Keep your current settings?"):
            ok("Kept settings in %s" % CONFIG_DIR)
            return None

    name = ARGS.name or ask("Name for the model server", DEFAULT_SERVER_NAME)
    address = ARGS.server or ask("Model server address (IP, host:port or URL)", DEFAULT_SERVER)
    if not address:
        info("No server address given; qcode will ask for it the first time you start it.")
        return None
    api_key = getattr(ARGS, "api_key", None)
    if api_key is None:
        api_key = ask("Password or API key (optional, press Enter for none)", DEFAULT_API_KEY) if not ARGS.yes else DEFAULT_API_KEY
    api_key = api_key.strip() if api_key else "ollama"
    while True:
        try:
            url = normalize(address)
        except RuntimeError as e:
            if ARGS.yes:
                die("Invalid server address %r: %s" % (address, e))
            warn(str(e) if address else "Please enter the server address.")
            address = ask("Model server address", DEFAULT_SERVER)
            continue
        models = fetch_models(url, api_key=api_key)
        if models is None and not ARGS.yes and offer_zerotier(url):
            for _ in range(6):
                models = fetch_models(url, api_key=api_key)
                if models is not None:
                    break
                info("still waiting for the network…")
                import time

                time.sleep(5)
        if models is not None:
            ok("Connected to %s · %d models" % (url, len(models)))
            break
        warn("Still can't reach %s." % url)
        if ARGS.yes or confirm("Save it anyway and fix the connection later?"):
            break
        address = ask("Model server address", address)

    model = ARGS.model
    if not model:
        default = next((m for m in PREFERRED_MODELS if models and m in models), (models or ["qcoder-14b"])[0])
        if models and len(models) > 1 and not ARGS.yes:
            print()
            for i, m in enumerate(models, 1):
                mark = c("1;35", "❯") if m == default else " "
                print("    %s %2d. %s" % (mark, i, m))
            pick = ask("Default model (number or name)", default)
            model = models[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(models) else pick
        else:
            model = default

    venv_python(
        """
import re, sys
from qcode import config
from qcode.servers import Server, ServerStore
url, model, name, key = sys.argv[1:5]
text = config.CONFIG_FILE.read_text(encoding="utf-8") if config.CONFIG_FILE.exists() else config.DEFAULT_TOML
text = re.sub(r'(?m)^base_url = .*$', 'base_url = "%s"' % url, text)
text = re.sub(r'(?m)^model = .*$', 'model = "%s"' % model, text)
text = re.sub(r'(?m)^api_key = .*$', 'api_key = "%s"' % key, text)
config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
config.CONFIG_FILE.write_text(text, encoding="utf-8")
store = ServerStore(seed=Server(name, url, key))
if store.get(name) is None or store.get(name).base_url != url or store.get(name).api_key != key:
    store.add(name, url, key)
store.use(name)
""",
        url,
        model,
        name,
        api_key,
    )
    ok("Saved settings: server %s → %s, model %s" % (name, url, model))
    return url


def verify():
    result = run([VENV_QCODE, "--help"], capture_output=True, text=True)
    if result.returncode != 0:
        die("qcode was installed but doesn't start.", (result.stderr or result.stdout)[-1500:])
    ok("qcode starts correctly")


def uninstall():
    banner()
    removed = False
    for path in (VENV_DIR, SRC_DIR):
        if path.exists():
            shutil.rmtree(path)
            removed = True
    for launcher in (BIN_DIR / "qcode", BIN_DIR / "qcode.cmd"):
        if launcher.is_symlink() or launcher.exists():
            launcher.unlink()
            removed = True
    if ARGS.purge:
        for path in (APP_DIR, CONFIG_DIR):
            if path.exists():
                shutil.rmtree(path)
                removed = True
        ok("Removed qcode, its settings and sessions.")
    else:
        ok("Removed qcode." if removed else "qcode wasn't installed.")
        info("Settings kept in %s (use --uninstall --purge to delete them too)." % CONFIG_DIR)


def main():
    global ARGS
    parser = argparse.ArgumentParser(description="Install the qcode coding CLI.")
    parser.add_argument("--server", help="model server address (IP, host:port or URL)")
    parser.add_argument("--name", help="name for the server (default: home)")
    parser.add_argument("--model", help="default model")
    parser.add_argument("--key", "--api-key", "--password", dest="api_key", help="API key or password for the server")
    parser.add_argument("--yes", "-y", action="store_true", help="accept all defaults, no questions")
    parser.add_argument("--reconfigure", action="store_true", help="ask for server and model again")
    parser.add_argument("--uninstall", action="store_true", help="remove qcode")
    parser.add_argument("--purge", action="store_true", help="with --uninstall, also delete settings and sessions")
    ARGS = parser.parse_args()

    if ARGS.uninstall:
        uninstall()
        return

    banner()
    info(describe_machine())
    total = 5

    step(1, total, "Getting qcode")
    source = get_source()
    ok("Source ready")

    step(2, total, "Creating a private Python environment")
    make_venv()

    step(3, total, "Installing packages")
    pip_install(source)

    step(4, total, "Adding the qcode command")
    new_terminal = install_launcher()

    step(5, total, "Connecting to your model server")
    configure()
    verify()

    print()
    print(c("1;32", "  All done! 🎉"))
    if new_terminal:
        print("  Open a " + c("1", "new terminal window") + ", then:")
    else:
        print("  Now:")
    print("    " + c("1;36", "cd your-project-folder"))
    print("    " + c("1;36", "qcode"))
    print()
    info("Inside qcode: /help for commands, /servers to see servers, /add_api_server to add one.")
    info("To update later, run this installer again. To remove: --uninstall")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        die("Cancelled.")
