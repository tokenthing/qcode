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
3. Asks for your model server, tests it, and lets you choose a default model.
4. If the server is unreachable, offers to install ZeroTier and join your network. The network owner then approves the device at my.zerotier.com.

Run it again to reinstall. Flags: `--yes`, `--server ADDR --name NAME --model M`, `--reconfigure`, `--uninstall [--purge]`.

**Sharing with family or friends:** create `installer/defaults.json` (it's git-ignored):
```json
{"server": "192.168.1.50", "server_name": "home", "zerotier_network": "0123456789abcdef"}
```
Then run `python3 scripts/build_installer.py`. `dist/install_qcode.py` will have your server and network pre-filled.

## Updating

qcode checks GitHub for a new release once a day and shows `⬆ vX.Y.Z` in the bottom bar.
- `/update` shows the release notes, installs the update and restarts with your conversation kept.
- `/config set auto_update auto` installs updates automatically at startup. `off` disables the check.

## Usage

```sh
cd your/project
qcode                              # interactive
qcode -p "fix the failing test"    # one shot
qcode -r                           # resume the last session in this folder
qcode -m qcoder-7b                 # pick a model
qcode -s laptop                    # use a saved server
qcode --base-url 192.168.1.50      # one-off server (IP, host:port or URL)
qcode --yolo                       # skip approval prompts (careful)
```

The bottom bar shows the model, the active server and its address (green dot means online), a context gauge, the project and git branch, YOLO mode, and available updates.

### Commands

| | |
|---|---|
| **Conversation** | `/clear` `/retry` (ask again) `/undo` (revert last turn's file edits) `/compact` `/context` (token breakdown) `/copy` (last reply → clipboard) `/export [file]` (Markdown) `/resume [n]` `/save` |
| **Models & servers** | `/model [name]` `/servers` (live ping) `/server <name>` `/add_api_server <name> <ip\|host:port\|url> [key]` `/remove_api_server <name>` |
| **Project** | `/diff` (uncommitted git changes) `/init` (write QCODE.md) `/memory` `/cwd` |
| **Settings** | `/config` · `/config set <key> <value>` · `/config edit` · `/config path` · `/permissions [reset\|remove n]` · `/yolo` · `/tools` |
| **App** | `/status` `/doctor` (diagnostics) `/version` `/update` `/help` `/exit` |
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

- `~/.config/qcode/config.toml` holds the default model, `max_steps`, `tool_output_limit`, `auto_update`, allow rules and fallback context sizes. Change it with `/config set` or `/config edit`.
- Per-project approvals are saved in `.qcode/settings.json`.
- `QCODE.md` in the project root (and `~/.config/qcode/QCODE.md`) is added to the system prompt. Use `/init` to generate one.

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
- Tools: `read_file` (paged), `write_file`, `edit_file` (exact unique match), `list_dir`, `glob`, `grep`, `bash`. File tools can't leave the project root.
- Read-only tools run freely. Writes and `bash` ask first (once, always for the session, or always for the project). Allow rules never auto-approve chained shell commands (`;`, `&&`, `|`, `$(`…).
- Qwen2.5-Coder often writes tool calls as plain JSON. `toolparse.py` recovers them (bare JSON, fenced JSON or `<tool_call>` tags).
- Tool output is capped. At 80% of the context window, the history is summarized automatically.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
python3 scripts/build_installer.py
```
Releases: bump `__version__` in `src/qcode/__init__.py`, commit, then `git tag vX.Y.Z && git push --tags`. CI tests on Linux, macOS and Windows, then publishes the release with `install_qcode.py` attached.
