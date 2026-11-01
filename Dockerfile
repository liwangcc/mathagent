# syntax=docker/dockerfile:1

# ---- 阶段 1：构建前端 ----
FROM node:22-alpine AS frontend

WORKDIR /build/frontend

# 先只拷贝依赖清单，让 npm 层在源码变动时仍能命中缓存
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# ---- 阶段 2：运行后端 ----
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

# curl 供容器的 HEALTHCHECK 使用；libglib2.0-0 / libgomp1 是 numpy/scipy/opencv
# 运行期依赖；build-essential 只在安装依赖时需要，装完即卸掉，避免留在运行镜像里。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libglib2.0-0 libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

COPY . .
COPY --from=frontend /build/frontend/dist ./frontend/dist

# 非 root 运行：数据、上传、导出、日志和两个中文归档目录都必须提前建好并授权，
# 否则 config.py 在导入期创建目录会直接 PermissionError。
RUN groupadd --system mathagent \
    && useradd --system --gid mathagent --home-dir /app --shell /usr/sbin/nologin mathagent \
    && mkdir -p /app/data /app/uploads /app/output /app/logs /app/学生档案 /app/批阅失败 \
    && chown -R mathagent:mathagent \
        /app/data /app/uploads /app/output /app/logs /app/学生档案 /app/批阅失败 \
    && chmod +x /app/docker/entrypoint.sh

ENV HOST=0.0.0.0 \
    PORT=8080 \
    MATHAGENT_DATA_DIR=/app/data \
    DATABASE_URL=sqlite:////app/data/mathagent.db \
    GUNICORN_WORKERS=2

# 注意：镜像默认以 root 启动，entrypoint 只做「修正 bind mount 属主 + 建库」，
# 随后立即 setpriv 降权到 mathagent 运行 gunicorn（见 docker/entrypoint.sh）。
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]