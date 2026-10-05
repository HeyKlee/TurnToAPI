#!/usr/bin/env bash
set +e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop turntoapi.service >/dev/null 2>&1 || true
fi

pkill -f "$ROOT/turn_to_api_server.py" 2>/dev/null || true
pkill -f "$ROOT/turn_to_api_live_proxy.py" 2>/dev/null || true

echo "TurnToAPI stopped. Other Firefox sessions were not targeted."
