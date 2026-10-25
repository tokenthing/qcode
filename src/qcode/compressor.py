"""Input token compression: reduces prompt tokens sent to the model.

Compresses older tool outputs and bloated history turns to slash input tokens
for local models with tight context windows. Also provides optional neural
compression via LLMLingua.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys

from .context import estimate_tokens

_MULTI_NEWLINE = re.compile(r"\n{3,}")


def is_llmlingua_available() -> bool:
    """Check if the optional neural prompt compressor (llmlingua) is installed."""
    return importlib.util.find_spec("llmlingua") is not None


def install_compressor(ui=None) -> bool:
    """Install the optional neural input compressor (llmlingua) into the current Python environment."""
    msg = "Installing input compressor (llmlingua)… this may take a minute"
    if ui and hasattr(ui, "info"):
        ui.info(msg)
    else:
        print(f"  {msg}")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "llmlingua"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            ok_msg = "Successfully installed llmlingua input compressor!"
            if ui and hasattr(ui, "ok"):
                ui.ok(ok_msg)
            else:
                print(f"  ✓ {ok_msg}")
            return True
        else:
            err = (proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout) else "failed"
            err_msg = f"Failed to install llmlingua: {err}"
            if ui and hasattr(ui, "error"):
                ui.error(err_msg)
            else:
                print(f"  ✗ {err_msg}")
            return False
    except Exception as e:
        err_msg = f"Failed to install llmlingua: {e}"
        if ui and hasattr(ui, "error"):
            ui.error(err_msg)
        else:
            print(f"  ✗ {err_msg}")
        return False


def compress_tool_content(content: str, max_chars: int = 400) -> str:
    """Compress older tool output into a concise summary while preserving crucial info."""
    if not isinstance(content, str) or len(content) <= max_chars:
        return content
    lines = content.splitlines()
    if len(lines) <= 10:
        head = content[: max_chars * 2 // 3]
        tail = content[-(max_chars // 3) :]
        return f"{head}\n... [omitted {len(content) - max_chars} chars] ...\n{tail}"
    head_lines = lines[:4]
    tail_lines = lines[-3:]
    omitted = len(lines) - 7
    return "\n".join(head_lines) + f"\n... [{omitted} lines omitted to reduce input tokens] ...\n" + "\n".join(tail_lines)


def compress_messages(
    messages: list[dict],
    max_tool_chars: int = 400,
    keep_recent_tools: int = 2,
) -> list[dict]:
    """Return a copy of messages with earlier tool outputs compressed to drastically reduce input tokens."""
    if len(messages) <= 2:
        return messages

    out = []
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    recent_tools = set(tool_indices[-keep_recent_tools:]) if keep_recent_tools > 0 else set()

    for i, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content") or ""

        # System message and recent tool results are kept intact
        if i == 0 or i in recent_tools:
            out.append(m)
            continue

        if role == "tool" and isinstance(content, str):
            compressed_content = compress_tool_content(content, max_chars=max_tool_chars)
            new_m = dict(m)
            new_m["content"] = compressed_content
            out.append(new_m)
        else:
            if isinstance(content, str) and "\n\n\n" in content:
                new_m = dict(m)
                new_m["content"] = _MULTI_NEWLINE.sub("\n\n", content)
                out.append(new_m)
            else:
                out.append(m)

    return out


def compression_ratio(original_messages: list[dict], compressed_messages: list[dict]) -> tuple[int, int, float]:
    """(original_tokens, compressed_tokens, percent_saved)"""
    orig = estimate_tokens(original_messages)
    comp = estimate_tokens(compressed_messages)
    saved = max(0.0, (orig - comp) / max(orig, 1) * 100.0)
    return orig, comp, saved
