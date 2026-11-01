#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -n "${MATHAGENT_WSL_VENV:-}" ]]; then
    VENV_DIR="$MATHAGENT_WSL_VENV"
else
    VENV_DIR="$HOME/mathagent-venv"
fi
PORT="${MATHAGENT_LOCAL_PORT:-8080}"
DATABASE_PATH="${MATHAGENT_LOCAL_DB:-$ROOT_DIR/.local/server-runtime/mathagent.db}"

[[ -x "$VENV_DIR/bin/uvicorn" ]] || {
    echo "WSL venv is missing: $VENV_DIR" >&2
    echo "Create it with Python 3.11 and install requirements.txt first." >&2
    exit 1
}
[[ -f "$DATABASE_PATH" ]] || {
    echo "Local database is missing: $DATABASE_PATH" >&2
    echo "Run scripts/wsl_sync_server_runtime.sh first." >&2
    exit 1
}

export DATABASE_URL="sqlite:///$DATABASE_PATH"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT_DIR"

exec "$VENV_DIR/bin/uvicorn" app:app --host 127.0.0.1 --port "$PORT" "$@"
