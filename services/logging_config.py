"""日志配置模块

用法:
    from services.logging_config import setup_logging
    setup_logging()  # 在 app 启动时调用一次
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 日志目录
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 日志格式
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_processing_logging(log_dir=LOG_DIR):
    """Dedicated JSONL sink; no duplicate propagation into legacy error.log."""
    target = logging.getLogger("mathagent.processing")
    for handler in target.handlers[:]:
        handler.close()
        target.removeHandler(handler)
    target.setLevel(logging.INFO)
    target.propagate = False
    handler = RotatingFileHandler(
        Path(log_dir) / "processing.jsonl", maxBytes=5 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    target.addHandler(handler)


def setup_logging(level: str = None):
    """配置全局日志。    
    # 降低 python_multipart 的日志级别（避免 "Skipping data after last boundary" 噪音）
    logging.getLogger("multipart.multipart").setLevel(logging.ERROR)
    logging.getLogger("python_multipart").setLevel(logging.ERROR)

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)，默认从环境变量 LOG_LEVEL 读取
    """
    level = level or os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, level.upper(), logging.INFO)

    # 根 logger
    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler（避免重复）
    root.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # 1. 控制台输出
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 2. 文件输出（按大小轮转，保留 7 个备份，每个最大 10MB）
    file_handler = RotatingFileHandler(
        LOG_DIR / "mathagent.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 3. 错误日志单独一份（只记录 WARNING+）
    error_handler = RotatingFileHandler(
        LOG_DIR / "error.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)
    setup_processing_logging()

    # 降低第三方库日志级别
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.info("Logging initialized: level=%s, dir=%s", logging.getLevelName(level), LOG_DIR)
