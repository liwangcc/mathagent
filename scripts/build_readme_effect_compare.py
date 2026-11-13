#!/usr/bin/env python3
"""重建 README 的 effect-compare.svg：从源图重裁 Q21/Q16，修正 Q16 被截断的问题。

Q16 旧裁剪把右侧「或 b≤0」切掉、上沿切字、下沿露出下一行。这里按墨迹实际范围重裁，
并把虚线高亮框按源图坐标重新映射。Q21 三条本来完整，几何与框注保持不变。

用法：env -u PYTHONPATH ./venv_local/Scripts/python.exe scripts/build_readme_effect_compare.py
"""
from __future__ import annotations
import base64, io
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "evidence/01_difference-vs-raw/case_a_human_truth/samples"
OUT = ROOT / "docs/readme-assets/effect-compare.svg"

# 每条裁剪框 (x0, y0, x1, y1)，按源图像素坐标。Q16 为修正后的完整范围。
CROPS = {
    "raw":    {"src": "S01_paper_raw.jpg",
               "q21": (1290, 1278, 2394, 1350), "q16": (1838, 946, 2355, 1030)},
    "diff":   {"src": "S01_diff_col1.png",
               "q21": (75, 1035, 995, 1095),   "q16": (545, 728, 995, 812)},
    "nodash": {"src": "S01_diff_nodash_col1.png",
               "q21": (75, 1035, 995, 1095),   "q16": (545, 728, 995, 812)},
}
# 虚线高亮带（源图坐标）。diff 残留虚线 / nodash 抹除位置，都在 y≈796..806。
DASH_BAND = {"diff": (560, 796, 980, 806), "nodash": (560, 796, 980, 806)}

Q21_W = 800.0   # Q21 显示宽（与旧版一致）
Q16_W = 560.0   # Q16 显示宽（保持放大观感）
LEFT = 40.0


def b64(img: Image.Image, fmt: str) -> str:
    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, "JPEG", quality=90)
        mime = "image/jpeg"
    else:
        img.save(buf, "PNG")
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def crop_img(key: str, which: str) -> Image.Image:
    c = CROPS[key]
    return Image.open(SAMPLES / c["src"]).crop(c[which])


def map_rect(src_box, disp_xy, disp_wh, band):
    """把源图坐标的 band 映射到显示坐标。"""
    x0, y0, x1, y1 = src_box
    dx, dy = disp_xy
    dw, dh = disp_wh
    bx0, by0, bx1, by1 = band
    fx = lambda v: dx + (v - x0) / (x1 - x0) * dw
    fy = lambda v: dy + (v - y0) / (y1 - y0) * dh
    return fx(bx0), fy(by0), fx(bx1) - fx(bx0), fy(by1) - fy(by0)


def main():
    parts = []
    # 每块：header、Q21、Q16、右侧统计、分隔线。用游标自上而下排版。
    blocks = [
        {"key": "raw", "title": "原图（含印刷题干）",
         "cap1": "Q16 局部放大", "cap2": ("s", "← 印刷虚线压在笔迹下方"),
         "stat": [("hl-o", "Q21：46/50 改写成 4/5"), ("s", "裸提示词「转录这份试卷。」"),
                  ("s", "如实转录 0 次"), ("acc", "转录准确率 97.2%"), ("s", "243/250")]},
        {"key": "diff", "title": "差分后（题干被减掉）",
         "cap1": "Q16 局部放大", "cap2": ("o", "← 残留印刷虚线横穿手写行"),
         "stat": [("hl-b", "Q21：0/50 改写，50/50 如实"), ("s", "同一句提示词"),
                  ("s", "残留虚线会把 Q16 的 ab 拆成 a,b"), ("acc", "转录准确率 98.0%"), ("s", "245/250")]},
        {"key": "nodash", "title": "差分 + 抹掉残留虚线",
         "cap1": "Q16 局部放大", "cap2": ("bl", "← 虚线段已抹成空白"),
         "stat": [("hl-b", "残留虚线也抹掉"), ("s", "原型实测；生产已上线同类处理"),
                  ("acc", "转录准确率 99.6%"), ("s", "249/250")]},
    ]

    cursor = 118.0  # 第一块 header 基线
    block_geom = []
    for b in blocks:
        key = b["key"]
        q21 = crop_img(key, "q21"); q16 = crop_img(key, "q16")
        q21_h = Q21_W * q21.height / q21.width
        q16_h = Q16_W * q16.height / q16.width
        header_y = cursor
        q21_y = cursor + 12
        q16_y = q21_y + q21_h + 10
        block_bottom = q16_y + q16_h
        block_geom.append({**b, "header_y": header_y, "q21_y": q21_y, "q21_h": q21_h,
                           "q16_y": q16_y, "q16_h": q16_h, "q21": q21, "q16": q16,
                           "bottom": block_bottom})
        cursor = block_bottom + 16.6 + 30  # 分隔线间距 + 下一块 header 偏移

    total_h = cursor + 40
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 {total_h:.0f}" '
                 f'width="1200" height="{total_h:.0f}" role="img" aria-labelledby="t d">')
    parts.append('<title id="t">效果对比：原图、差分后、差分加抹虚线</title>')
    parts.append('<desc id="d">同一张教师自印测试卷的同一位置。原图里有印刷题干，模型据此把学生写的 4√5 改写成 4/5；差分后题干被减掉，只剩笔迹；再抹掉残留印刷虚线。</desc>')
    parts.append("""<style>
text{font-family:'PingFang SC','Hiragino Sans GB','Microsoft YaHei','Noto Sans CJK SC','Source Han Sans SC',sans-serif;fill:#1f2328}
.t{font-size:22px;font-weight:700}
.h{font-size:16px;font-weight:700}
.b{font-size:14px}
.s{font-size:12.5px;fill:#57606a}
.o{font-size:12.5px;fill:#b43c0c}
.bl{font-size:12.5px;fill:#2457a6}
.hl-o{font-size:14px;font-weight:700;fill:#b43c0c}
.hl-b{font-size:14px;font-weight:700;fill:#2457a6}
.acc{font-size:15px;font-weight:700}
</style>""")
    parts.append(f'<rect x="0.5" y="0.5" width="1199" height="{total_h-1:.0f}" rx="14" fill="#ffffff" stroke="#d0d7de"/>')
    parts.append('<text x="40" y="48" class="t">效果对比：模型实际收到的是什么</text>')
    parts.append('<text x="40" y="76" class="s">同一张卷、同一位置截取，三行横向对齐。上条：Q21 整行（看题干）；下条：Q16 作答区局部放大（看虚线）。</text>')

    for i, g in enumerate(block_geom):
        key = g["key"]; hy = g["header_y"]; q21y = g["q21_y"]; q16y = g["q16_y"]
        q21h = g["q21_h"]; q16h = g["q16_h"]
        parts.append(f'<text x="{LEFT:.0f}" y="{hy:.0f}" class="h">{g["title"]}</text>')
        # Q21 条 + 边框
        parts.append(f'<image x="{LEFT:.0f}" y="{q21y:.1f}" width="{Q21_W:.0f}" height="{q21h:.1f}" preserveAspectRatio="none" href="{b64(g["q21"], "jpeg" if key=="raw" else "png")}"/>')
        parts.append(f'<rect x="{LEFT:.0f}" y="{q21y:.1f}" width="{Q21_W:.0f}" height="{q21h:.1f}" fill="none" stroke="#d0d7de"/>')
        # Q16 条 + 边框
        parts.append(f'<image x="{LEFT:.0f}" y="{q16y:.1f}" width="{Q16_W:.0f}" height="{q16h:.1f}" preserveAspectRatio="none" href="{b64(g["q16"], "jpeg" if key=="raw" else "png")}"/>')
        parts.append(f'<rect x="{LEFT:.0f}" y="{q16y:.1f}" width="{Q16_W:.0f}" height="{q16h:.1f}" fill="none" stroke="#d0d7de"/>')
        # Q16 说明（右侧，垂直居中于 Q16 条）
        cap_y = q16y + q16h/2 - 6
        parts.append(f'<text x="616" y="{cap_y:.1f}" class="s">{g["cap1"]}</text>')
        cls, txt = g["cap2"]
        parts.append(f'<text x="616" y="{cap_y+20:.1f}" class="{cls}">{txt}</text>')
        # Q21 框注（相对 Q21 图原点，与旧版一致）
        if key == "raw":
            parts.append(f'<rect x="{LEFT+74.8:.1f}" y="{q21y+14.8:.1f}" width="234.8" height="28.7" rx="4" fill="none" stroke="#c2410c" stroke-width="2"/>')
            parts.append(f'<rect x="{LEFT+407.0:.1f}" y="{q21y+1.7:.1f}" width="55.7" height="43.5" rx="4" fill="none" stroke="#2457a6" stroke-width="2"/>')
            parts.append(f'<text x="250" y="{hy:.0f}" class="o">橙框：模型能读到的题干（sinα=3/5 → 自己算出 4/5）</text>')
            parts.append(f'<text x="640" y="{hy:.0f}" class="bl">蓝框：学生写的 4√5</text>')
        else:
            parts.append(f'<rect x="{LEFT+74.8:.1f}" y="{q21y+10.4:.1f}" width="234.8" height="33.0" rx="4" fill="none" stroke="#8c959f" stroke-dasharray="5 4"/>')
            parts.append(f'<text x="232.2" y="{q21y+32.2:.1f}" class="s" text-anchor="middle">题干已被减掉</text>')
        # Q16 虚线高亮框（按源图坐标映射）
        if key in DASH_BAND:
            rx, ry, rw, rh = map_rect(CROPS[key]["q16"], (LEFT, q16y), (Q16_W, q16h), DASH_BAND[key])
            color = "#c2410c" if key == "diff" else "#2457a6"
            parts.append(f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" rx="3" fill="none" stroke="{color}" stroke-width="2"/>')
        # 右侧统计列（相对 header 的固定偏移）
        stat_offsets = [0, 22, 44, 102, 124] if len(g["stat"]) == 5 else [0, 22, 102, 124]
        for (cls, txt), off in zip(g["stat"], stat_offsets):
            parts.append(f'<text x="880" y="{hy+off:.0f}" class="{cls}">{txt}</text>')
        # 分隔线
        if i < len(block_geom) - 1:
            sep_y = g["bottom"] + 16.6
            parts.append(f'<line x1="40" y1="{sep_y:.0f}" x2="1160" y2="{sep_y:.0f}" stroke="#eaeef2"/>')

    foot_y = block_geom[-1]["bottom"] + 30
    parts.append(f'<text x="40" y="{foot_y:.0f}" class="s">改写次数出自 evidence/03（同图同题 Q21，qwen3.8-flash，每组 50 次）；转录准确率出自 evidence/01（25 题 × 10 次，生产提示词）。</text>')
    parts.append(f'<text x="40" y="{foot_y+22:.0f}" class="s">样图为教师自印测试卷、本人笔迹，不含真实学生数据；卷头与密封线身份区已遮盖。</text>')
    parts.append('</svg>')
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"written {OUT}  height={total_h:.0f}")
    for g in block_geom:
        print(f"  {g['key']:7s} Q21 {Q21_W:.0f}x{g['q21_h']:.1f} @y{g['q21_y']:.1f}  "
              f"Q16 {Q16_W:.0f}x{g['q16_h']:.1f} @y{g['q16_y']:.1f}")


if __name__ == "__main__":
    main()