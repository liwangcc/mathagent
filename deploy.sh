#!/usr/bin/env bash
# MathAgent production release.
#
# Usage:
#   cd /d/MathAgent && bash deploy.sh [--dry-run]
#
# The script intentionally leaves nginx unchanged.  A healthy loopback service
# is required before a separate, reviewed public-vhost switch can remove the
# maintenance page.
set -euo pipefail

# 部署目标与密钥不进入仓库：复制 .deploy.local.example 为 .deploy.local 后填写。
DEPLOY_LOCAL="$(cd "$(dirname "$0")" && pwd)/.deploy.local"
if [[ -f "$DEPLOY_LOCAL" ]]; then
    # shellcheck disable=SC1090
    source "$DEPLOY_LOCAL"
fi

SSH_KEY="${MATHAGENT_SSH_KEY:-$HOME/.ssh/Hermes.pem}"
SERVER="${MATHAGENT_SERVER:-}"
REMOTE_DIR="${MATHAGENT_REMOTE_DIR:-/opt/mathagent}"
# `pwd -W` exists in Git Bash but not WSL; keep LOCAL_DIR in the shell's
# native form so the same release script works from either environment.
# Native Git cannot resolve an MSYS '/d/...' path with -C when path
# conversion is disabled, so GIT_WORKTREE prefers the Windows form.
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
GIT_WORKTREE="$(cd "$(dirname "$0")" && { pwd -W 2>/dev/null || pwd; })"
PYTHON_CMD="${MATHAGENT_PYTHON:-$LOCAL_DIR/venv_local/Scripts/python.exe}"
TEST_JWT_SECRET="${MATHAGENT_TEST_JWT_SECRET:-test-only-fixed-secret-0123456789abcdef0123456789abcdef0123456789abcdef}"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
elif [[ $# -gt 0 ]]; then
    echo "Usage: bash deploy.sh [--dry-run]" >&2
    exit 2
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
ok() { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die() { echo -e "${RED}[ERR ]${NC} $*" >&2; exit 1; }

run_release_tests() {
    # WSL interop does not reliably forward `env VAR=value` assignments to a
    # Windows .exe. Set the release-only environment inside the test Python
    # process instead, and use a disposable database outside the worktree.
    # This keeps local credentials and the developer's runtime database out of
    # the release gate while preserving the real interpreter and test suite.
    # The equivalent direct invocation remains: "$PYTHON_CMD" -m pytest -q
    # tests --ignore=desktop.
    (cd "$LOCAL_DIR" && "$PYTHON_CMD" scripts/release_test_runner.py \
        "$RELEASE_ID" "$TEST_JWT_SECRET" "$@")
}

[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || die "MATHAGENT_REMOTE_DIR must be a simple absolute path"

require_file() {
    [[ -f "$1" ]] || die "Missing required file: $1"
}

for command in git tar gzip ssh sha256sum npm; do
    command -v "$command" >/dev/null 2>&1 || die "Missing command: $command"
done
require_file "$SSH_KEY"
require_file "$PYTHON_CMD"

ARTIFACT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mathagent-release.XXXXXX")"
RELEASE_ID="$(date +%Y%m%d_%H%M%S)"
RELEASE_TAR="$ARTIFACT_DIR/mathagent-release.tar"
RELEASE_ARCHIVE="$RELEASE_TAR.gz"
trap 'rm -rf "$ARTIFACT_DIR"' EXIT

build_release_archive() {
    info "Running backend tests with the release interpreter..."
    # The Codex-owned desktop worker has its own isolated test package and
    # is intentionally excluded from the backend release gate.
    # Release tests must not inherit local provider credentials. Hermes is part
    # of the supported chat path and is therefore included in this release gate.
    run_release_tests -q tests --ignore=desktop

    info "Running frontend tests and production build..."
    (
        cd "$LOCAL_DIR/frontend"
        npm run test
        npm run build
    )
    [[ -f "$LOCAL_DIR/frontend/dist/index.html" ]] || die "Frontend build did not produce dist/index.html"

    info "Checking source whitespace..."
    # The release may be built from a Windows worktree through WSL. Normalize
    # CRLF before checking so Git does not report every inherited line ending
    # as a newly introduced trailing-space error.
    git -C "$GIT_WORKTREE" -c core.autocrlf=true diff --check

    info "Creating a controlled release archive..."
    (
        cd "$LOCAL_DIR"
        # Use tracked paths so ignored credentials, local venvs, databases,
        # uploads, node_modules, and agent state cannot enter the artifact.
        # Exclude tests/docs/experiments from the production runtime payload.
        git -C "$GIT_WORKTREE" ls-files -z | while IFS= read -r -d '' path; do
            case "$path" in
                .cache/*|.motd_shown|data/*|docs/*|experiments/*|tests/*|frontend/tests/*)
                    continue
                    ;;
            esac
            # A tracked path may be intentionally deleted in the working
            # tree.  Do not make an otherwise valid release fail while asking
            # tar to read a file that is no longer present.
            [[ -e "$path" ]] || continue
            printf '%s\0' "$path"
        done | tar --null --verbatim-files-from --files-from=- -cf "$RELEASE_TAR"

        # Explicitly allow newly-created runtime modules that are part of this
        # reviewed release but have not yet been committed.  Never broaden this
        # to all untracked files: desktop packages, credentials and local output
        # may coexist in the worktree.
        runtime_release_files=(
            services/accelerator_business.py
            services/system_readiness.py
            services/retry_backoff.py
            services/template_canonicalization_contract.py
            services/answer_sheet_acceleration.py
            services/filename_identity.py
            services/question_classification.py
            services/review_marks.py
            services/evidence_label_layout.py
            services/processing_errors.py
            services/processing_http.py
            services/template_max_owned.py
            services/template_one_three.py
            services/template_slots.py
            services/template_output_evidence.py
            routers/courseware.py
            models_courseware.py
            services/courseware.py
            services/courseware_3d.py
        )
        for path in "${runtime_release_files[@]}"; do
            [[ -f "$path" ]] || die "Release runtime file is missing: $path"
            if ! git -C "$GIT_WORKTREE" ls-files --error-unmatch "$path" >/dev/null 2>&1; then
                tar -rf "$RELEASE_TAR" "$path"
            fi
        done
        if [[ -d services/courseware_vendor ]]; then
            tar -rf "$RELEASE_TAR" services/courseware_vendor
        fi

        # Vite output is intentionally ignored by Git, so append the freshly
        # built production assets explicitly.
        tar -rf "$RELEASE_TAR" frontend/dist
    )

    local archive_manifest="$ARTIFACT_DIR/release-manifest.txt"
    tar -tf "$RELEASE_TAR" > "$archive_manifest"
    grep -qx 'frontend/dist/index.html' "$archive_manifest" \
        || die "Release archive is missing frontend/dist/index.html"
    for path in \
        services/accelerator_business.py \
        services/system_readiness.py \
        services/retry_backoff.py \
        services/template_canonicalization_contract.py \
        services/answer_sheet_acceleration.py \
        services/filename_identity.py \
        services/question_classification.py \
        services/evidence_label_layout.py \
        services/processing_errors.py \
        services/processing_http.py \
        services/template_max_owned.py \
        services/template_one_three.py \
        services/template_slots.py \
        services/template_output_evidence.py
    do
        grep -qx "$path" "$archive_manifest" \
            || die "Release archive is missing $path"
    done
    if grep -Eq '(^|/)\.env(\.local)?$|(^|/)(venv|venv_local|node_modules)(/|$)' "$archive_manifest"; then
        die "Release archive contains a protected local runtime artifact"
    fi
    gzip -9 "$RELEASE_TAR"
    ok "Release archive verified: $(du -h "$RELEASE_ARCHIVE" | cut -f1)"
}

build_release_archive

if $DRY_RUN; then
    warn "Dry run complete: no files were sent and no server state changed."
    exit 0
fi

ARCHIVE_SHA="$(sha256sum "$RELEASE_ARCHIVE" | awk '{print $1}')"
REMOTE_ARCHIVE="/tmp/mathagent-release-$RELEASE_ID.tar.gz"
SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15)
printf -v REMOTE_DIR_Q '%q' "$REMOTE_DIR"
printf -v RELEASE_ID_Q '%q' "$RELEASE_ID"
printf -v ARCHIVE_SHA_Q '%q' "$ARCHIVE_SHA"

info "Uploading verified release archive..."
"${SSH[@]}" "$SERVER" "cat > '$REMOTE_ARCHIVE'" < "$RELEASE_ARCHIVE"
ok "Archive upload completed"

info "Installing release to the protected server staging area..."
"${SSH[@]}" "$SERVER" "bash -s -- $REMOTE_DIR_Q $RELEASE_ID_Q $ARCHIVE_SHA_Q" <<'REMOTE'
set -Eeuo pipefail
umask 027

REMOTE_DIR="$1"
RELEASE_ID="$2"
EXPECTED_SHA="$3"
REMOTE_ARCHIVE="/tmp/mathagent-release-$RELEASE_ID.tar.gz"
BACKUP_DIR="$REMOTE_DIR/backups/releases/$RELEASE_ID"
STAGE_DIR="$(mktemp -d /tmp/mathagent-stage.XXXXXX)"
ROLLBACK_STAGE=""
VENV_PYTHON="$REMOTE_DIR/venv/bin/python"
VENV_GUNICORN="$REMOTE_DIR/venv/bin/gunicorn"
DEPLOY_STARTED=0
SERVICE_WAS_ACTIVE=0
ROLLING_BACK=0
ROLLBACK_READY=0
STAGE_ENV_MOUNTS=()

persistent_excludes=(
    --exclude='/.env'
    --exclude='/.env.local'
    --exclude='/config_local.py'
    --exclude='/venv/'
    --exclude='/uploads/'
    --exclude='/output/'
    --exclude='/data/'
    --exclude='/backups/'
    --exclude='/logs/'
    --exclude='/.cache/'
    --exclude='/.hermes_mathagent/'
    --exclude='/学生档案/'
    --exclude='/批阅失败/'
    --exclude='/_dead_code_backup/'
    --exclude='*.db'
    --exclude='*.db-wal'
    --exclude='*.db-shm'
    --exclude='*.sqlite'
    --exclude='*.sqlite3'
)

cleanup() {
    local mount_path
    for mount_path in "${STAGE_ENV_MOUNTS[@]}"; do
        umount "$mount_path" 2>/dev/null || true
    done
    STAGE_ENV_MOUNTS=()
    rm -rf -- "$STAGE_DIR" "$REMOTE_ARCHIVE"
    if [[ -n "$ROLLBACK_STAGE" ]]; then
        rm -rf -- "$ROLLBACK_STAGE"
    fi
}

fail_remote() {
    echo "$*" >&2
    return 1
}

restore_database_backup() {
    "$VENV_PYTHON" - "$BACKUP_DIR" "$RELEASE_ID" <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

backup_dir = Path(sys.argv[1])
release_id = sys.argv[2]
metadata_file = backup_dir / "database-before.json"
if not metadata_file.exists():
    raise SystemExit("Database rollback metadata is missing")
metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
db_path = Path(metadata["path"])
if not db_path.is_absolute():
    raise SystemExit("Refusing to restore a non-absolute database path")
for suffix in ("-wal", "-shm"):
    sidecar = Path(f"{db_path}{suffix}")
    if sidecar.exists():
        sidecar.unlink()
if not metadata["existed"]:
    if db_path.exists():
        db_path.unlink()
    raise SystemExit(0)
source = backup_dir / "database-before.db"
if not source.is_file():
    raise SystemExit("Database rollback image is missing")
temporary = db_path.with_name(f".{db_path.name}.rollback-{release_id}")
shutil.copyfile(source, temporary)
os.chmod(temporary, int(metadata["mode"]))
os.chown(temporary, int(metadata["uid"]), int(metadata["gid"]))
os.replace(temporary, db_path)
PY
}

rollback() {
    local status="${1:-1}"
    ROLLING_BACK=1
    trap - EXIT ERR HUP INT TERM
    set +e
    if [[ "$DEPLOY_STARTED" == "1" ]]; then
        rollback_restore_status=0
        echo "Release failed; restoring the previous source, configuration, and database..." >&2
        systemctl stop mathagent
        if [[ "$ROLLBACK_READY" != "1" ]]; then
            echo "Release failed before a complete rollback set was created; source was not switched." >&2
        fi
        if [[ -f "$BACKUP_DIR/source-before.tar.gz" ]]; then
            ROLLBACK_STAGE="$(mktemp -d /tmp/mathagent-rollback.XXXXXX)"
            if tar -xzf "$BACKUP_DIR/source-before.tar.gz" -C "$ROLLBACK_STAGE"; then
                rsync -a --delete-delay --delay-updates \
                    "${persistent_excludes[@]}" "$ROLLBACK_STAGE/" "$REMOTE_DIR/"
                source_restore_status=$?
                if [[ "$source_restore_status" != "0" ]]; then
                    echo "Source rollback failed" >&2
                    rollback_restore_status=1
                fi
            else
                echo "Source rollback archive could not be extracted" >&2
                rollback_restore_status=1
            fi
        elif [[ "$ROLLBACK_READY" == "1" ]]; then
            echo "Source rollback archive is missing" >&2
            rollback_restore_status=1
        fi
        if [[ "$ROLLBACK_READY" == "1" && -f "$BACKUP_DIR/mathagent.service.before" ]]; then
            install -D -m 0644 "$BACKUP_DIR/mathagent.service.before" /etc/systemd/system/mathagent.service
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        elif [[ "$ROLLBACK_READY" == "1" && -f "$BACKUP_DIR/mathagent.service.missing" ]]; then
            rm -f /etc/systemd/system/mathagent.service
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        elif [[ "$ROLLBACK_READY" == "1" ]]; then
            echo "Systemd rollback image is missing" >&2
            rollback_restore_status=1
        fi
        if [[ "$ROLLBACK_READY" == "1" && -f "$BACKUP_DIR/20-resource-profile.conf.before" ]]; then
            install -D -m 0644 "$BACKUP_DIR/20-resource-profile.conf.before" \
                /etc/systemd/system/mathagent.service.d/20-resource-profile.conf
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        elif [[ "$ROLLBACK_READY" == "1" && -f "$BACKUP_DIR/20-resource-profile.conf.missing" ]]; then
            rm -f /etc/systemd/system/mathagent.service.d/20-resource-profile.conf
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        elif [[ "$ROLLBACK_READY" == "1" ]]; then
            echo "Resource-profile rollback image is missing" >&2
            rollback_restore_status=1
        fi
        if [[ "$ROLLBACK_READY" == "1" && -f "$BACKUP_DIR/gunicorn.conf.py.before" ]]; then
            install -D -m 0644 "$BACKUP_DIR/gunicorn.conf.py.before" /etc/mathagent/gunicorn.conf.py
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        elif [[ "$ROLLBACK_READY" == "1" && -f "$BACKUP_DIR/gunicorn.conf.py.missing" ]]; then
            rm -f /etc/mathagent/gunicorn.conf.py
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        elif [[ "$ROLLBACK_READY" == "1" ]]; then
            echo "Gunicorn rollback image is missing" >&2
            rollback_restore_status=1
        fi
        if [[ "$ROLLBACK_READY" == "1" ]]; then
            restore_database_backup
            database_restore_status=$?
            if [[ "$database_restore_status" != "0" ]]; then
                echo "Database rollback failed" >&2
                rollback_restore_status=1
            fi
        fi
        chgrp www-data "$REMOTE_DIR"
        if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        chmod 0775 "$REMOTE_DIR"
        if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        if [[ -f "$REMOTE_DIR/.env" ]]; then
            chown root:www-data "$REMOTE_DIR/.env"
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
            chmod 0640 "$REMOTE_DIR/.env"
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        fi
        systemctl daemon-reload
        if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
        if [[ "$SERVICE_WAS_ACTIVE" == "1" && "$rollback_restore_status" == "0" ]]; then
            systemctl start mathagent
            if [[ "$?" != "0" ]]; then rollback_restore_status=1; fi
            for _ in $(seq 1 10); do
                systemctl is-active --quiet mathagent && break
                sleep 1
            done
            if ! systemctl is-active --quiet mathagent; then
                echo "Previous MathAgent service did not recover" >&2
                rollback_restore_status=1
            fi
        fi
        if [[ "$rollback_restore_status" != "0" ]]; then
            systemctl stop mathagent
            cleanup
            exit 2
        fi
    fi
    cleanup
    exit "$status"
}

on_exit() {
    local status="$?"
    trap - EXIT
    if [[ "$status" != "0" && "$DEPLOY_STARTED" == "1" && "$ROLLING_BACK" == "0" ]]; then
        rollback "$status"
    fi
    cleanup
    exit "$status"
}

trap 'rollback 1' HUP INT TERM
trap on_exit EXIT

check_redis_ping() {
    local application_root="$1"
    (
        cd "$application_root"
        sudo -u www-data env PYTHONDONTWRITEBYTECODE=1 \
            "$VENV_PYTHON" - <<'PY'
import config
import redis

client = redis.Redis.from_url(
    config.REDIS_URL,
    socket_connect_timeout=3,
    socket_timeout=3,
)
try:
    if not client.ping():
        raise SystemExit("Redis PING was not acknowledged")
finally:
    client.close()
print("redis_ping=ok")
PY
    )
}

check_application_candidate() {
    local application_root="$1"
    (
        cd "$application_root"
        sudo -u www-data env \
            PYTHONDONTWRITEBYTECODE=1 \
            "MPLCONFIGDIR=$application_root/.cache/matplotlib" \
            "$VENV_PYTHON" - <<'PY'
import asyncio
import httpx

billable_http_attempted = False

def forbid_sync_http(*_args, **_kwargs):
    global billable_http_attempted
    billable_http_attempted = True
    raise RuntimeError("billable model HTTP request attempted during deployment readiness")

async def forbid_async_http(*_args, **_kwargs):
    global billable_http_attempted
    billable_http_attempted = True
    raise RuntimeError("billable model HTTP request attempted during deployment readiness")

httpx.Client.request = forbid_sync_http
httpx.AsyncClient.request = forbid_async_http

import app as app_module
from config import UPLOAD_DIR
from services.system_readiness import get_grading_infrastructure_readiness

if not getattr(app_module, "app", None):
    raise SystemExit("FastAPI application object is missing")
state = asyncio.run(get_grading_infrastructure_readiness(UPLOAD_DIR, 1))
if billable_http_attempted:
    raise SystemExit("deployment readiness attempted a billable model HTTP request")
if not isinstance(state, dict):
    raise SystemExit("Grading readiness did not return an object")
if state.get("status") not in {"available", "unavailable", "fault"}:
    raise SystemExit("Grading readiness returned an invalid status")
if not isinstance(state.get("ready"), bool):
    raise SystemExit("Grading readiness did not return a boolean ready flag")
code = state.get("code")
if not isinstance(code, str) or not code:
    raise SystemExit("Grading readiness did not return a public code")
print(f"application_import=ok grading_readiness={state['status']}:{code}")
PY
        sudo -u www-data env \
            PYTHONDONTWRITEBYTECODE=1 \
            "MPLCONFIGDIR=$application_root/.cache/matplotlib" \
            "$VENV_GUNICORN" --check-config \
            -c "$application_root/deploy/gunicorn.conf.py" app:app
    )
}

ensure_runtime_dirs() {
    local root="$1"
    for path in \
        "$root/uploads" \
        "$root/output" \
        "$root/data" \
        "$root/logs" \
        "$root/backups" \
        "$root/学生档案" \
        "$root/批阅失败" \
        "$root/.cache/matplotlib"
    do
        install -d -o www-data -g www-data -m 0775 "$path"
    done
    # Logrotate may leave an existing file owned by root. Gunicorn runs as
    # www-data and must be able to reopen every current and rotated log after a
    # HUP, otherwise the service can die at the next rotation boundary.
    find "$root/logs" -maxdepth 1 -type f -name '*.log*' \
        -exec chown www-data:www-data {} + \
        -exec chmod u+rw,g+r {} +
}

bind_stage_environment() {
    local filename source target
    for filename in .env .env.local; do
        source="$REMOTE_DIR/$filename"
        [[ -f "$source" ]] || continue
        target="$STAGE_DIR/$filename"
        install -m 0640 -o root -g www-data /dev/null "$target"
        mount --bind "$source" "$target"
        STAGE_ENV_MOUNTS+=("$target")
    done
}

unbind_stage_environment() {
    local mount_path
    for mount_path in "${STAGE_ENV_MOUNTS[@]}"; do
        umount "$mount_path"
        rm -f -- "$mount_path"
    done
    STAGE_ENV_MOUNTS=()
}

[[ -d "$REMOTE_DIR" ]] || fail_remote "Missing deployment directory: $REMOTE_DIR"
[[ -x "$VENV_PYTHON" && -x "$VENV_GUNICORN" ]] || \
    fail_remote "Production venv is missing under $REMOTE_DIR/venv"
[[ -f "$REMOTE_DIR/requirements.txt" ]] || \
    fail_remote "Deployed requirements.txt is missing"
[[ -f "$REMOTE_DIR/.env" ]] || \
    fail_remote "Protected production environment file is missing"
[[ ! -e "$BACKUP_DIR" ]] || \
    fail_remote "Release backup directory already exists: $BACKUP_DIR"

actual_sha="$(sha256sum "$REMOTE_ARCHIVE" | awk '{print $1}')"
[[ "$actual_sha" == "$EXPECTED_SHA" ]] || fail_remote "Release archive checksum mismatch"

tar -xzf "$REMOTE_ARCHIVE" -C "$STAGE_DIR"
required_release_files=(
    app.py config.py database.py requirements.txt frontend/dist/index.html
    deploy/mathagent.service deploy/gunicorn.conf.py
    services/accelerator_business.py
    services/system_readiness.py
    services/retry_backoff.py services/template_canonicalization_contract.py
    services/answer_sheet_acceleration.py
    deploy/mathagent.service.d/20-resource-profile.conf
)
for path in "${required_release_files[@]}"; do
    [[ -f "$STAGE_DIR/$path" ]] || fail_remote "Release missing runtime file: $path"
done

# The release path never mutates the live venv. Dependency changes require a
# separately reviewed venv rollout before source deployment can proceed.
stage_requirements_sha="$(sha256sum "$STAGE_DIR/requirements.txt" | awk '{print $1}')"
deployed_requirements_sha="$(sha256sum "$REMOTE_DIR/requirements.txt" | awk '{print $1}')"
[[ "$stage_requirements_sha" == "$deployed_requirements_sha" ]] || \
    fail_remote "requirements.txt changed; prepare a reviewed production venv before deploying"

chown root:www-data "$REMOTE_DIR/.env"
chmod 0640 "$REMOTE_DIR/.env"
sudo -u www-data test -r "$REMOTE_DIR/.env" || \
    fail_remote "Production service user cannot read $REMOTE_DIR/.env"
# The archive is assembled from explicit tracked files, so tar creates their
# implicit parent directories with the root umask and root:root ownership.
# The candidate import runs as www-data and must be able to traverse the whole
# staged Python/frontend tree before the live service is stopped.
chown -R root:www-data "$STAGE_DIR"
chmod 0750 "$STAGE_DIR"
ensure_runtime_dirs "$STAGE_DIR"
bind_stage_environment

"$VENV_PYTHON" -m compileall -q "$STAGE_DIR"
find "$STAGE_DIR" -type d -name __pycache__ -prune -exec rm -rf -- {} +
systemd-analyze verify "$STAGE_DIR/deploy/mathagent.service"
check_redis_ping "$STAGE_DIR"
check_application_candidate "$STAGE_DIR"
unbind_stage_environment

systemctl is-active --quiet mathagent || \
    fail_remote "MathAgent must be active before an in-place production release"
SERVICE_WAS_ACTIVE=1
DEPLOY_STARTED=1
systemctl stop mathagent
systemctl is-active --quiet mathagent && fail_remote "MathAgent did not stop cleanly"

# Backups are taken only after all workers have stopped.
install -d -o root -g root -m 0700 "$REMOTE_DIR/backups/releases" "$BACKUP_DIR"
"$VENV_PYTHON" - "$REMOTE_DIR/.env" "$REMOTE_DIR" "$BACKUP_DIR" <<'PY'
import json
import os
import sqlite3
import sys
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

env_file = Path(sys.argv[1])
application_root = Path(sys.argv[2])
backup_dir = Path(sys.argv[3])
values = dotenv_values(env_file)
default_url = f"sqlite:///{application_root / 'mathagent.db'}"
url = make_url(str(values.get("DATABASE_URL") or default_url))
if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
    raise SystemExit("Only a persistent SQLite DATABASE_URL is supported by release rollback")
db_path = Path(url.database)
if not db_path.is_absolute():
    db_path = application_root / db_path
db_path = db_path.resolve(strict=False)
metadata = {"path": str(db_path), "existed": db_path.exists()}
if db_path.exists():
    stat = db_path.stat()
    metadata.update({"uid": stat.st_uid, "gid": stat.st_gid, "mode": stat.st_mode & 0o777})
    source = sqlite3.connect(db_path)
    destination_path = backup_dir / "database-before.db"
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.chmod(destination_path, 0o600)
(backup_dir / "database-before.json").write_text(
    json.dumps(metadata, ensure_ascii=True), encoding="utf-8"
)
os.chmod(backup_dir / "database-before.json", 0o600)
PY

if [[ -f /etc/systemd/system/mathagent.service ]]; then
    cp -a /etc/systemd/system/mathagent.service "$BACKUP_DIR/mathagent.service.before"
else
    touch "$BACKUP_DIR/mathagent.service.missing"
fi
if [[ -f /etc/systemd/system/mathagent.service.d/20-resource-profile.conf ]]; then
    cp -a /etc/systemd/system/mathagent.service.d/20-resource-profile.conf \
        "$BACKUP_DIR/20-resource-profile.conf.before"
else
    touch "$BACKUP_DIR/20-resource-profile.conf.missing"
fi
if [[ -f /etc/mathagent/gunicorn.conf.py ]]; then
    cp -a /etc/mathagent/gunicorn.conf.py "$BACKUP_DIR/gunicorn.conf.py.before"
else
    touch "$BACKUP_DIR/gunicorn.conf.py.missing"
fi

SOURCE_BACKUP_TMP="$BACKUP_DIR/source-before.tar.gz.incomplete"
tar \
    --exclude='.env' --exclude='.env.local' --exclude='config_local.py' \
    --exclude='venv' --exclude='uploads' --exclude='output' --exclude='data' \
    --exclude='backups' --exclude='logs' --exclude='.cache' \
    --exclude='.hermes_mathagent' --exclude='学生档案' --exclude='批阅失败' \
    --exclude='_dead_code_backup' --exclude='*.db' --exclude='*.db-wal' \
    --exclude='*.db-shm' --exclude='*.sqlite' --exclude='*.sqlite3' \
    -C "$REMOTE_DIR" -czf "$SOURCE_BACKUP_TMP" .
mv "$SOURCE_BACKUP_TMP" "$BACKUP_DIR/source-before.tar.gz"
ROLLBACK_READY=1

# Delayed updates keep candidate files hidden until the stopped-service switch.
# Environment files are excluded so protected production credentials survive.
rsync -a --delete-delay --delay-updates \
    "${persistent_excludes[@]}" "$STAGE_DIR/" "$REMOTE_DIR/"

# rsync -a may copy the root-only mode of the temporary staging directory to
# the destination. The service must be able to chdir here and SQLite must be
# able to create WAL sidecars, while ownership of the deployment root remains
# unchanged.
chgrp www-data "$REMOTE_DIR"
chmod 0775 "$REMOTE_DIR"
ensure_runtime_dirs "$REMOTE_DIR"
chown root:root "$REMOTE_DIR/backups/releases" "$BACKUP_DIR"
chmod 0700 "$REMOTE_DIR/backups/releases" "$BACKUP_DIR"

install -D -m 0644 "$REMOTE_DIR/deploy/mathagent.service" /etc/systemd/system/mathagent.service
install -D -m 0644 "$REMOTE_DIR/deploy/mathagent.service.d/20-resource-profile.conf" \
    /etc/systemd/system/mathagent.service.d/20-resource-profile.conf
install -D -m 0644 "$REMOTE_DIR/deploy/gunicorn.conf.py" /etc/mathagent/gunicorn.conf.py
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/mathagent.service

# The application loads the protected environment file itself. Atomic secret
# rotation may replace its inode, so reassert both ownership and mode before a
# www-data Gunicorn worker imports config.py.
chown root:www-data "$REMOTE_DIR/.env"
chmod 0640 "$REMOTE_DIR/.env"
sudo -u www-data test -r "$REMOTE_DIR/.env" || \
    fail_remote "Production service user cannot read $REMOTE_DIR/.env"

find "$REMOTE_DIR" -maxdepth 2 -type f \( \
    -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \
\) -exec chown www-data:www-data {} +

systemctl restart mathagent
health_ready=0
for _ in $(seq 1 15); do
    health="$(curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8080/api/health 2>/dev/null || true)"
    if printf '%s' "$health" | "$VENV_PYTHON" -c '
import json
import sys
state = json.load(sys.stdin)
raise SystemExit(0 if state.get("status") == "ok" and state.get("database") == "ok" else 1)
' >/dev/null 2>&1; then
        health_ready=1
        break
    fi
    sleep 1
done
[[ "$health_ready" == "1" ]] || \
    fail_remote "Loopback database health did not become ready"
systemctl is-active --quiet mathagent || \
    fail_remote "MathAgent systemd unit is not active after restart"
check_redis_ping "$REMOTE_DIR"
check_application_candidate "$REMOTE_DIR"
systemctl is-active --quiet mathagent || \
    fail_remote "MathAgent stopped during production readiness checks"

echo "release_id=$RELEASE_ID"
echo "backup_dir=$BACKUP_DIR"
echo "loopback_database_health=ok"
echo "systemd_active=ok"
exit 0
REMOTE
ok "Server release completed with database, Redis, import, and systemd checks"
info "release_archive_sha256=$ARCHIVE_SHA"
warn "Nginx remains on the maintenance vhost until the explicit public-entry check."
