"""
knowledge_events 表迁移脚本（班级管理模块 — 坑1 数据脊椎）

用法:
    python scripts/migrate_knowledge_events.py            # dry-run 预览
    python scripts/migrate_knowledge_events.py --commit   # 实际回填

做两件事:
    1. 建 knowledge_events 表（Base.metadata.create_all，幂等）
    2. 回填存量 grading_results 的逐题明细为事件行

回填口径（历史数据是 best-effort，诚实标注其局限）:
    - student_id / class_id / teacher_id ← 该 grading_result 关联的 student
    - result ← 明细项的 result（correct/partial/wrong/needs_review）
    - chapter / topic / difficulty ← 按题干模糊匹配 question_bank（匹配不到则留空）
    - error_type ← 不回填已下线的学生错因分类，统一留空；技术复核诊断只由批阅安全分支产生
    - created_at ← 优先取关联 StudentProfile.created_at；缺失则用当前时间
    - 幂等: 若某 grading_result_id 已存在事件行，则跳过该批改
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import SessionLocal, engine, Base  # noqa: E402
# 注册全部模型到 metadata，使 create_all 能解析跨表外键（users / teacher_classes 等）
import models_user  # noqa: E402,F401
import models_rate_limit  # noqa: E402,F401
from models import (  # noqa: E402
    GradingResult, StudentProfile, Student, QuestionBank, KnowledgeEvent,
)
from services.answer_import import _fuzzy_key  # noqa: E402

DRY_RUN = "--commit" not in sys.argv


def _build_qbank_index(db):
    """题干模糊键 → (chapter, topic, difficulty)，用于历史明细的知识点回填。"""
    index = {}
    for q in db.query(QuestionBank.question, QuestionBank.chapter,
                      QuestionBank.topic, QuestionBank.difficulty).all():
        key = _fuzzy_key(q.question)
        if key and key not in index:
            index[key] = (q.chapter or "", q.topic or "", q.difficulty or 3)
    return index


def _lookup(qindex, question_text):
    key = _fuzzy_key(question_text or "")
    return qindex.get(key, ("", "", 3))


def migrate():
    db = SessionLocal()
    try:
        # 1) 建表（幂等）
        Base.metadata.create_all(bind=engine)
        print("[1/2] knowledge_events 表已就绪")

        qindex = _build_qbank_index(db)
        print(f"      题库索引: {len(qindex)} 条")

        # student_id → (class_id, teacher_id) 缓存
        students = {s.id: (s.class_id, s.teacher_id) for s in db.query(Student).all()}

        # grading_result_id → StudentProfile.created_at（取最早一条）
        prof_time = {}
        for p in db.query(StudentProfile.grading_result_id, StudentProfile.created_at).all():
            if p.grading_result_id and p.grading_result_id not in prof_time:
                prof_time[p.grading_result_id] = p.created_at

        # 已有事件的 grading_result_id（幂等跳过）
        existing = {gid for (gid,) in db.query(KnowledgeEvent.grading_result_id)
                    .filter(KnowledgeEvent.grading_result_id.isnot(None)).distinct().all()}

        total_events = 0
        skipped_gr = 0
        new_rows = []
        for gr in db.query(GradingResult).all():
            if gr.id in existing:
                skipped_gr += 1
                continue
            sid = gr.student_id
            if sid not in students:
                continue  # 学生已删，跳过
            class_id, teacher_id = students[sid]
            if teacher_id is None:
                continue  # 无归属，跳过（越权隔离前提）
            ts = prof_time.get(gr.id)
            for d in gr.get_details():
                result = d.get("result", "")
                if not result:
                    continue
                chapter, topic, diff = _lookup(qindex, d.get("question", ""))
                err = ""
                row = KnowledgeEvent(
                    student_id=sid, class_id=class_id, teacher_id=teacher_id,
                    grading_result_id=gr.id, question_bank_id=None,
                    chapter=chapter, topic=topic or "未知", result=result,
                    error_type=err, difficulty=diff,
                )
                if ts is not None:
                    row.created_at = ts
                new_rows.append(row)
                total_events += 1

        print(f"[2/2] 待回填事件: {total_events} 条（来自 "
              f"{db.query(GradingResult).count() - skipped_gr} 个批改，跳过已存在 {skipped_gr} 个）")

        if DRY_RUN:
            print("\n[DRY-RUN] 未写入。加 --commit 实际执行。")
            # 抽样预览前 5 条
            for r in new_rows[:5]:
                print(f"  - student={r.student_id} {r.chapter}/{r.topic} "
                      f"{r.result}/{r.error_type or '—'} @ {r.created_at}")
            return

        db.bulk_save_objects(new_rows)
        db.commit()
        print(f"\n[OK] 已回填 {total_events} 条事件。")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
