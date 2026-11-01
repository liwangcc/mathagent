"""联调用 demo 种子数据 — 在真实库造一个完整班级，供前端联调有真实画面。

用法:
    python scripts/seed_demo.py            # 预览将创建什么
    python scripts/seed_demo.py --commit   # 实际写入
    python scripts/seed_demo.py --clean    # 删除本脚本造的 demo 数据

造什么（覆盖各种成长状态，让前端每个分支都有数据可渲染）:
    - 1 个老师（若不存在则建）下的 1 个 demo 班级
    - 6 名学生，分别处于：已纠正/未纠正/混合/从未错/新生无数据 等状态
    - 每生若干 knowledge_events（含四种错因），2~3 次月考成绩

幂等：靠班级名 DEMO_CLASS_NAME 识别，重复跑先清后建。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import SessionLocal, engine, Base  # noqa: E402
import models_user  # noqa: E402,F401
import models_rate_limit  # noqa: E402,F401
from models import Student, KnowledgeEvent, ExamScore, WeaknessOverride  # noqa: E402
from models_user import User  # noqa: E402
from models_rate_limit import TeacherClass  # noqa: E402

DEMO_CLASS_NAME = "【联调DEMO】24级数控1班"
DEMO_TEACHER_NAME = "联调演示老师"

DRY_RUN = "--commit" not in sys.argv and "--clean" not in sys.argv
CLEAN = "--clean" in sys.argv


# 学生画像：name, no, 事件序列[(知识点章, 知识点, [(result,error_type)...])], 月考[(名,分)]
# 错因语义分类已下线，事件中的 error_type 保持为空；技术诊断不由演示数据伪造。
DEMO_STUDENTS = [
    # 张三：二次函数已攻克(错→对→对)，三角仍弱；进步型
    ("张三", "001",
     [("代数与函数", "二次函数", [("w", ""), ("c", ""), ("c", "")]),
      ("三角函数", "正弦", [("w", ""), ("p", "")])],
     [("月考一", 72), ("月考二", 80), ("期中考", 88)]),
    # 李四：多个未纠正薄弱点，下滑型
    ("李四", "002",
     [("代数与函数", "对数函数", [("w", ""), ("w", "")]),
      ("三角函数", "诱导公式", [("w", ""), ("c", ""), ("w", "")])],
     [("月考一", 65), ("月考二", 58), ("期中考", 55)]),
    # 王五：全部已纠正，优等生
    ("王五", "003",
     [("代数与函数", "二次函数", [("w", ""), ("c", ""), ("c", "")]),
      ("代数与函数", "对数函数", [("p", ""), ("c", ""), ("c", "")])],
     [("月考一", 90), ("月考二", 92), ("期中考", 95)]),
    # 赵六：从未做错，稳定型（不计入薄弱）
    ("赵六", "004",
     [("代数与函数", "集合", [("c", ""), ("c", "")]),
      ("三角函数", "正弦", [("c", "")])],
     [("月考一", 85), ("月考二", 86)]),
    # 孙七：四种错因各一，知识点分散
    ("孙七", "005",
     [("代数与函数", "指数函数", [("w", "")]),
      ("代数与函数", "二次函数", [("w", "")]),
      ("三角函数", "正弦", [("w", "")]),
      ("三角函数", "诱导公式", [("w", "")])],
     [("月考一", 70)]),
    # 周八：新生，无任何数据（验证空状态）
    ("周八", "006", [], []),
]

_RES_MAP = {"c": "correct", "w": "wrong", "p": "partial"}


def _clean(db):
    """删除本脚本造的 demo 班级及其学生的全部关联数据。"""
    teacher = db.query(User).filter(User.name == DEMO_TEACHER_NAME).first()
    if not teacher:
        print("未找到 demo 老师，无需清理。")
        return
    cls = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == teacher.id,
        TeacherClass.class_name == DEMO_CLASS_NAME,
    ).first()
    if not cls:
        print("未找到 demo 班级，无需清理。")
        return
    sids = [s.id for s in db.query(Student).filter(Student.class_id == cls.id).all()]
    if sids:
        db.query(KnowledgeEvent).filter(KnowledgeEvent.student_id.in_(sids)).delete(synchronize_session=False)
        db.query(ExamScore).filter(ExamScore.student_id.in_(sids)).delete(synchronize_session=False)
        db.query(WeaknessOverride).filter(WeaknessOverride.student_id.in_(sids)).delete(synchronize_session=False)
        db.query(Student).filter(Student.id.in_(sids)).delete(synchronize_session=False)
    db.delete(cls)
    db.commit()
    print(f"已清理 demo 班级 + {len(sids)} 名学生及其数据。")


def seed(db):
    # 幂等：先清后建
    _clean(db)

    teacher = db.query(User).filter(User.name == DEMO_TEACHER_NAME).first()
    if not teacher:
        teacher = User(name=DEMO_TEACHER_NAME, role="teacher")
        db.add(teacher)
        db.flush()
    cls = TeacherClass(teacher_id=teacher.id, class_name=DEMO_CLASS_NAME)
    db.add(cls)
    db.flush()

    base = datetime.now() - timedelta(days=60)
    n_students = n_events = n_exams = 0

    for name, no, topics, exams in DEMO_STUDENTS:
        s = Student(teacher_id=teacher.id, class_id=cls.id, name=name, student_no=no)
        db.add(s)
        db.flush()
        n_students += 1
        day = 0
        for chapter, topic, seq in topics:
            for res_code, err in seq:
                ev = KnowledgeEvent(
                    student_id=s.id, class_id=cls.id, teacher_id=teacher.id,
                    grading_result_id=None,  # demo 数据不关联真实批改（FK 约束）
                    chapter=chapter, topic=topic,
                    result=_RES_MAP[res_code],
                    error_type=(err if res_code in ("w", "p") else ""),
                    difficulty=3,
                )
                ev.created_at = base + timedelta(days=day)
                db.add(ev)
                n_events += 1
                day += 3
        for i, (ename, score) in enumerate(exams):
            es = ExamScore(student_id=s.id, class_id=cls.id, teacher_id=teacher.id,
                           exam_name=ename, score=score)
            es.exam_date = (base + timedelta(days=15 * (i + 1))).date()
            db.add(es)
            n_exams += 1

    db.commit()
    print(f"[OK] demo 班级『{DEMO_CLASS_NAME}』(class_id={cls.id}, teacher_id={teacher.id})")
    print(f"     学生 {n_students} · 知识点事件 {n_events} · 月考成绩 {n_exams}")
    print(f"     前端联调登录老师：{DEMO_TEACHER_NAME}")


def main():
    db = SessionLocal()
    try:
        Base.metadata.create_all(bind=engine)
        if CLEAN:
            _clean(db)
            return
        if DRY_RUN:
            print("[DRY-RUN] 将创建：")
            print(f"  班级：{DEMO_CLASS_NAME}")
            print(f"  学生 {len(DEMO_STUDENTS)} 名：" + "、".join(s[0] for s in DEMO_STUDENTS))
            print("  覆盖状态：进步型/下滑型/优等生/稳定型/多错因/空数据新生")
            print("\n加 --commit 实际写入，--clean 清除。")
            return
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
