#!/usr/bin/env bash
set -euo pipefail

REPO_URL="\${TURNTOAPI_REPO:-https://github.com/HeyKlee/TurnToAPI.git}"
INSTALL_DIR="\${TURNTOAPI_HOME:-$HOME/.local/share/turntoapi}"
VENV="$INSTALL_DIR/.venv"

echo "=========================================="
echo " TurnToAPI Linux Installer"
echo "=========================================="
echo "Install directory: $INSTALL_DIR"
echo

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y \
      git curl ca-certificates python3 python3-pip python3-venv \
      libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y git curl python3 python3-pip gtk3 dbus-glib libXt
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm git curl python python-pip gtk3 dbus-glib libxt
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper --non-interactive install git curl python3 python3-pip python3-virtualenv gtk3
  else
    echo "No supported package manager found (apt, dnf, pacman, zypper)." >&2
    exit 1
  fi
}

install_packages

mkdir -p "$(dirname "$INSTALL_DIR")"

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Updating existing TurnToAPI checkout..."
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

echo "Installing Playwright Firefox..."
"$VENV/bin/python" -m playwright install firefox

if command -v apt-get >/dev/null 2>&1; then
  sudo "$VENV/bin/python" -m playwright install-deps firefox || true
fi

mkdir -p \
  logs \
  browser_debug \
  playwright_profiles/arena \
  playwright_profiles/chatgpt

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
fi

TSIP=""
if command -v tailscale >/dev/null 2>&1; then
  TSIP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
fi

if [ -n "$TSIP" ]; then
  BIND_HOST="$TSIP"
  echo "Tailscale detected: binding API to $BIND_HOST"
else
  BIND_HOST="127.0.0.1"
  echo "Tailscale not detected: binding API to loopback only."
fi

printf '%s\n' "$BIND_HOST" > .turntoapi-bind

cat > start-turntoapi.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="$(cat .turntoapi-bind 2>/dev/null || printf '127.0.0.1')"

export TURNTOAPI_WEBDRIVER_TIMEOUT="\${TURNTOAPI_WEBDRIVER_TIMEOUT:-900}"
export TURNTOAPI_ARENA_GENERATION_TIMEOUT="\${TURNTOAPI_ARENA_GENERATION_TIMEOUT:-900}"
export TURNTOAPI_CLOUDFLARE_AUTO_WAIT="\${TURNTOAPI_CLOUDFLARE_AUTO_WAIT:-180}"
export TURNTOAPI_CLOUDFLARE_HUMAN_AFTER="\${TURNTOAPI_CLOUDFLARE_HUMAN_AFTER:-4}"
export TURNTOAPI_CLOUDFLARE_STREAK_EXPIRY="\${TURNTOAPI_CLOUDFLARE_STREAK_EXPIRY:-900}"

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

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop turntoapi.service >/dev/null 2>&1 || true
fi

pkill -f "$ROOT/turn_to_api_server.py" 2>/dev/null || true
pkill -f "$ROOT/turn_to_api_live_proxy.py" 2>/dev/null || true

echo "TurnToAPI stopped. Unrelated Firefox sessions were not targeted."
EOF

chmod +x start-turntoapi.sh kill-turntoapi.sh install.sh

"$VENV/bin/python" -m py_compile \
  turn_to_api_server.py \
  browser_web_adapter.py \
  arena_model_registry.py \
  turn_to_api_live_proxy.py

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"

  systemctl --user import-environment \
    DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS \
    >/dev/null 2>&1 || true

  cat > "$HOME/.config/systemd/user/turntoapi.service" <<EOF
[Unit]
Description=TurnToAPI OpenAI-compatible browser API
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/start-turntoapi.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now turntoapi.service

  echo
  echo "systemd user service installed and started."
else
  echo
  echo "systemd user service unavailable; starting TurnToAPI in the background."
  nohup "$INSTALL_DIR/start-turntoapi.sh" \
    >"$INSTALL_DIR/logs/turntoapi.stdout.log" \
    2>"$INSTALL_DIR/logs/turntoapi.stderr.log" &
fi

echo
echo "Waiting for API health..."

READY=0
for _ in $(seq 1 40); do
  if curl -fsS "http://$BIND_HOST:8000/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.5
done

echo
if [ "$READY" -eq 1 ]; then
  echo "TurnToAPI installed successfully."
  echo "OpenAI-compatible endpoint: http://$BIND_HOST:8000/v1"
  echo "Health: http://$BIND_HOST:8000/health"
else
  echo "TurnToAPI installed, but health check did not pass yet." >&2
  echo "Inspect: systemctl --user status turntoapi --no-pager" >&2
  echo "Or run: $INSTALL_DIR/start-turntoapi.sh" >&2
fi

echo
echo "IMPORTANT:"
echo "Arena uses a headed persistent Firefox profile."
echo "If Arena presents a security verification, complete it manually."
echo "TurnToAPI does not solve or bypass CAPTCHA/Cloudflare challenges."
echo
echo "Kill switch:"
echo "  $INSTALL_DIR/kill-turntoapi.sh"
