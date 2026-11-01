# 唯一生产 Gunicorn 配置。systemd 以绝对路径 /etc/mathagent/gunicorn.conf.py
# 加载此文件，避免发布目录中的同名配置覆盖或相对路径漂移。
#
# worker 数只能由环境变量显式设为 1 或 2，禁止按 CPU 核数自动放大。
#
# 跨 worker 的状态现在都已共享，2 个 worker 是安全的：
# - 短信验证码、登录失败计数、登出黑名单：SQLite（WAL）
# - 图形验证票据、微信 OAuth state：Redis（有 REDIS_URL 时）或 SQLite
# - 模型/用户配额：Redis
#
# 但这只解决「多个 worker」，**不是横向扩容**：uploads/ 是本地磁盘、批阅批次在
# HTTP 请求内执行、主库是单文件 SQLite，所以仍然只能一台机器一个实例。
import os

workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
if workers not in {1, 2}:
    raise RuntimeError("GUNICORN_WORKERS 生产仅允许 1 或经验证的灰度值 2")
worker_class = "uvicorn.workers.UvicornWorker"
threads = 1

bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8080")

# UvicornWorker periodically notifies Gunicorn while async requests are alive;
# this is a worker-heartbeat watchdog, not a per-request grading deadline.
# Keep it bounded so a blocked event loop is replaced within five minutes.
timeout = 480
graceful_timeout = 30
keepalive = 5

# 批改/OCR 使用可持续数分钟的 SSE。单 worker 按请求数回收会先停止接收
# 新请求，随后由 watchdog 杀掉仍在流式处理的批次，因此不能作为内存治理手段。
# 生产重启必须走有检查点的受控发布流程。
max_requests = 0
max_requests_jitter = 0

accesslog = "/opt/mathagent/logs/gunicorn_access.log"
errorlog = "/opt/mathagent/logs/gunicorn_error.log"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

proc_name = "mathagent"
