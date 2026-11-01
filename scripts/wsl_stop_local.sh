#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${MATHAGENT_LOCAL_STATE_DIR:-$ROOT_DIR/.local}"
PID_FILE="$STATE_DIR/wsl-local.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "local_backend=not_running"
    exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in {1..10}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
fi
rm -f "$PID_FILE"
echo "local_backend=stopped"
