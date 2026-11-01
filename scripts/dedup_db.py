"""
QuestionBank 模糊去重清理脚本

用法:
    python scripts/dedup_db.py

策略:
    1. 按题库归属范围读取 question_bank 表记录（不跨老师/公私库合并）
    2. 用 services.answer_import._fuzzy_key 归一化题干
    3. 两两比较 SequenceMatcher 相似度，>= 0.98 视为同一组
    4. 每组保留 id 最大者，先将历史事件重指向保留题，再删除其余题
    5. dry-run 输出 → 用户确认 → 真正删除
"""

import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import SessionLocal  # noqa: E402
from models import KnowledgeEvent, QuestionBank  # noqa: E402
from services.answer_import import FUZZY_DEDUP_THRESHOLD, _fuzzy_key  # noqa: E402


def _preview(text: str, n: int = 60) -> str:
    s = (text or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def find_duplicate_groups(rows: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Greedy union: scan in id order, attach each row to the first group whose
    representative key is >= FUZZY_DEDUP_THRESHOLD similar."""
    keyed = [(qid, text, _fuzzy_key(text)) for qid, text in rows]
    keyed.sort(key=lambda x: x[0])

    groups: list[list[tuple[int, str, str]]] = []
    for qid, text, key in keyed:
        if not key:
            continue
        placed = False
        for g in groups:
            rep_key = g[0][2]
            if abs(len(key) - len(rep_key)) / max(len(key), len(rep_key)) > (1 - FUZZY_DEDUP_THRESHOLD):
                continue
            if SequenceMatcher(None, key, rep_key).ratio() >= FUZZY_DEDUP_THRESHOLD:
                g.append((qid, text, key))
                placed = True
                break
        if not placed:
            groups.append([(qid, text, key)])

    return [[(qid, text) for qid, text, _ in g] for g in groups if len(g) >= 2]


def main() -> int:
    db = SessionLocal()
    try:
        scoped_rows = db.query(
            QuestionBank.id,
            QuestionBank.question,
            QuestionBank.teacher_id,
            QuestionBank.is_public,
        ).all()
        rows_by_scope = defaultdict(list)
        for question_id, question, teacher_id, is_public in scoped_rows:
            rows_by_scope[(teacher_id, bool(is_public))].append((question_id, question))

        print(f"已加载 {len(scoped_rows)} 条题目，开始查找模糊重复（阈值 {FUZZY_DEDUP_THRESHOLD}）...")
        groups = []
        for rows in rows_by_scope.values():
            groups.extend(find_duplicate_groups(rows))
        if not groups:
            print("未发现重复题目。")
            return 0

        delete_ids: list[int] = []
        replacement_by_id: dict[int, int] = {}
        print()
        print("=" * 70)
        print(f"发现 {len(groups)} 组重复:")
        print("=" * 70)
        for i, group in enumerate(groups, 1):
            group_sorted = sorted(group, key=lambda x: x[0])
            keep_id = max(qid for qid, _ in group_sorted)
            to_delete = [(qid, text) for qid, text in group_sorted if qid != keep_id]

            print(f"\n[组 {i}] 共 {len(group_sorted)} 条 → 保留 id={keep_id}，删除 {len(to_delete)} 条")
            for qid, text in group_sorted:
                tag = "保留" if qid == keep_id else "删除"
                print(f"  [{tag}] id={qid}: {_preview(text)}")
            delete_ids.extend(qid for qid, _ in to_delete)
            replacement_by_id.update({qid: keep_id for qid, _ in to_delete})

        print()
        print("=" * 70)
        print(f"DRY-RUN 汇总: 共 {len(groups)} 组重复，将删除 {len(delete_ids)} 条")
        print("=" * 70)

        try:
            answer = input("\n确认删除以上记录吗？输入 yes 继续，其它任意键取消: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("已取消，未做任何修改。")
            return 0

        remapped_events = 0
        for deleted_question_id, retained_question_id in replacement_by_id.items():
            remapped_events += (
                db.query(KnowledgeEvent)
                .filter(KnowledgeEvent.question_bank_id == deleted_question_id)
                .update(
                    {KnowledgeEvent.question_bank_id: retained_question_id},
                    synchronize_session=False,
                )
            )
        deleted = db.query(QuestionBank).filter(QuestionBank.id.in_(delete_ids)).delete(synchronize_session=False)
        db.commit()
        print(f"\n完成: 实际删除 {deleted} 条记录，重关联 {remapped_events} 条历史知识事件。")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
