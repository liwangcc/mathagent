#!/bin/bash
# ============================================
# MathAgent 一键部署脚本 (Ubuntu 22.04)
# 使用方法：SSH登录服务器后，粘贴执行
# ============================================

set -euo pipefail

DOMAIN="${MATHAGENT_DOMAIN:-${1:-}}"
if [[ -z "$DOMAIN" || ! "$DOMAIN" =~ ^([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$ ]]; then
    echo "用法: MATHAGENT_DOMAIN=math.example.com bash deploy/setup.sh" >&2
    exit 2
fi

echo "===== MathAgent 部署开始 ====="

# 1. 更新系统
echo "[1/8] 更新系统..."
sudo apt update -y
sudo apt upgrade -y

# 2. 安装 Python 3.11 和必要工具
echo "[2/8] 安装 Python 3.11 和 Redis..."
sudo apt install -y python3.11 python3.11-venv python3-pip nginx redis-server
# Redis is an in-host coordinator only.  Do not expose the model/LLM limiter
# to the network; production traffic reaches it exclusively through loopback.
sudo sed -i 's/^bind .*/bind 127.0.0.1 ::1/' /etc/redis/redis.conf
sudo systemctl enable --now redis-server

# 3. 创建项目目录
echo "[3/8] 创建项目目录..."
sudo mkdir -p /opt/mathagent
# 运行时需要的子目录：logs（gunicorn 日志，见 gunicorn.conf.py）、
# uploads（试卷照片）、backups（数据库备份）。缺 logs 会导致 gunicorn 启动失败。
sudo mkdir -p /opt/mathagent
sudo chown -R $USER:$USER /opt/mathagent
cd /opt/mathagent

# setup.sh is normally copied from a complete release, but do not continue with
# a half-uploaded tree and install a systemd unit pointing at missing code.
if [ ! -f requirements.txt ] || [ ! -f deploy/gunicorn.conf.py ] || [ ! -f deploy/mathagent.service ] \
    || [ ! -f deploy/mathagent.service.d/20-resource-profile.conf ]; then
    echo "缺少项目发布文件。请先完整上传 MathAgent 到 /opt/mathagent，再运行 setup.sh。" >&2
    exit 1
fi

# 4. 把项目文件传到这个目录（后面会教你怎么做）
# 这里先创建虚拟环境
echo "[4/8] 创建 Python 虚拟环境..."
python3.11 -m venv venv
source venv/bin/activate

# 5. 安装依赖
echo "[5/8] 安装 Python 依赖..."
pip install --upgrade pip
pip install -r requirements.txt

# 6. 创建 systemd 服务（开机自启+崩溃自动重启）
echo "[6/8] 安装唯一 Gunicorn 配置和系统服务..."
sudo install -d -m 0755 /etc/mathagent
sudo install -m 0644 deploy/gunicorn.conf.py /etc/mathagent/gunicorn.conf.py
sudo cp deploy/mathagent.service /etc/systemd/system/mathagent.service
# Measured provider lanes; without this drop-in Flash grading falls to 4 in flight.
sudo install -d -m 0755 /etc/systemd/system/mathagent.service.d
sudo install -m 0644 deploy/mathagent.service.d/20-resource-profile.conf \
    /etc/systemd/system/mathagent.service.d/20-resource-profile.conf

# 服务以 www-data 运行（见 mathagent.service），运行时写入的目录与数据库
# 必须归 www-data，否则 gunicorn 写日志/备份/上传会权限拒绝。
runtime_dirs=(
    /opt/mathagent/uploads
    /opt/mathagent/output
    /opt/mathagent/data
    /opt/mathagent/logs
    /opt/mathagent/backups
    /opt/mathagent/.cache/matplotlib
    /opt/mathagent/学生档案
    /opt/mathagent/批阅失败
)
sudo install -d -o www-data -g www-data -m 0775 "${runtime_dirs[@]}"
# 数据库文件（若已存在）也交给 www-data，WAL 的 -wal/-shm 同理
[ -f /opt/mathagent/mathagent.db ] && sudo chown www-data:www-data /opt/mathagent/mathagent.db* || true

sudo systemctl daemon-reload
sudo systemctl enable mathagent

# Require a real loopback coordinator before the service is ever started.
redis-cli -h 127.0.0.1 ping | grep -qx PONG

# 7. 配置 Nginx 反向代理（可选，让80端口转发到8080）
echo "[7/8] 配置 Nginx..."
nginx_rendered="$(mktemp)"
trap 'rm -f "$nginx_rendered"' EXIT
sed "s/__DOMAIN__/$DOMAIN/g" deploy/nginx.conf > "$nginx_rendered"
if grep -q '__DOMAIN__' "$nginx_rendered"; then
    echo "Nginx 域名模板替换失败" >&2
    exit 1
fi
sudo install -m 0644 "$nginx_rendered" /etc/nginx/sites-available/mathagent

sudo ln -sf /etc/nginx/sites-available/mathagent /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# The systemd unit and the parsed Gunicorn config are deployment gates, not
# optional documentation.  This catches a stale/missing configuration before
# an operator issues `systemctl start mathagent`.
echo "[8/8] 验证部署配置..."
sudo systemd-analyze verify /etc/systemd/system/mathagent.service
sudo -u www-data /opt/mathagent/venv/bin/gunicorn --check-config \
    -c /etc/mathagent/gunicorn.conf.py app:app

echo ""
echo "===== 部署完成！====="
echo "接下来："
echo "1. 把 MathAgent 项目文件传到 /opt/mathagent/"
echo "2. cd /opt/mathagent && source venv/bin/activate"
echo "3. pip install -r requirements.txt"
echo "4. 配置 .env 文件"
echo "5. sudo systemctl start mathagent"
echo ""
echo "访问方式：http://你的服务器公网IP"
