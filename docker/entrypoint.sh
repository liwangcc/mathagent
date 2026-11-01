#!/bin/sh
# MathAgent 容器启动脚本：首次启动建库、补齐密钥、修正卷权限，然后降权运行。
#
# 设计约束（一台服务器 = 一个学校 / 单实例部署）：
# - 所有可变状态都在 ./data 与 ./uploads 两个持久卷里；
# - JWT_SECRET 只在缺失时生成一次，写进持久卷，重启不重生成；
# - data/.model_key_master（模型密钥主密钥）与 data/.first-run-password 都由后端
#   首次生成，备份 data/ 时必须连它们一起备份，否则已加密的 Key 无法解密；
# - 脚本以 root 起步只为了修正 bind mount 的属主，随后 drop 到 mathagent 用户。
set -eu

DATA_DIR="${MATHAGENT_DATA_DIR:-/app/data}"

mkdir -p "$DATA_DIR" /app/uploads /app/output /app/logs

# 1) 可选覆盖配置：优先持久卷里的 $DATA_DIR/.env，其次挂载进来的 /app/.env。
#    格式必须是简单的 KEY=VALUE（逐行解析，不当作 shell 代码执行）。
for cfg in "$DATA_DIR/.env" /app/.env; do
    [ -f "$cfg" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        case "$line" in ''|'#'*) continue ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key=${line%%=*}
        value=${line#*=}
        case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
        value=${value#\"}
        value=${value%\"}
        export "$key=$value"
    done < "$cfg"
done

# 2) JWT_SECRET：缺失时生成一次并写入持久卷，绝不在每次重启时重新生成
#    （否则所有老师会被强制下线）。
if [ -z "${JWT_SECRET:-}" ]; then
    secret_file="$DATA_DIR/jwt_secret"
    if [ -s "$secret_file" ]; then
        JWT_SECRET="$(cat "$secret_file")"
    else
        JWT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
        ( umask 077; printf '%s' "$JWT_SECRET" > "$secret_file" )
        echo "[mathagent] 已生成 JWT_SECRET 并写入 $secret_file（之后重启沿用，不再重新生成）"
    fi
    export JWT_SECRET
fi

# 3) 管理员首启密码由后端在 init_db() 里生成（前端/脚本共用一个来源）：
#    - 打到 stdout（docker logs 可见），并写入 $DATA_DIR/.first-run-password（0600）；
#    - 库里已有密码时不会覆盖，忘记密码请运行 scripts/set_admin_pwd.py。
export ADMIN_PHONE="${ADMIN_PHONE:-13800000000}"

# 4) 首次启动自动建库（create_all 幂等），多 worker 起来之前先做一次，
#    避免两个 worker 同时建表。
python -c "from database import init_db; init_db()"

# 5) bind mount 的目录属主通常是 root，先修正再降权。
if [ "$(id -u)" = "0" ]; then
    chown -R mathagent:mathagent "$DATA_DIR" /app/uploads /app/output /app/logs 2>/dev/null || true
    exec setpriv --reuid=mathagent --regid=mathagent --init-groups \
        gunicorn -c /app/docker/gunicorn.conf.py app:app
fi

exec gunicorn -c /app/docker/gunicorn.conf.py app:app