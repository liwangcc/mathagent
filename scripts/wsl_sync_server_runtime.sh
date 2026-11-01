#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1090
[[ -f "$ROOT_DIR/.deploy.local" ]] && source "$ROOT_DIR/.deploy.local"
SERVER="${MATHAGENT_SERVER:-}"
REMOTE_DIR="${MATHAGENT_REMOTE_DIR:-/opt/mathagent}"
SSH_KEY="${MATHAGENT_SSH_KEY:-$HOME/.ssh/Hermes.pem}"
RUNTIME_DIR="${MATHAGENT_LOCAL_RUNTIME_DIR:-$ROOT_DIR/.local/server-runtime}"

[[ -f "$SSH_KEY" ]] || { echo "SSH key not found: $SSH_KEY" >&2; exit 1; }
mkdir -p "$RUNTIME_DIR/data"

tmp_key="$(mktemp)"
trap 'rm -f "$tmp_key"' EXIT
cp "$SSH_KEY" "$tmp_key"
chmod 600 "$tmp_key"

scp_args=(scp -q -i "$tmp_key" -o BatchMode=yes -o ConnectTimeout=15)
"${scp_args[@]}" "$SERVER:$REMOTE_DIR/.env" "$RUNTIME_DIR/.env"
"${scp_args[@]}" "$SERVER:$REMOTE_DIR/mathagent.db" "$RUNTIME_DIR/mathagent.db"
"${scp_args[@]}" "$SERVER:$REMOTE_DIR/data/ip_failures.db" "$RUNTIME_DIR/data/ip_failures.db"

chmod 600 "$RUNTIME_DIR/.env" "$RUNTIME_DIR"/*.db "$RUNTIME_DIR/data"/*.db
cp "$RUNTIME_DIR/.env" "$ROOT_DIR/.env.local"
printf '\nDATABASE_URL=sqlite:///%s\n' "$RUNTIME_DIR/mathagent.db" >> "$ROOT_DIR/.env.local"
chmod 600 "$ROOT_DIR/.env.local"

echo "runtime_sync=ok"
