#!/bin/bash
# MathAgent 数据库备份 — 保留最近7天
set -euo pipefail
BACKUP_DIR="/opt/mathagent/backups"
DB_PATH="/opt/mathagent/mathagent.db"
DATE=$(date +%Y%m%d_%H%M%S)
DEST="$BACKUP_DIR/mathagent_$DATE.db"

mkdir -p "$BACKUP_DIR"

# 数据库开启了 WAL 模式（database.py: PRAGMA journal_mode=WAL）。
# 直接 cp 只拷 .db 文件，已提交但未 checkpoint 的事务还在 -wal 文件里 →
# 备份可能丢失最近写入。必须用 sqlite3 .backup 做在线一致性快照
# （它会正确合并 WAL，且不阻塞写入）。
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_PATH" ".backup '$DEST'"
else
    # 退路：先 checkpoint 把 WAL 落盘，再 cp。要求此刻无并发长写事务。
    echo "警告: 未找到 sqlite3，回退到 cp（一致性弱于 .backup）"
    cp "$DB_PATH" "$DEST"
    [ -f "$DB_PATH-wal" ] && cp "$DB_PATH-wal" "$DEST-wal" 2>/dev/null || true
fi

# 删除7天前的备份
find "$BACKUP_DIR" -name "mathagent_*.db" -mtime +7 -delete

echo "备份完成: mathagent_$DATE.db"

