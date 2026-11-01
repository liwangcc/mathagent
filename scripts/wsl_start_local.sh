#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${MATHAGENT_LOCAL_STATE_DIR:-$ROOT_DIR/.local}"
PID_FILE="$STATE_DIR/wsl-local.pid"
LOG_FILE="$STATE_DIR/wsl-local.log"
mkdir -p "$STATE_DIR"

if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        echo "local_backend=already_running pid=$pid"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

export WATCHFILES_FORCE_POLLING="${WATCHFILES_FORCE_POLLING:-true}"
nohup "$ROOT_DIR/scripts/wsl_run_local.sh" --reload --log-level warning \
    >>"$LOG_FILE" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"

for _ in {1..20}; do
    if curl --fail --silent --show-error http://127.0.0.1:"${MATHAGENT_LOCAL_PORT:-8080}"/api/health >/dev/null 2>&1; then
        echo "local_backend=ready pid=$pid"
        exit 0
    fi
    sleep 1
done

echo "Local backend did not become healthy. See $LOG_FILE" >&2
exit 1
