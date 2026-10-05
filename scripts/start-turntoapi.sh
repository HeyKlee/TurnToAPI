#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "\${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="\${TURNTOAPI_HOST:-$(cat .turntoapi-bind 2>/dev/null || printf '127.0.0.1')}"

exec "$ROOT/.venv/bin/python" \
  "$ROOT/turn_to_api_server.py" \
  --host "$HOST" \
  --port "\${TURNTOAPI_PORT:-8000}" \
  --config "$ROOT/config.yaml"
