#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/HeyKlee/TurnToAPI.git"
INSTALL_DIR="\${TURNTOAPI_HOME:-$HOME/Library/Application Support/TurnToAPI}"
VENV="$INSTALL_DIR/.venv"

echo "=========================================="
echo " TurnToAPI macOS Installer"
echo "=========================================="

if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install git python
  else
    echo "Git and Python 3 are required. Install Homebrew or Python 3 first." >&2
    exit 1
  fi
fi

mkdir -p "$(dirname "$INSTALL_DIR")"

if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --prune origin
  git -C "$INSTALL_DIR" checkout main
  git -C "$INSTALL_DIR" pull --ff-only origin main
else
  rm -rf "$INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel setuptools
"$VENV/bin/python" -m pip install -r requirements.txt
"$VENV/bin/python" -m playwright install firefox

mkdir -p logs browser_debug playwright_profiles/arena playwright_profiles/chatgpt

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
fi

TSIP=""
if command -v tailscale >/dev/null 2>&1; then
  TSIP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
fi

if [ -n "$TSIP" ]; then
  HOST="$TSIP"
else
  HOST="127.0.0.1"
fi

printf '%s\n' "$HOST" > .turntoapi-bind

"$VENV/bin/python" -m py_compile \
  turn_to_api_server.py \
  browser_web_adapter.py \
  arena_model_registry.py \
  turn_to_api_live_proxy.py

cat > start-turntoapi.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
HOST="$(cat "$ROOT/.turntoapi-bind")"
exec "$ROOT/.venv/bin/python" \
  "$ROOT/turn_to_api_server.py" \
  --host "$HOST" \
  --port 8000 \
  --config "$ROOT/config.yaml"
EOF

cat > kill-turntoapi.sh <<'EOF'
#!/usr/bin/env bash
set +e
ROOT="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
pkill -f "$ROOT/turn_to_api_server.py" 2>/dev/null || true
pkill -f "$ROOT/turn_to_api_live_proxy.py" 2>/dev/null || true
echo "TurnToAPI stopped."
EOF

chmod +x start-turntoapi.sh kill-turntoapi.sh install-macos.sh

nohup "$INSTALL_DIR/start-turntoapi.sh" \
  >"$INSTALL_DIR/logs/turntoapi.stdout.log" \
  2>"$INSTALL_DIR/logs/turntoapi.stderr.log" &

echo
echo "TurnToAPI installed."
echo "Endpoint: http://$HOST:8000/v1"
echo "Arena uses a persistent headed Firefox; complete security verification manually if shown."
