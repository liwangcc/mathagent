"""灰提示词探针：同一张原图（含印刷题干），换几种「不提醒」的短提示词，看 Q21 会不会被读向标准答案。

背景：Q21 学生写 `4√5`，标准答案 `4/5`。生产提示词（含「题干不能作为转录依据」）下原图
10 次里有 6 次读成 `4√5/5`；差分图 0/10。这里只换提示词，其余参数照生产。

用法（仓库根目录）：python evidence/03_prompt-context-leak/probe.py [次数]
输出：本目录 receipts_<时间戳>.json（不覆盖已提交的 receipts.json）+ 控制台汇总
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
import config  # noqa: E402,F401  (加载 .env)
from services.ocr_service import _call_vision_request  # noqa: E402

BASE = HERE / "samples"
RAW = str(BASE / "S01_paper_raw.jpg")
DIFF = [str(BASE / "S01_diff_col1.png"), str(BASE / "S01_diff_col2.png")]
OUT = HERE
OUT.mkdir(parents=True, exist_ok=True)

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
CONCURRENCY = 6
MODEL = "qwen3.8-flash"  # 与 services/ocr_service.py 里学生转录的模型钉死值一致

PROMPTS = {
    "P1_用户灰提示词": "转录这份试卷，不要看题干，不要计算，直接转录。",
    "P2_裸一句": "转录这份试卷。",
    "P3_普通JSON转录": (
        "把这份试卷上学生的作答内容转录成 JSON，题号用 question_number，"
        "答案用 student_answer。只输出 JSON。"
    ),
}


def classify(answer: str) -> str:
    s = str(answer or "").replace(" ", "")
    if not s:
        return "empty"
    if re.search(r"4√5/5|4/5|\\frac\{4\}\{5\}", s):
        return "drift_to_standard"
    if "4√5" in s or "4\\sqrt{5}" in s:
        return "faithful"
    return "other"


def extract_q21(text: str) -> str:
    """尽量从各种输出形态里捞出第 21 题的作答（JSON 对象 / JSON 数组 / 纯文本）。"""
    blob = text.strip()
    for match in re.finditer(r"\{.*\}", blob, re.S):
        try:
            data = json.loads(match.group(0))
        except Exception:
            continue
        items = data.get("questions") if isinstance(data, dict) else data
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("question_number", "")).strip() == "21":
                    return str(item.get("student_answer", "")).strip()
    for match in re.finditer(r"\[.*\]", blob, re.S):
        try:
            items = json.loads(match.group(0))
        except Exception:
            continue
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("question_number", "")).strip() == "21":
                    return str(item.get("student_answer", "")).strip()
    m = re.search(r"(?:\*\*)?Q?\s*21(?:\*\*)?\s*[:：.、\)]\s*(.{0,200})", blob, re.S)
    if m:
        return m.group(1).split("\n\n")[0].strip()[:200]
    return ""


async def one(sem, name, prompt, images, idx):
    async with sem:
        started = time.time()
        try:
            text = await asyncio.wait_for(
                _call_vision_request(images, prompt, 9000, prompt_first=True),
                timeout=240,
            )
            answer = extract_q21(text)
            return {
                "prompt": name, "run": idx, "ok": True,
                "model": MODEL, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "raw_answer": answer, "verdict": classify(answer),
                "seconds": round(time.time() - started, 1),
                "text_head": text[:400],
                "text": text,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "prompt": name, "run": idx, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "seconds": round(time.time() - started, 1),
            }


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)
    jobs = []
    for name, prompt in PROMPTS.items():
        for idx in range(1, RUNS + 1):
            jobs.append(one(sem, name, prompt, [RAW], idx))
    for idx in range(1, RUNS + 1):
        jobs.append(one(sem, "P2_裸一句_差分对照", PROMPTS["P2_裸一句"], DIFF, idx))

    results = await asyncio.gather(*jobs)
    out_path = OUT / f"receipts_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"回执写入 {out_path.name}（已提交的 receipts.json 不动）")

    summary = {}
    for row in results:
        summary.setdefault(row["prompt"], {}).setdefault(
            row.get("verdict", "error"), []
        ).append(row.get("raw_answer", row.get("error", "")))
    for name, buckets in summary.items():
        total = sum(len(v) for v in buckets.values())
        parts = ", ".join(f"{k}={len(v)}" for k, v in sorted(buckets.items()))
        print(f"{name}: 共 {total} 次 → {parts}")
        for k, vals in sorted(buckets.items()):
            sample = [v for v in vals[:3]]
            print(f"    {k}: {sample}")


if __name__ == "__main__":
    asyncio.run(main())