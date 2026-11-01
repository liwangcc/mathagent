#!/usr/bin/env python3
"""SQLite 数据库备份脚本

用法:
    python scripts/backup_db.py              # 立即备份一次
    python scripts/backup_db.py --schedule   # 启动定时备份（每6小时）

备份文件保存在 backups/ 目录，格式: mathagent_YYYYMMDD_HHMMSS.db
自动清理超过 7 天的旧备份。
"""

import os
import sys
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

# 配置
DB_PATH = Path(__file__).parent.parent / "mathagent.db"
BACKUP_DIR = Path(__file__).parent.parent / "backups"
KEEP_DAYS = 7  # 保留最近 7 天的备份
BACKUP_INTERVAL = 6 * 3600  # 6 小时


def backup_database() -> Path | None:
    """执行一次备份，返回备份文件路径"""
    if not DB_PATH.exists():
        print(f"[ERROR] 数据库文件不存在: {DB_PATH}")
        return None

    # 创建备份目录
    BACKUP_DIR.mkdir(exist_ok=True)

    # 生成备份文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"mathagent_{timestamp}.db"

    try:
        # 使用 SQLite 的 backup API（安全，即使数据库正在使用）
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(backup_file)
        source.backup(dest)
        source.close()
        dest.close()

        size_mb = backup_file.stat().st_size / (1024 * 1024)
        print(f"[OK] 备份完成: {backup_file.name} ({size_mb:.2f} MB)")
        return backup_file

    except Exception as e:
        print(f"[ERROR] 备份失败: {e}")
        return None


def cleanup_old_backups():
    """删除超过 KEEP_DAYS 天的旧备份"""
    if not BACKUP_DIR.exists():
        return

    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    removed = 0

    for f in BACKUP_DIR.glob("mathagent_*.db"):
        if f.stat().st_mtime < cutoff.timestamp():
            f.unlink()
            removed += 1
            print(f"[DEL] 删除旧备份: {f.name}")

    if removed:
        print(f"共清理 {removed} 个旧备份")


def run_scheduled():
    """定时备份循环"""
    print(f"[SCHEDULE] 启动定时备份，间隔 {BACKUP_INTERVAL // 3600} 小时")
    print(f"[DIR] 备份目录: {BACKUP_DIR}")
    print(f"[KEEP] 保留天数: {KEEP_DAYS} 天")
    print("-" * 40)

    while True:
        backup_database()
        cleanup_old_backups()
        print(f"下次备份: {datetime.now() + timedelta(seconds=BACKUP_INTERVAL)}")
        print("-" * 40)
        time.sleep(BACKUP_INTERVAL)


def main():
    if "--schedule" in sys.argv:
        run_scheduled()
    else:
        backup_database()
        cleanup_old_backups()


if __name__ == "__main__":
    main()
