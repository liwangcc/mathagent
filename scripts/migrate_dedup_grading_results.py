"""grading_results 去重迁移 —— 为唯一约束 uq_grading_student_image_category_exam 铺路。

背景:
    models.GradingResult 现声明了部分唯一索引
        (student_id, image_content_hash, category, exam_name) WHERE image_content_hash != ''
        AND deleted_at IS NULL
    作为批改落库幂等的真相源。但历史上 find_existing + insert 是 check-then-insert 的
    非原子路径，并发/多 worker 下可能已写入重复行。database.py 启动时虽尝试建该索引，
    但存量重复会让 CREATE UNIQUE INDEX 失败并被静默吞掉 —— 等于没保护。

    本脚本先清理存量重复，使唯一索引能成功建立。

用法:
    python scripts/migrate_dedup_grading_results.py            # dry-run 预览（默认，不写库）
    python scripts/migrate_dedup_grading_results.py --commit   # 实际去重 + 建唯一索引

去重口径:
    - 分组键: (student_id, image_content_hash, category, exam_name)，仅 image_content_hash
      非空的行参与（空 hash 是"内容算不出"的降级行，按设计允许并存，不去重）。
    - 每组保留 id 最小的一行（最早批改）为 keeper，其余为 losers。
    - losers 及其子表行（student_profiles / knowledge_events，按 grading_result_id 关联）
      一并删除，避免成长曲线/班级分析重复计数。keeper 自身的子表行保留；若 keeper 缺子表
      行，下次走 persist_grading_result 时会自动补齐（该函数本就有 repair 逻辑）。
"""

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from database import engine  # noqa: E402

DRY_RUN = "--commit" not in sys.argv

GROUP_SQL = text(
    "SELECT id, student_id, image_content_hash, category, exam_name "
    "FROM grading_results "
    "WHERE image_content_hash IS NOT NULL AND image_content_hash != '' "
    "AND deleted_at IS NULL "
    "ORDER BY student_id, image_content_hash, category, exam_name, id"
)

INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_grading_student_image_category_exam "
    "ON grading_results(student_id, image_content_hash, category, exam_name) "
    "WHERE image_content_hash IS NOT NULL AND image_content_hash != '' AND deleted_at IS NULL"
)


def _columns(conn) -> set[str]:
    return {r[1] for r in conn.execute(text("PRAGMA table_info(grading_results)"))}


def _ensure_schema(conn) -> None:
    """补齐去重所需列（与 database._migrate 的默认值保持一致）。

    旧库可能尚未跑过列迁移：image_content_hash / category 由 database._migrate 在
    应用启动时 ALTER 添加。本脚本可能在应用首次启动前运行，故自带最小补列逻辑，
    保证去重查询不依赖启动顺序。仅在 --commit 下写入。
    """
    cols = _columns(conn)
    if "image_content_hash" not in cols:
        conn.execute(text(
            "ALTER TABLE grading_results ADD COLUMN "
            "image_content_hash VARCHAR(64) DEFAULT '' NOT NULL"
        ))
        print("[schema] 已补列 grading_results.image_content_hash（默认空串）")
    if "category" not in cols:
        conn.execute(text(
            "ALTER TABLE grading_results ADD COLUMN "
            "category VARCHAR(20) DEFAULT 'daily' NOT NULL"
        ))
        print("[schema] 已补列 grading_results.category（默认 daily）")
    if "deleted_at" not in cols:
        conn.execute(text("ALTER TABLE grading_results ADD COLUMN deleted_at DATETIME"))
        print("[schema] 已补列 grading_results.deleted_at")


def _collect_duplicates(conn):
    """返回 (loser_ids, group_count) —— 需删除的重复行 id 与受影响分组数。"""
    groups = defaultdict(list)
    for row in conn.execute(GROUP_SQL):
        key = (row.student_id, row.image_content_hash, row.category, row.exam_name)
        groups[key].append(row.id)

    loser_ids = []
    dup_groups = 0
    for key, ids in groups.items():
        if len(ids) > 1:
            dup_groups += 1
            keeper = min(ids)
            losers = [i for i in ids if i != keeper]
            loser_ids.extend(losers)
    return loser_ids, dup_groups


def migrate():
    with engine.begin() as conn:
        # 旧库可能还没有 image_content_hash/deleted_at 列（由 database._migrate 在应用启动时添加）。
        cols = _columns(conn)
        if "image_content_hash" not in cols or "deleted_at" not in cols:
            if DRY_RUN:
                missing = "、".join(col for col in ("image_content_hash", "deleted_at") if col not in cols)
                print(f"[扫描] grading_results 尚无 {missing} 列。")
                print("       现存行将补列；隐藏行不参与活跃批改的唯一索引。")
                print("\n[DRY-RUN] 未写入。加 --commit 将补列并建唯一索引。")
                return
            _ensure_schema(conn)

        loser_ids, dup_groups = _collect_duplicates(conn)

        # 统计将被牵连删除的子表行
        if loser_ids:
            in_clause = ",".join(str(i) for i in loser_ids)
            prof_cnt = conn.execute(
                text(f"SELECT COUNT(*) FROM student_profiles WHERE grading_result_id IN ({in_clause})")
            ).scalar()
            ke_cnt = conn.execute(
                text(f"SELECT COUNT(*) FROM knowledge_events WHERE grading_result_id IN ({in_clause})")
            ).scalar()
        else:
            prof_cnt = ke_cnt = 0

        total = conn.execute(text("SELECT COUNT(*) FROM grading_results")).scalar()
        print(f"[扫描] grading_results 共 {total} 行")
        print(f"       重复分组: {dup_groups} 组，需删除重复行: {len(loser_ids)} 行")
        print(f"       牵连删除 student_profiles: {prof_cnt} 行，knowledge_events: {ke_cnt} 行")

        # 检查现有唯一索引状态
        idx_exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='index' "
                 "AND name='uq_grading_student_image_category_exam'")
        ).first()
        print(f"       唯一索引当前{'已存在' if idx_exists else '不存在'}")

        if DRY_RUN:
            if loser_ids:
                preview = loser_ids[:10]
                print(f"\n[DRY-RUN] 将删除的重复行 id（前10）: {preview}"
                      f"{' ...' if len(loser_ids) > 10 else ''}")
            print("\n[DRY-RUN] 未写入。确认无误后加 --commit 实际执行。")
            return

        if loser_ids:
            in_clause = ",".join(str(i) for i in loser_ids)
            # 先删子表（FK: knowledge_events / student_profiles → grading_results）
            conn.execute(text(f"DELETE FROM knowledge_events WHERE grading_result_id IN ({in_clause})"))
            conn.execute(text(f"DELETE FROM student_profiles WHERE grading_result_id IN ({in_clause})"))
            conn.execute(text(f"DELETE FROM grading_results WHERE id IN ({in_clause})"))
            print(f"\n[OK] 已删除 {len(loser_ids)} 行重复批改及其子表行")
        else:
            print("\n[OK] 无重复行，无需删除")

        # Rebuild in case an older process created the pre-soft-delete predicate.
        conn.execute(text("DROP INDEX IF EXISTS uq_grading_student_image_category_exam"))
        conn.execute(text(INDEX_SQL))
        print("[OK] 唯一索引 uq_grading_student_image_category_exam 已就绪")

        # 校验 FK 完整性（应无孤儿行）
        problems = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        if problems:
            print(f"[警告] FK 完整性检查发现问题: {problems}")
        else:
            print("[OK] FK 完整性检查通过")


if __name__ == "__main__":
    migrate()
