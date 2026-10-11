#!/usr/bin/env python3
"""Find the largest context (num_ctx) each Ollama model can use while staying 100% on the GPU.

Run on the model server:  python3 tune-context.py [--apply] [--headroom-mb 700] [model ...]
  - Without --apply it only measures and prints a table.
  - With --apply it bakes the result into each model: `ollama create <model>` with PARAMETER num_ctx.
Models that can't fit on the GPU at all are left alone and reported.
"""

import argparse
import json
import subprocess
import tempfile
import urllib.request

API = "http://localhost:11434"
CANDIDATES = [4096, 8192, 12288, 14336, 16384, 20480, 24576, 32768, 40960, 49152, 65536, 98304, 131072]


def call(path, body=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def unload_all():
    for m in call("/api/ps").get("models", []):
        call("/api/generate", {"model": m["name"], "keep_alive": 0})


def free_vram_mb():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.split(",")
    return int(out[0]) - int(out[1])


def fits(model, ctx, headroom):
    """Load `model` with num_ctx=ctx; True if it's entirely on the GPU with `headroom` MB left."""
    unload_all()
    call("/api/generate", {"model": model, "prompt": "hi", "stream": False, "options": {"num_ctx": ctx, "num_predict": 1}})
    entry = next((m for m in call("/api/ps").get("models", []) if m["name"].split(":")[0] == model.split(":")[0]), None)
    on_gpu = bool(entry) and entry.get("size_vram", 0) >= entry.get("size", 1)
    free = free_vram_mb()
    unload_all()
    return on_gpu and free >= headroom, free, entry.get("size_vram", 0) * 100 // max(entry.get("size", 1), 1) if entry else 0


def model_max(model):
    info = call("/api/show", {"model": model}).get("model_info", {})
    return next((int(v) for k, v in info.items() if k.endswith(".context_length")), 131072)


def apply(model, ctx):
    with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False) as f:
        f.write(f"FROM {model}\nPARAMETER num_ctx {ctx}\n")
    subprocess.run(["ollama", "create", model, "-f", f.name], check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--headroom-mb", type=int, default=700)
    args = ap.parse_args()
    models = args.models or sorted(m["name"] for m in call("/api/tags")["models"])
    results = []
    for model in models:
        top = model_max(model)
        cands = [c for c in CANDIDATES if c < top] + [top]
        lo, hi, best = 0, len(cands) - 1, None
        while lo <= hi:  # binary search: bigger context only ever needs more memory
            mid = (lo + hi) // 2
            ok, free, pct = fits(model, cands[mid], args.headroom_mb)
            print(f"  {model:<22} {cands[mid]:>7,}  {'fits' if ok else 'no':<4}  gpu {pct:>3}%  free {free:>5} MB", flush=True)
            if ok:
                best, lo = cands[mid], mid + 1
            else:
                hi = mid - 1
        results.append((model, best, top))
        if best and args.apply:
            apply(model, best)
    print("\nRESULTS")
    for model, best, top in results:
        verdict = f"{best:,} of {top:,}" if best else f"doesn't fit on the GPU even at 4,096 (max {top:,})"
        print(f"  {model:<22} {verdict}{'  (applied)' if best and args.apply else ''}")


if __name__ == "__main__":
    main()
