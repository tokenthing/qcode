# qcode

A Claude-Code-style terminal coding agent for **your own** models. It runs Qwen2.5-Coder (or any OpenAI-compatible model) on a PC you control, and can be reached from anywhere over a private network like ZeroTier.

```
laptop / Pi / desktop (qcode) ──ZeroTier──▶ GPU box running Ollama :11434
```

It reads, searches and edits your code and runs commands. Every change is shown as a diff and needs your approval. It also handles sessions, project memory, `/undo`, multiple servers and self-updates.

## Install

Download **`install_qcode.py`** from the [latest release](https://github.com/tokenthing/qcode/releases/latest) and run it:

```sh
python3 install_qcode.py        # Windows: py install_qcode.py
```

It works on macOS, Linux, Raspberry Pi and Windows, needs Python 3.9+, and doesn't need admin rights. The installer:
1. Picks the newest working Python on the machine (it skips known-broken releases such as 3.10.0) and creates a private environment in `~/.local/share/qcode` (Windows: `%LOCALAPPDATA%\qcode`). On Debian, Ubuntu or Raspberry Pi OS it offers to `apt install python3-venv` if needed.
2. Installs qcode and adds a `qcode` command to your PATH (zsh, bash, fish or the Windows user PATH).
3. Asks for your model server (there's no built-in default), tests it, and lets you choose a default model. Skip it and qcode asks the first time it starts. Either way it's saved, so you only enter it once.
4. If the server is unreachable, offers to install ZeroTier and join your network. The network owner then approves the device at my.zerotier.com.

Run it again to reinstall. Flags: `--yes`, `--server ADDR [--name NAME] [--model M] [--password KEY]`, `--reconfigure`, `--uninstall [--purge]`.

**Sharing with family or friends:** create `installer/defaults.json` (it's git-ignored):
```json
{"server_name": "home", "zerotier_network": "0123456789abcdef"}
```
Then run `python3 scripts/build_installer.py`. `dist/install_qcode.py` will have your ZeroTier network pre-filled; people still type the server address themselves. (You can add `"server": "…"` to pre-fill that too.)

## Updating

qcode checks GitHub for a new release once a day and shows `⬆ vX.Y.Z` in the bottom bar.
- `/update` shows the release notes, installs the update and restarts with your conversation kept.
- `/config set auto_update auto` installs updates automatically at startup. `off` disables the check.

## Usage

```sh
cd your/project
qcode                              # interactive
qcode -p "fix the failing test"    # one shot
qcode -r                           # resume the last session here: replays the chat and lists its files
qcode -m qcoder-7b                 # pick a model
qcode -s laptop                    # use a saved server
qcode --base-url 192.168.1.50      # one-off server (IP, host:port or URL)
qcode --mode auto-edit             # start in a permission mode (ask, auto-edit, auto, plan)
```

The bottom bar shows the model, the active server and its address (green dot means online), a context gauge, the project and git branch, the permission mode, and available updates.

### Permission modes

Press **Shift+Tab** to cycle through them, or use `/mode <name>`. The startup default is in `/settings`.

| mode | what happens |
|---|---|
| `⏵ ask` (default) | asks before every file edit and command |
| `⏵⏵ auto-edit` | file edits happen automatically; commands still ask |
| `⚡ auto` | everything happens automatically (same as `--yolo`) |
| `⏸ plan` | read-only: the model looks around and proposes a plan, but changes nothing |

### Settings

`/settings` opens a menu (arrow keys, Enter to change, Esc to close) with these options:
- permission mode
- context size
- default model
- temperature (default: 0.8)
- max completion tokens (default: 4096)
- input token compression
- auto-continue (nudges the model when it says "I'll do X" and stops)
- read-before-overwrite protection
- when to compact the history
- max steps
- tool output limit
- update behaviour

Everything is saved to `~/.config/qcode/config.toml`, which is fully commented if you'd rather edit it by hand (`/config edit`).

### Context size

`/context` shows what's filling the context window. To resize it:
```
/context set 16k      # or 16384
/context set max      # the most the model supports
```
On Ollama this updates the model on the server, so it applies to everyone using it. qcode then loads the model and warns you if it no longer fits on the GPU. For other servers it's saved in `config.toml`.

### Commands

| | |
|---|---|
| **Conversation** | `/clear` (restart qcode fresh; the old session stays saved) `/retry` (ask again) `/undo` (revert last turn's file edits) `/compact` `/compress` (input token reduction / install) `/context` (token breakdown) `/copy` (last reply → clipboard) `/export [file]` (Markdown) `/resume [n]` `/save` |
| **Models & servers** | `/model [name]` `/servers` (live ping) `/server <name>` `/add_api_server <name> <ip\|host:port\|url> [key]` `/remove_api_server <name>` |
| **Project** | `/diff` (uncommitted git changes) `/init` (write QCODE.md) `/memory` `/cwd` |
| **Web** | `/web <question>` (answers with web access on) · otherwise web tools turn on when you ask for something online |
| **Settings** | `/settings` (menu) · `/mode [ask\|auto-edit\|auto\|plan]` · `/context set <tokens>` · `/config` · `/config set <key> <value>` · `/config edit` · `/permissions [reset\|remove n]` · `/tools` |
| **App** | `/status` `/doctor` (diagnostics) `/version` `/update` `/help` `/exit` |
| **Command line** | `qcode bench` — score models (see below) |
| **Shell** | `!git status` runs a command directly; its output is sent along with your next message |

Tab completes commands, subcommands, server names, model names and setting keys.

### Servers

Any OpenAI-compatible server works, including Ollama, LM Studio, llama.cpp, vLLM and hosted APIs. Servers are saved in `~/.config/qcode/servers.json`.
```
/add_api_server gpu 192.168.1.50            # → http://192.168.1.50:11434/v1
/add_api_server lms 192.168.1.60:1234       # → http://192.168.1.60:1234/v1
/add_api_server cloud https://api.example.com/v1 sk-...
```
qcode reads the context size (`num_ctx`) from Ollama automatically. For other servers, set it in `config.toml`:
```toml
[models."Qwen3-27B"]
num_ctx = 32768
```

### Config & memory

- `~/.config/qcode/config.toml` holds every setting from `/settings`, the allow rules and per-model context sizes for non-Ollama servers. Each line is commented.
- Per-project approvals are saved in `.qcode/settings.json`.
- `QCODE.md` in the project root (and `~/.config/qcode/QCODE.md`) is added to the system prompt. Use `/init` to generate one.

## Web access

qcode can search the web (DuckDuckGo), read pages and check the weather (Open-Meteo). It needs no API keys. **It only goes online when your message asks for it**, for example:

```
what's the weather today in my location
search the web for the asyncio TaskGroup docs
look up the latest release of ripgrep
summarize https://docs.python.org/3/whatsnew/3.13.html
/web what changed in Django 5.2        ← force web access for one question
```

Ordinary coding requests never offer the model any web tools.
- **Web access setting** (`/settings`): `when-asked` (the default), `always` or `off`.
- **Location setting**, e.g. `Bangkok`: used for weather and local questions, and only sent on web requests. If it's not set, the model asks you.

## Benchmarking models

Which model should you actually use? `qcode bench` runs 10 small, self-checking coding tasks against your models and scores them:

```sh
qcode bench                                  # your default model
qcode bench --models qcoder-14b,qwen3,llama3.1
qcode bench --models all --repeat 3          # every model, 3 tries each (fairer, slower)
qcode bench --tasks fix_bug,surgical_edit    # just some tasks   (qcode bench --list shows them)
qcode bench --history                        # scores from earlier runs
```

The tasks cover creating files, fixing a bug until the tests pass, adding a function, a one-line change to a 120-line file *without wiping the rest*, finding a definition, renaming across files, running a script and reporting its output, creating HTML, editing JSON while keeping the other keys, and reading a file instead of guessing. Each task runs in a throwaway folder with approvals on auto. Dangerous shell commands (`sudo`, paths outside the folder, network tools…) are blocked.

The scoreboard shows the pass rate, average time, tokens per second, failed tool calls, unreadable tool calls, nudges and how much of the model fits on the GPU. A per-task grid compares models side by side. Results are saved as JSON in `~/.local/share/qcode/bench/`.

## Hosting the model (Ollama + ZeroTier)

On the GPU machine (Linux):
```sh
curl -fsSL https://ollama.com/install.sh | sh
curl -s https://install.zerotier.com | sudo bash && sudo zerotier-cli join <network-id>
ollama pull qwen2.5-coder:14b
ollama create qcoder-14b -f server/Modelfile.qcoder14     # bakes in the context size
sudo ZT_IFACE=<zt-interface> bash server/setup-server.sh  # listen on the network, firewall to ZeroTier only
```
`setup-server.sh` keeps one model in VRAM at a time (switching swaps it) and serves one request at a time (a second user briefly waits instead of pushing the model onto the CPU). It also enables flash attention and a q8_0 KV cache, which halves context memory.

To give every model the largest context that still fits fully on the GPU:
```sh
python3 server/tune-context.py            # measure only
python3 server/tune-context.py --apply    # bake the result into each model (num_ctx)
```
Re-run it after `ollama pull`, because pulling resets a model's settings. On an 11GB GTX 1080 Ti, the 14B model fits 14k context fully on the GPU at about 24 tok/s, and the 7B fits 32k at about 50 tok/s.

## How it works

- `agent.py` runs the loop: stream the reply, run its tool calls, feed the results back, and repeat until the model answers without tools (at most `max_steps`). File edits are checkpointed per turn for `/undo`.
- Tools: `read_file` (paged), `write_file`, `edit_file` (exact unique match), `list_dir`, `glob`, `grep`, `bash`. File tools can't leave the project root. Web tools (`web_search`, `web_fetch`, `weather`) are added only on turns that ask for something online.
- Read-only tools run freely. Writes and `bash` follow the permission mode, and in `ask` mode you approve once, for the session, or for the project. Allow rules never auto-approve chained shell commands (`;`, `&&`, `|`, `$(`…).
- Safety net: the model can't overwrite a file it hasn't read this session. An overwrite that would delete most of a file gets a red warning. `/undo` history is saved with the session, so it survives restarts.
- Qwen2.5-Coder often writes tool calls as plain JSON. `toolparse.py` recovers them (bare JSON, fenced JSON or `<tool_call>` tags).
- Tool output is capped. At 80% of the context window, the history is summarized automatically.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
python3 scripts/build_installer.py
```
Releases: bump `__version__` in `src/qcode/__init__.py`, commit, then `git tag vX.Y.Z && git push --tags`. CI tests on Linux, macOS and Windows, then publishes the release with `install_qcode.py` attached.
