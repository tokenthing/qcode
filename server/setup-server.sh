#!/usr/bin/env bash
# Run on the model server (Ubuntu) with sudo. Exposes Ollama on the network and
# firewalls it so only the ZeroTier interface can reach port 11434.
#   scp server/setup-server.sh <user>@<server-ip>:/tmp/
#   ssh -t <user>@<server-ip> 'sudo ZT_IFACE=<zt-interface> bash /tmp/setup-server.sh'
# ZT_IFACE is the ZeroTier interface your clients connect through (see: ip -br addr | grep ^zt).
set -euo pipefail

ZT_IFACE="${ZT_IFACE:-}"
if [[ -z "$ZT_IFACE" ]]; then
  echo "Set ZT_IFACE to your ZeroTier interface. Found:" >&2
  ip -br addr | grep '^zt' >&2 || echo "  (none; is ZeroTier installed and joined?)" >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then echo "run with sudo" >&2; exit 1; fi

mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
# One model in VRAM at a time (switching models swaps them), one request at a time
# (a 2nd user waits instead of doubling KV-cache memory and spilling the model to CPU).
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
EOF
systemctl daemon-reload
systemctl restart ollama
echo "ollama restarted:"; ss -tlnp | grep 11434 || true

ufw allow OpenSSH
ufw allow in on "$ZT_IFACE" to any port 11434 proto tcp
ufw deny 11434/tcp
ufw --force enable
ufw status verbose
