"""容器内专用 Gunicorn 配置。

与生产 systemd 版本（deploy/gunicorn.conf.py）的区别只有绑定地址和日志目标：
容器里必须监听 0.0.0.0（由 compose 做端口映射），日志直接写 stdout/stderr
交给 docker 收集，避免日志落在容器可写层里随容器一起消失。

worker 数默认 2：验证码票据、微信 state、登出黑名单、限流计数都已落共享存储
（Redis / SQLite），多个 worker 之间不会出现「A 签发 B 校验失败」。

这只是「同进程组多 worker」，不是横向扩容：uploads/ 是本地磁盘、批阅批次在
HTTP 请求内执行、主库是单文件 SQLite。一台服务器只服务一个学校。
"""

import os

workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
if workers not in {1, 2}:
    raise RuntimeError("GUNICORN_WORKERS 仅允许 1 或 2（单实例部署，不做横向扩容）")

worker_class = "uvicorn.workers.UvicornWorker"
threads = 1

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# 批改/OCR 走可持续数分钟的 SSE，watchdog 只负责替换卡死的 worker。
timeout = 480
graceful_timeout = 30
keepalive = 5

# 禁止按请求数回收：会先停止接收新请求再杀掉仍在流式处理的批次。
max_requests = 0
max_requests_jitter = 0

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

proc_name = "mathagent"