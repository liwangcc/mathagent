"""把 usage.json 里的 token 计数按 prices.json 的单价换算成金额。

用法：
    python evidence/04_usage-cost/compute.py prices.json [--pages 2] [--columns 2] [--students 100] [--cache-hit 0]

prices.json 形如（元 / 百万 token）：
    {"qwen3.8-flash": {"input": 0.8, "output": 2.7, "cache_hit": 0.08},
     "qwen3.8-max":   {"input": 12.0, "output": 36.0, "cache_hit": 1.2}}

--cache-hit 是模板解题（Max）那一步的缓存命中比例，0 表示全部按未命中计价。
只做「usage × 单价」的乘法，不做任何估算或补全：缺单价就直接报错，不填默认值。
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MILLION = 1_000_000

STAGE_LABEL = {
    "template_hierarchy": "模板 · 层级识别",
    "template_coarse_locator": "模板 · 粗定位",
    "template_transcribe": "模板 · 转录",
    "template_max_solve": "模板 · 解题",
    "student_ocr": "学生卷 · 转录",
    "grading": "学生卷 · 判分",
}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def price_of(prices, model):
    entry = prices.get(model)
    if not entry:
        raise SystemExit(f"prices.json 缺少模型 {model!r} 的单价")
    for key in ("input", "output"):
        if not isinstance(entry.get(key), (int, float)):
            raise SystemExit(f"prices.json 里 {model!r} 缺少 {key} 单价")
    return entry


def cost_of(model, usage, prices, cache_hit=0.0):
    p = price_of(prices, model)
    hit = p.get("cache_hit", p["input"])
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    return (
        prompt * (1 - cache_hit) * p["input"] / MILLION
        + prompt * cache_hit * hit / MILLION
        + completion * p["output"] / MILLION
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prices")
    ap.add_argument("--pages", type=int, default=2, help="模板页数")
    ap.add_argument("--columns", type=int, default=2, help="学生卷栏数（每栏一次转录）")
    ap.add_argument("--students", type=int, default=100, help="学生份数")
    ap.add_argument("--cache-hit", type=float, default=0.0, help="模板解题的缓存命中比例 0–1")
    ap.add_argument("--credits", action="store_true",
                    help="按 Token Plan Credits 输出（1 Credit = ¥0.01 的按量表价，见 token-plan.md）")
    args = ap.parse_args()
    scale = 100 if args.credits else 1
    symbol = "" if args.credits else "¥"
    unit_name = "Credits" if args.credits else "元"

    prices = load_json(args.prices)
    rows = load_json(os.path.join(HERE, "usage.json"))["rows"]

    by_stage = {}
    for row in rows:
        # 有 form 标记的，只把生产形态算进均值（对照实验的整页原图不参与）
        if row.get("form") == "control":
            continue
        by_stage.setdefault(row["stage"], []).append(row)

    print("各阶段单次用量（来源：usage.json，供应商回报值）")
    print(f"{'阶段':<16}{'模型':<14}{'输入':>9}{'输出':>9}{'单次' + unit_name:>14}")
    unit = {}
    for stage, items in by_stage.items():
        model = items[0]["model"]
        avg_in = sum(r["prompt_tokens"] for r in items) / len(items)
        avg_out = sum(r["completion_tokens"] for r in items) / len(items)
        per_call = sum(cost_of(model, r, prices, args.cache_hit if stage == "template_max_solve" else 0) for r in items) / len(items) * scale
        unit[stage] = per_call
        print(f"{STAGE_LABEL.get(stage, stage):<16}{model:<14}{avg_in:>9.0f}{avg_out:>9.0f}{per_call:>14.2f}")

    template = args.pages * (
        unit.get("template_hierarchy", 0)
        + unit.get("template_coarse_locator", 0)
        + unit.get("template_transcribe", 0)
        + unit.get("template_max_solve", 0)
    )
    per_student = args.columns * unit.get("student_ocr", 0) + unit.get("grading", 0)
    students = args.students * per_student
    total = template + students

    print()
    print(f"模板 {args.pages} 页（一次性）      {symbol}{template:.2f}")
    print(f"学生 {args.students} 份 × {symbol}{per_student:.4f}   {symbol}{students:.2f}")
    print(f"合计                        {symbol}{total:.2f}")
    if total:
        print(f"其中模板占                    {template / total * 100:.1f}%")
        print(f"摊到每份学生                {symbol}{total / args.students:.4f}")


if __name__ == "__main__":
    main()