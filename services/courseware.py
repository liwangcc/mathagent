"""课件库存储与代表性示例课件。"""

from __future__ import annotations

import html
import json
import os
import re
import uuid
from typing import Any
from services.courseware_3d import build_solid_html, build_space_relations_html

COURSEWARE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "courseware")
STAGE_DIRECTORY_NAMES = {"primary": "小学", "junior": "初中", "senior": "高中"}
COURSEWARE_V4_CATALOG = os.path.join(COURSEWARE_DIR, "catalog_v4.json")


def courseware_cover_metadata(*, chapter: str = "", knowledge_point: str = "", stored_path: str = "", code: str = "") -> dict[str, Any]:
    """Return short, content-derived labels used on catalogue covers.

    v4 HTML chapters expose a JSON ``CHAPTER`` declaration.  Reading only that
    declaration keeps the cover tied to the lesson's actual knowledge points
    without loading or duplicating the interactive HTML in the API response.
    """
    chapter_text = str(chapter or "").strip()
    point_text = str(knowledge_point or "").strip()
    metadata: dict[str, Any] = {}
    if stored_path and os.path.isfile(stored_path):
        try:
            with open(stored_path, "r", encoding="utf-8") as handle:
                html_text = handle.read()
            match = re.search(r"const\s+CHAPTER\s*=\s*(\{.*?\});\s*(?:const|let|var)", html_text, re.S)
            if match:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict):
                    metadata = parsed
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            metadata = {}
    knowledge = metadata.get("knowledge") if isinstance(metadata.get("knowledge"), list) else []
    names = [str(item.get("name") or "").strip() for item in knowledge if isinstance(item, dict)]
    names = [name for name in names if name]
    scenes = metadata.get("scenes") if isinstance(metadata.get("scenes"), list) else []
    scene_names = [str(item.get("title") or "").strip() for item in scenes if isinstance(item, dict)]
    scene_names = [name for name in scene_names if name]
    # Keep the cover legible while retaining enough context to distinguish a
    # full chapter from another chapter with a similar visual motif.
    labels = names[:3] or scene_names[:2]
    if not labels:
        fallback_point = re.sub(r"^[A-Z]\d+\s*", "", point_text, flags=re.I)
        fallback_point = fallback_point.replace("全章交互", "").strip()
        labels = [fallback_point or chapter_text or "数学课件"]
    return {
        "cover_label": " · ".join(labels),
        "cover_topics": labels,
        "cover_source": "chapter-metadata" if metadata else "chapter-fallback",
    }


def safe_courseware_name(value: str, fallback: str = "课件") -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value or "")).strip()
    value = re.sub(r"\s+", " ", value)
    value = value or fallback
    return (fallback if value in {".", ".."} else value)[:180]


def bundled_courseware_for_stage(stage: str) -> tuple[dict, ...]:
    """Return imported v4 chapters whose HTML is already in the repository.

    The catalog is generated from the supplied release archives.  Keeping the
    lookup data beside the files lets the API expose the same standalone HTML
    that the frontend serves for preview/download, without rebuilding or
    rewriting the imported artifacts on first request.
    """
    try:
        with open(COURSEWARE_V4_CATALOG, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return ()
    rows = []
    for item in payload.get("entries", []):
        if item.get("stage") != stage:
            continue
        relative_path = str(item.get("coursewarePath") or "")
        path = os.path.abspath(os.path.join(os.path.dirname(COURSEWARE_DIR), relative_path))
        if os.path.commonpath([path, os.path.abspath(COURSEWARE_DIR)]) != os.path.abspath(COURSEWARE_DIR):
            continue
        if not os.path.isfile(path):
            continue
        rows.append({**item, "stored_path": path})
    return tuple(rows)


def courseware_directory(education_stage: str = "", chapter: str = "") -> str:
    """Return the human-readable stage/chapter storage directory."""
    stage_name = STAGE_DIRECTORY_NAMES.get(str(education_stage or "").strip(), "未分类")
    parts = [COURSEWARE_DIR, safe_courseware_name(stage_name, "未分类")]
    if chapter:
        parts.append(safe_courseware_name(chapter, "未命名章节"))
    return os.path.join(*parts)


def write_html_file(file_name: str, content_html: str, *, education_stage: str = "", chapter: str = "") -> str:
    directory = courseware_directory(education_stage, chapter)
    os.makedirs(directory, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.html"
    path = os.path.join(directory, stored)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content_html)
    return path


def build_demo_html(title: str, chapter: str, point: str, accent: str) -> str:
    """生成与预览使用相同状态模型的自包含示例课件。"""
    if "分数" in chapter:
        return _fraction_html(title, chapter, point, accent)
    if "函数基础" in chapter or "一次函数" in chapter:
        return _linear_html(title, chapter, point, accent)
    if "三角函数" in chapter or "单位圆" in chapter:
        return _circle_html(title, chapter, point, accent)
    if "长度、周长与面积" in chapter:
        return _area_html(title, chapter, point, accent)
    if "位置、方向" in chapter or "图形运动" in chapter:
        return _transformations_html(title, chapter, point, accent)
    if "图形变换与坐标" in chapter:
        return _transformations_html(title, chapter, point, accent)
    if "统计与数据分析" in chapter:
        return _statistics_html(title, chapter, point, accent)
    if "二次函数" in chapter:
        return _quadratic_html(title, chapter, point, accent)
    if "平面向量" in chapter:
        return _vectors_html(title, chapter, point, accent)
    if "导数与函数变化率" in chapter:
        return _derivative_html(title, chapter, point, accent)
    if "立体" in chapter or "正方体" in chapter or "圆柱体" in chapter:
        return build_solid_html(title, chapter, point, accent)
    if "三视图" in chapter or "从不同方向" in point:
        from services.courseware_3d import build_views_html
        return build_views_html(title, chapter, point, accent)
    if "空间几何" in chapter or "线面" in point or "面面" in point:
        return build_space_relations_html(title, chapter, point, accent)
    safe_title, safe_chapter, safe_point = map(html.escape, (title, chapter, point))
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title><style>
:root{{--accent:{accent};--ink:#18324b;--paper:#f6f8fb}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.65 system-ui,"Microsoft Yahei",sans-serif}}main{{max-width:960px;margin:0 auto;padding:42px 24px}}.hero{{background:white;border-radius:24px;padding:28px 32px;box-shadow:0 12px 40px #23405b16}}h1{{margin:.15em 0;font-size:clamp(28px,4vw,44px)}}.tag{{color:var(--accent);font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-top:20px}}.card{{background:white;border:1px solid #dfe8f1;border-radius:16px;padding:18px}}button{{border:0;border-radius:999px;background:var(--accent);color:white;padding:10px 18px;font-size:15px;cursor:pointer}}.answer{{display:none;margin-top:12px;background:#eef8f3;border-radius:12px;padding:10px}}button:focus-visible{{outline:3px solid #182f4c55;outline-offset:3px}}</style>
<main><section class="hero"><div class="tag">{safe_chapter} · {safe_point}</div><h1>{safe_title}</h1><p>点击卡片中的“演示”，观察步骤变化；你可以暂停、继续，也可以在课后再次打开这个文件。</p><div class="grid"><div class="card"><b>第一步：看清数量关系</b><p>把已知量和未知量用不同颜色标记。</p><button onclick="toggle(this)">演示</button><div class="answer" aria-live="polite">先圈出题目中的单位，再写出等量关系。</div></div><div class="card"><b>第二步：选择方法</b><p>拖慢一步，先说出“为什么这样算”。</p><button onclick="toggle(this)">演示</button><div class="answer" aria-live="polite">用线段图或表格表示关系，最后列式。</div></div><div class="card"><b>第三步：检查结果</b><p>把答案放回原题，检查单位和范围。</p><button onclick="toggle(this)">演示</button><div class="answer" aria-live="polite">单位正确且符合题意，才算完成。</div></div></div></section></main><script>function toggle(b){{const a=b.nextElementSibling;a.style.display=a.style.display==='block'?'none':'block';b.textContent=a.style.display==='block'?'收起':'演示'}}</script></html>'''


def _scene_steps(chapter: str) -> tuple[tuple[str, str], ...]:
    if "分数" in chapter:
        return (("观察分割", "两条同长度分数条"), ("标记分子", "填色份数随参数变化"), ("比较大小", "先看填色区域再读分数"), ("说出规律", "同分母比较分子"))
    if "函数基础" in chapter or "一次函数" in chapter:
        return (("观察坐标系", "直线与 y 轴截距点 B"), ("调整斜率 k", "直线绕 B 改变倾斜"), ("移动截距 b", "B 沿 y 轴上下移动"), ("验证公式", "用 y = kx + b 解释"))
    if "三角函数" in chapter or "单位圆" in chapter:
        return (("定位原点", "建立 x、y 坐标轴"), ("旋转半径", "拖动圆周点 A"), ("读出坐标", "观察 cos θ 与 sin θ"), ("验证恒等式", "sin²θ + cos²θ = 1"))
    if "长度、周长与面积" in chapter:
        return (("观察方格", "看长方形的长和宽"), ("拖动顶点", "改变两条边的长度"), ("读取结果", "面积与周长同步变化"), ("解释公式", "长×宽与 2×(长+宽)"))
    if "位置、方向" in chapter or "图形运动" in chapter:
        return (("观察网格", "读出点的数对"), ("平移图形", "改变横纵位移"), ("切换变换", "轴对称或旋转"), ("解释坐标", "用规则验证结果"))
    if "图形变换与坐标" in chapter:
        return (("观察坐标", "定位原图形顶点"), ("操作变换", "改变平移量或角度"), ("比较位置", "对应点坐标同步变化"), ("解释规律", "用变换规则验证"))
    if "统计与数据分析" in chapter:
        return (("观察条形图", "比较四类数据高低"), ("调整数据", "拖动参数改变一类"), ("读取平均数", "总和除以数据个数"), ("验证规律", "极值变化会影响平均数"))
    if "二次函数" in chapter:
        return (("观察抛物线", "顶点与对称轴清晰标出"), ("调整参数", "a、h、k 改变图像"), ("拖动顶点", "直接移动 V 点"), ("解释顶点式", "y=a(x−h)²+k"))
    if "平面向量" in chapter:
        return (("定位原点", "向量从 O 出发"), ("拖动终点", "改变方向和长度"), ("读取分量", "观察 x、y 投影"), ("验证长度", "√(x²+y²)"))
    if "导数与函数变化率" in chapter:
        return (("观察曲线", "在 y=x² 上选定 x₀"), ("改变间隔 h", "比较割线斜率"), ("逼近切线", "让 h 变小"), ("解释导数", "切线斜率等于 2x₀"))
    if "简单几何图形与测量" in chapter or "三视图" in chapter:
        return (("观察长方体", "确认长、宽、高"), ("切换视图", "正视图、俯视图、左视图"), ("旋转模型", "长按后拖动改变观察角度"), ("解释面积", "每个视图由两条棱决定"))
    return (("观察图形", "先看数量和关系"), ("改变参数", "让图形与数值同步"), ("解释变化", "用公式描述规律"), ("小练习", "换一组数据检查"))


def _scene_meta(chapter: str) -> tuple[str, str]:
    if "分数" in chapter:
        return ("理解分母表示总份数、分子表示填色份数，并比较同分母分数大小", "两条同长度分数条")
    if "函数基础" in chapter or "一次函数" in chapter:
        return ("观察 k 控制直线斜率、b 控制 y 轴截距，并用 y = kx + b 解释", "坐标系与一次函数直线")
    if "三角函数" in chapter or "单位圆" in chapter:
        return ("在单位圆上理解 A=(cos θ, sin θ)，并验证 sin²θ + cos²θ = 1", "原点 O、x/y 轴与单位圆")
    if "长度、周长与面积" in chapter:
        return ("用方格和拖动验证面积、周长的计算公式", "可拖动长方形与单位方格")
    if "位置、方向" in chapter or "图形运动" in chapter:
        return ("读数对并用坐标规则解释平移、轴对称和旋转", "坐标网格与可变换三角形")
    if "图形变换与坐标" in chapter:
        return ("用坐标规则描述平移、旋转和轴对称，检查对应点的位置变化", "坐标网格与三角形变换")
    if "统计与数据分析" in chapter:
        return ("用条形图比较数据，并理解平均数等于总和除以数据个数", "四类数据条形图与平均数")
    if "二次函数" in chapter:
        return ("观察 a、h、k 如何决定抛物线的开口、顶点和对称轴", "JSXGraph 坐标系与抛物线")
    if "平面向量" in chapter:
        return ("从同一个向量读取坐标分量、投影和长度", "坐标系、箭头与正交投影")
    if "导数与函数变化率" in chapter:
        return ("通过割线斜率逼近切线斜率，理解导数是函数在一点的瞬时变化率", "y=x² 曲线、割线与切线")
    if "简单几何图形与测量" in chapter or "三视图" in chapter:
        return ("用长方体的长、宽、高解释正视图、俯视图和左视图", "长方体与三个正投影视图")
    return ("建立图形、参数和公式之间的对应关系", "章节专属交互场景")


def _shell(title, chapter, point, accent, body, script):
    safe = [html.escape(str(v)) for v in (title, chapter, point)]
    steps = _scene_steps(chapter)
    objective, model_name = _scene_meta(chapter)
    step_html = "".join(
        f'<div class="step"><strong>{index} · {html.escape(step_title)}</strong>{html.escape(detail)}</div>'
        for index, (step_title, detail) in enumerate(steps, 1)
    )
    if "分数" in chapter:
        hero_art = '<svg class="hero-art" viewBox="0 0 220 110" aria-hidden="true"><rect x="12" y="30" width="190" height="18" rx="5" fill="#fff" stroke="#4c997e"/><rect x="12" y="30" width="105" height="18" rx="5" fill="#83c5ab"/><rect x="12" y="62" width="190" height="18" rx="5" fill="#fff" stroke="#4c997e"/><rect x="12" y="62" width="145" height="18" rx="5" fill="#a3d7c2"/></svg>'
    elif "函数基础" in chapter or "一次函数" in chapter:
        hero_art = '<svg class="hero-art" viewBox="0 0 220 110" aria-hidden="true"><line x1="12" y1="88" x2="208" y2="88" stroke="#789"/><line x1="108" y1="100" x2="108" y2="10" stroke="#789"/><line x1="22" y1="80" x2="198" y2="24" stroke="#7860aa" stroke-width="6" stroke-linecap="round"/><circle cx="108" cy="58" r="7" fill="#d66c62" stroke="#fff" stroke-width="2"/></svg>'
    elif "三角函数" in chapter or "单位圆" in chapter:
        hero_art = '<svg class="hero-art" viewBox="0 0 220 110" aria-hidden="true"><line x1="12" y1="55" x2="208" y2="55" stroke="#789"/><line x1="110" y1="102" x2="110" y2="8" stroke="#789"/><circle cx="110" cy="55" r="38" fill="none" stroke="#5d8eb8" stroke-width="3"/><line x1="110" y1="55" x2="140" y2="31" stroke="#7860aa" stroke-width="4"/><circle cx="140" cy="31" r="6" fill="#d66c62" stroke="#fff" stroke-width="2"/></svg>'
    elif "长度、周长与面积" in chapter:
        hero_art = '<svg class="hero-art" viewBox="0 0 220 110" aria-hidden="true"><rect x="30" y="24" width="132" height="62" rx="5" fill="#d9efe5" stroke="#4b9278" stroke-width="3"/><path d="M30 18v-8M162 18v-8M24 24h-8M24 86h-8" stroke="#4b9278" stroke-width="2"/><text x="92" y="16" text-anchor="middle" fill="#4b9278">长</text><text x="16" y="58" text-anchor="middle" fill="#4b9278" transform="rotate(-90 16 58)">宽</text></svg>'
    elif "二次函数" in chapter:
        hero_art = '<svg class="hero-art" viewBox="0 0 220 110" aria-hidden="true"><line x1="14" y1="88" x2="208" y2="88" stroke="#789"/><line x1="110" y1="102" x2="110" y2="8" stroke="#789"/><path d="M44 22 Q110 100 176 22" fill="none" stroke="#bd7a3c" stroke-width="5" stroke-linecap="round"/><circle cx="110" cy="88" r="7" fill="#d66c62" stroke="#fff" stroke-width="2"/></svg>'
    elif "平面向量" in chapter:
        hero_art = '<svg class="hero-art" viewBox="0 0 220 110" aria-hidden="true"><line x1="20" y1="86" x2="198" y2="86" stroke="#789"/><line x1="52" y1="102" x2="52" y2="12" stroke="#789"/><line x1="52" y1="86" x2="158" y2="32" stroke="#d66c62" stroke-width="6" stroke-linecap="round"/><path d="M158 32l-14 2 7 12z" fill="#d66c62"/><line x1="158" y1="32" x2="158" y2="86" stroke="#2878c8" stroke-dasharray="5 4"/><line x1="52" y1="86" x2="158" y2="86" stroke="#54a58c" stroke-dasharray="5 4"/></svg>'
    else:
        hero_art = ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe[0]}</title>
<style>:root{{--a:{accent};--ink:#203548;--muted:#627789;--line:#dbe6ed;--soft:#f3f8fb}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 10% 0%,#edf8f3,#f6f8fb 42%,#eef4fa);color:var(--ink);font:16px/1.65 system-ui,"Microsoft Yahei",sans-serif}}main{{max-width:980px;margin:auto;padding:28px 20px 48px}}section{{background:#fff;border:1px solid var(--line);border-radius:22px;padding:24px;box-shadow:0 14px 40px #23405b16}}.hero{{position:relative;overflow:hidden;background:linear-gradient(120deg,#fff,#f4fbf7);margin-bottom:18px}}.hero-art{{position:absolute;right:18px;top:12px;width:220px;height:110px;opacity:.82;border:0;background:transparent;box-shadow:none}}h1{{margin:6px 0 4px;font-size:clamp(28px,5vw,44px);letter-spacing:-.03em}}.tag{{color:var(--a);font-weight:700}}.hero-copy{{max-width:620px;color:#536b7d;margin:8px 0 0;position:relative;z-index:1}}.meta{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px;position:relative;z-index:1;max-width:680px}}.meta>div{{padding:11px 13px;background:#ffffffbf;border:1px solid var(--line);border-radius:12px}}.meta b{{display:block;color:var(--muted);font-size:12px;font-weight:500}}.scene{{padding:24px}}.board{{margin:18px 0;padding:20px;background:linear-gradient(145deg,#fbfdff,#f5f9fc);border:1px solid var(--line);border-radius:16px}}.step-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin:18px 0}}.step{{padding:10px 11px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--muted);font-size:13px}}.step strong{{display:block;color:var(--ink);font-size:14px}}.step:first-child{{border-color:var(--a);box-shadow:0 0 0 2px {accent}18}}.controls{{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin:12px 0}}label{{display:inline-flex;gap:8px;align-items:center;margin:6px 12px 6px 0;color:#40576a;font-weight:600}}input{{accent-color:var(--a);min-width:150px}}button{{border:0;border-radius:9px;background:var(--a);color:white;padding:9px 15px;cursor:pointer;margin:4px;box-shadow:0 3px 8px {accent}30}}button.secondary{{background:#fff;color:var(--a);border:1px solid {accent}66;box-shadow:none}}button:focus-visible,input:focus-visible{{outline:3px solid {accent}55;outline-offset:3px}}.hint{{margin:0;padding:12px 14px;background:#f4faf7;border-left:4px solid var(--a);border-radius:8px;color:#496477}}.legend{{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:13px}}.formula{{font-size:22px;font-weight:700;color:var(--a);letter-spacing:.02em}}.answer{{font-weight:600;color:#27654d;background:#edf8f2;border-radius:10px;padding:12px 14px;min-height:24px}}.footer-note{{color:var(--muted);font-size:13px}}svg{{max-width:100%;height:auto;user-select:none;-webkit-user-select:none;background:#fff;border:1px solid #dbe6ed;border-radius:14px;box-shadow:0 6px 20px #25425812;shape-rendering:geometricPrecision}}#plot{{touch-action:none;background-image:linear-gradient(#e8eff4 1px,transparent 1px),linear-gradient(90deg,#e8eff4 1px,transparent 1px);background-size:30px 30px}}#circle{{touch-action:none;background:radial-gradient(circle at 50% 50%,#fff 0,#f5faff 75%)}}#plot .main-line{{pointer-events:none}}#plot .intercept-handle{{cursor:grab;transform-box:fill-box;transform-origin:center;transition:transform .18s ease,filter .18s ease}}#plot .intercept-handle.is-dragging{{transform:scale(1.35);filter:drop-shadow(0 3px 5px #315f8c55)}}#circle .drag-point{{cursor:grab;transform-box:fill-box;transform-origin:center;transition:transform .18s ease,filter .18s ease}}#circle .drag-point.is-dragging{{transform:scale(1.3);filter:drop-shadow(0 3px 5px #315f8c55)}}@media(max-width:680px){{main{{padding:14px 10px 30px}}section{{padding:18px}}.hero-art{{opacity:.3;right:-36px}}.meta,.step-grid{{grid-template-columns:repeat(2,1fr)}}.controls{{display:grid;grid-template-columns:1fr}}label{{justify-content:space-between}}input{{flex:1}}}}@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important;transition:none!important}}}}</style></head><body><main><section class="hero"><div class="tag">{safe[1]} · {safe[2]}</div><h1>{safe[0]}</h1><p class="hero-copy">本课件展示 {safe[2]} 的图形、参数与规律。</p><div class="meta"><div><b>课堂目标</b>{html.escape(objective)}</div><div><b>知识点</b>{safe[2]}</div><div><b>图形模型</b>{html.escape(model_name)}</div></div>{hero_art}</section><section class="scene"><div class="step-grid">{step_html}</div>{body}</section></main><script>{script}</script></body></html>'''
def _exercise_html(question: str, answer: str) -> str:
    return f'<div class="board"><strong>小练习</strong><p>{html.escape(question)}</p><details><summary>显示答案</summary><p class="answer" aria-live="polite">{html.escape(answer)}</p></details></div>'


def _fraction_html(title, chapter, point, accent):
    body = '''<style>.fraction-bars{display:grid;gap:18px;margin:18px 0}.bar-row{display:flex;align-items:flex-start;gap:12px}.bar-caption{width:48px;display:grid;gap:2px;text-align:center;color:#40576a}.bar-caption strong{font-size:20px;color:var(--a)}.bar-column{flex:1;min-width:0}.fraction-bar{display:flex;width:100%;height:42px;box-shadow:0 4px 12px #25425812}.fraction-cell{flex:1;margin:0;padding:0;border:1px solid #7aa891;border-right-width:0;border-radius:0;background:#edf3ef;cursor:ew-resize}.fraction-cell:first-child{border-radius:8px 0 0 8px}.fraction-cell:last-child{border-right-width:1px;border-radius:0 8px 8px 0}.fraction-cell.filled{background:linear-gradient(180deg,#8bd0b2,#54aa8c)}.fraction-cell:focus-visible{position:relative;z-index:1;outline:3px solid var(--a);outline-offset:2px}.bar-labels{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:5px}.bar-labels span:first-child{color:var(--a);font-weight:600;white-space:nowrap}.fraction-cell{transition:transform .18s ease,box-shadow .18s ease,background .2s ease}.fraction-bars.is-changing .fraction-cell{animation:cellChange .32s ease}.fraction-cell.is-pressing{transform:scale(1.08);box-shadow:0 4px 10px #25425838;position:relative;z-index:1}@keyframes cellChange{0%{transform:scale(.96)}55%{transform:scale(1.06)}100%{transform:scale(1)}}.cos-key,.cos-value{color:#2878c8;font-weight:600}.sin-key,.sin-value{color:#d66c62;font-weight:600}</style><p class="hint">观察两条同长度分数条；拖动分子后先猜，再显示比较结果。</p><p class="direct-tip">直接点击或拖动分数条的格子，调整对应分子。每一格代表一份，分母是总份数。</p><div class="board"><div class="controls"><label>分母（总份数） <input id="d" type="range" min="2" max="12" value="8"><output id="dValue">8</output></label><label>分子 A（填色份数） <input id="a" type="range" min="0" max="8" value="3"><output id="aValue">3</output></label><label>分子 B（填色份数） <input id="b" type="range" min="0" max="8" value="5"><output id="bValue">5</output></label></div><div id="bars" class="fraction-bars" aria-live="polite"></div><p id="formula" class="formula"></p><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),bars=$('bars'),f=$('formula'),ans=$('answer');let revealed=false,dragBar='',pressTimer;function row(n,v,d){const ratio=v/d;return `<div class="bar-row"><div class="bar-caption"><strong>${n}</strong><span>${v}/${d}</span></div><div class="bar-column"><div class="fraction-bar" data-bar="${n.toLowerCase()}" role="group" aria-label="${n}分数条">${Array.from({length:d},(_,i)=>`<button type="button" class="fraction-cell ${i<v?'filled':''}" data-index="${i+1}" aria-label="${n}第${i+1}份"></button>`).join('')}</div><div class="bar-labels"><span style="width:${Math.max(ratio*100,8)}%">分子（填色份数）</span><span>分母（总份数）</span></div></div></div>`;}function draw(){let d=+$('d').value,a=Math.min(+$('a').value,d),b=Math.min(+$('b').value,d);$('a').max=$('b').max=d;$('dValue').textContent=d;$('aValue').textContent=a;$('bValue').textContent=b;bars.innerHTML=row('A',a,d)+row('B',b,d);f.textContent=`${a}/${d} ${revealed?(a===b?'=':a>b?'>':'<'):'?'} ${b}/${d}`;if(revealed)ans.textContent='同分母时，分子越大，填色份数越多，分数越大。';}function setCell(e){const cell=e.target.closest('[data-index]');if(!cell)return;const bar=cell.closest('[data-bar]')?.dataset.bar;if(!bar)return;const index=+cell.dataset.index;$(bar).value=Math.min(index,+$('d').value);revealed=false;draw();bars.classList.remove('is-changing');void bars.offsetWidth;bars.classList.add('is-changing');const fresh=bars.querySelector('[data-bar="'+bar+'"] [data-index="'+index+'"]');fresh?.classList.add('is-pressing');clearTimeout(pressTimer);pressTimer=setTimeout(()=>fresh?.classList.remove('is-pressing'),280);}bars.onpointerdown=e=>{const cell=e.target.closest('[data-index]');if(cell){dragBar=cell.closest('[data-bar]').dataset.bar;setCell(e);}};bars.onpointermove=e=>{if(dragBar)setCell(e);};bars.onpointerup=bars.onpointerleave=bars.onpointercancel=()=>dragBar='';bars.onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('[data-index]')){e.preventDefault();setCell(e);}};['d','a','b'].forEach(id=>$(id).oninput=draw);$('show').onclick=()=>{revealed=true;draw()};$('guess').onclick=()=>{ans.textContent='先指着两条分数条猜一猜：哪条填色更多？';};$('reset').onclick=()=>{$('d').value=8;$('a').value=3;$('b').value=5;revealed=false;ans.textContent='';draw()};draw();'''
    body += _exercise_html("把 A 设为 6/8，B 设为 4/8，哪个分数大？为什么？", "6/8 > 4/8。分母相同，比较分子；6 > 4，所以 A 的填色份数更多。")
    return _shell(title, chapter, point, accent, body, script)


def _linear_html(title, chapter, point, accent):
    body = '''<p class="hint">用参数条调整 k 观察直线倾斜；点击或拖动 y 轴截距 B 观察整条直线上下平移。</p><p class="direct-tip">斜率 k 用参数条精确调整；直接长按 y 轴上的截距点 B 后拖动，直线会整条上下平移。拖动时可左右移动，B 始终保持在 y 轴上。</p><div class="board"><label>k（斜率） <input id="k" type="range" min="-3" max="3" step="0.1" value="1"><output id="kValue">1.0</output></label><label>b（y 轴截距） <output id="bValue">0.0</output><input id="b" type="hidden" value="0"></label><svg id="plot" viewBox="0 0 440 260" role="img" aria-label="一次函数坐标系"></svg><p id="formula" class="formula"></p><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),svg=$('plot'),f=$('formula'),ans=$('answer'),bv=$('bValue'),kv=$('kValue');let revealed=false,dragging=false,pressTimer=null;function xy(e){const r=svg.getBoundingClientRect();return{x:-5+((e.clientX-r.left)/r.width)*10,y:(130-((e.clientY-r.top)/r.height)*260)/(220/12)};}function draw(){const k=+$('k').value,b=+$('b').value,X=x=>40+(x+5)*36,Y=y=>130-y*(220/12);const xTicks=[-5,-4,-3,-2,-1,1,2,3,4,5].map(x=>`<line x1="${X(x)}" y1="${Y(0)-4}" x2="${X(x)}" y2="${Y(0)+4}" stroke="#789"/><text x="${X(x)-4}" y="${Y(0)+18}" font-size="10" fill="#627789">${x}</text>`).join('');const yTicks=[-5,-4,-3,-2,-1,1,2,3,4,5].map(y=>`<line x1="${X(0)-4}" y1="${Y(y)}" x2="${X(0)+4}" y2="${Y(y)}" stroke="#789"/><text x="${X(0)+8}" y="${Y(y)+4}" font-size="10" fill="#627789">${y}</text>`).join('');const axes=`<defs><linearGradient id="lineGradient" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#4c8fc8"/><stop offset=".5" stop-color="#8063b4"/><stop offset="1" stop-color="#4c8fc8"/></linearGradient><filter id="lineShadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#315f8c" flood-opacity=".2"/></filter><marker id="axisArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#789"/></marker></defs><line x1="${X(-5)}" y1="${Y(0)}" x2="${X(5)}" y2="${Y(0)}" stroke="#789" marker-end="url(#axisArrow)"/><line x1="${X(0)}" y1="${Y(-6)}" x2="${X(0)}" y2="${Y(6)}" stroke="#789" marker-end="url(#axisArrow)"/><text x="${X(0)+8}" y="${Y(0)+16}" font-size="11" fill="#536b7d">O</text><text x="${X(5)-4}" y="${Y(0)-8}" font-size="12" fill="#536b7d">x</text><text x="${X(0)+8}" y="${Y(6)+12}" font-size="12" fill="#536b7d">y</text>${xTicks}${yTicks}`;const y1=Y(k*-5+b),y2=Y(k*5+b);const line=`<line class="main-line" x1="${X(-5)}" y1="${y1}" x2="${X(5)}" y2="${y2}" stroke="#fff" stroke-width="10" stroke-linecap="round" opacity=".7"/><line class="main-line" x1="${X(-5)}" y1="${y1}" x2="${X(5)}" y2="${y2}" stroke="url(#lineGradient)" stroke-width="6" stroke-linecap="round" filter="url(#lineShadow)"/><circle class="intercept-handle${dragging?' is-dragging':''}" cx="${X(0)}" cy="${Y(b)}" r="10" fill="#d66c62" stroke="#fff" stroke-width="3" tabindex="0" role="slider" aria-label="截距 b" aria-valuemin="-4" aria-valuemax="4" aria-valuenow="${b.toFixed(1)}"/><text x="${X(0)+15}" y="${Y(b)-13}" font-size="12" font-weight="600" fill="#a84d43">B（0, ${b.toFixed(1)}）</text>`;svg.innerHTML=axes+line;kv.textContent=k.toFixed(1);bv.textContent=b.toFixed(1);f.textContent=`y = ${k.toFixed(1)}x ${b>=0?'+':'−'} ${Math.abs(b).toFixed(1)}；k=${k.toFixed(1)}（斜率），b=${b.toFixed(1)}（截距）`;if(revealed)ans.textContent='k 决定倾斜方向和陡峭程度，b 决定 y 轴截距；拖动 B 时整条直线上下平移。';}function setB(e){const p=xy(e);$('b').value=Math.max(-4,Math.min(4,p.y)).toFixed(1);draw();}svg.onpointerdown=e=>{const p=xy(e);if(Math.abs(p.x)<1.15){clearTimeout(pressTimer);pressTimer=setTimeout(()=>{dragging=true;svg.setPointerCapture?.(e.pointerId);setB(e);},350);}};svg.onpointermove=e=>{if(dragging)setB(e)};svg.onpointerup=svg.onpointerleave=svg.onpointercancel=()=>{clearTimeout(pressTimer);pressTimer=null;dragging=false;draw()};svg.onkeydown=e=>{if(!e.target.matches('.intercept-handle')||!['ArrowUp','ArrowRight','ArrowDown','ArrowLeft'].includes(e.key))return;e.preventDefault();$('b').value=Math.max(-4,Math.min(4,+$('b').value+(e.key==='ArrowUp'||e.key==='ArrowRight'?0.1:-0.1))).toFixed(1);draw();};$('k').oninput=draw;$('show').onclick=()=>{revealed=true;draw()};$('guess').onclick=()=>{ans.textContent='猜一猜：改变 b 时直线会不会改变倾斜方向？'};$('reset').onclick=()=>{$('k').value=1;$('b').value=0;revealed=false;ans.textContent='';draw()};draw();'''
    body += _exercise_html("当 k = −1、b = 2 时，直线与 y 轴交于哪里？x = 2 时 y 是多少？", "交点是 B（0, 2）；代入 y = −x + 2，x = 2 时 y = 0。")
    return _shell(title, chapter, point, accent, body, script)


def _circle_html(title, chapter, point, accent):
    body = '''<p class="hint">旋转单位圆半径线，观察横向 cos、纵向 sin 投影、坐标和 sin²θ+cos²θ=1。</p><p class="direct-tip">长按圆周上的点 A 后拖动来旋转半径线；角度条可做精确调整。图中 O 为原点，x、y 为坐标轴。</p><div class="board"><label>θ（角度） <input id="angle" type="range" min="0" max="360" value="45"><output id="angleValue">45°</output></label><div class="legend"><span class="cos-key">横向投影 = cos θ</span><span class="sin-key">纵向投影 = sin θ</span><span>半径 OA = 1</span></div><svg id="circle" viewBox="0 0 420 320" role="img" aria-label="带原点和坐标轴的单位圆"></svg><p id="values"></p><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),svg=$('circle'),v=$('values'),ans=$('answer'),av=$('angleValue'),angle=$('angle');let revealed=false,dragging=false,pressTimer=null;function pos(e){const r=svg.getBoundingClientRect();return{x:(e.clientX-r.left)/r.width*420,y:(e.clientY-r.top)/r.height*320};}function draw(){let deg=+angle.value,a=deg*Math.PI/180,c=Math.cos(a),s=Math.sin(a),cx=210,cy=160,r=110,px=c*r+cx,py=cy-s*r,arcR=38,ax=cx+arcR,ay=cy,ex=cx+c*arcR,ey=cy-s*arcR;const arcPath=deg===0?'':deg===360?`M ${ax} ${ay} A ${arcR} ${arcR} 0 1 0 ${cx-arcR} ${cy} A ${arcR} ${arcR} 0 1 0 ${ax} ${ay}`:`M ${ax} ${ay} A ${arcR} ${arcR} 0 ${deg>180?1:0} 0 ${ex} ${ey}`;const xTicks=[-1,1].map(n=>`<line x1="${cx+n*r}" y1="${cy-6}" x2="${cx+n*r}" y2="${cy+6}" stroke="#789"/><text x="${cx+n*r-4}" y="${cy+19}" font-size="11" fill="#627789">${n}</text>`).join('');const yTicks=[-1,1].map(n=>`<line x1="${cx-6}" y1="${cy-n*r}" x2="${cx+6}" y2="${cy-n*r}" stroke="#789"/><text x="${cx+9}" y="${cy-n*r+4}" font-size="11" fill="#627789">${n}</text>`).join('');svg.innerHTML=`<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#789"/></marker><filter id="circleShadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="3" stdDeviation="3" flood-color="#315f8c" flood-opacity=".18"/></filter></defs><line x1="30" y1="${cy}" x2="390" y2="${cy}" stroke="#789" stroke-width="1.5" marker-end="url(#arrow)"/><line x1="${cx}" y1="295" x2="${cx}" y2="20" stroke="#789" stroke-width="1.5" marker-end="url(#arrow)"/>${xTicks}${yTicks}<text x="${cx+8}" y="${cy+16}" font-size="12" fill="#536b7d">O</text><text x="392" y="${cy-8}" font-size="13" fill="#536b7d">x</text><text x="${cx+10}" y="18" font-size="13" fill="#536b7d">y</text><circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#5d8eb8" stroke-width="3" filter="url(#circleShadow)"/><path d="${arcPath}" fill="none" stroke="#9a5b13" stroke-width="3" stroke-linecap="round"/><line x1="${cx}" y1="${cy}" x2="${px}" y2="${py}" stroke="var(--a)" stroke-width="6" stroke-linecap="round"/><line x1="${px}" y1="${py}" x2="${px}" y2="${cy}" stroke="#d66c62" stroke-width="2" stroke-dasharray="5 4"/><line x1="${cx}" y1="${cy}" x2="${px}" y2="${cy}" stroke="#2878c8" stroke-width="2" stroke-dasharray="5 4"/><circle class="drag-point${dragging ? ' is-dragging' : ''}" cx="${px}" cy="${py}" r="11" fill="var(--a)" stroke="#fff" stroke-width="3" tabindex="0" role="slider" aria-label="角度 θ" aria-valuemin="0" aria-valuemax="360" aria-valuenow="${deg}"/><text x="${px>cx?px-16:px+16}" y="${py-14}" text-anchor="${px>cx?'end':'start'}" font-size="16" font-weight="700" fill="var(--a)">A</text><text x="${px>cx?px-16:px+16}" y="${py+5}" text-anchor="${px>cx?'end':'start'}" font-size="12"><tspan fill="#2878c8">(${c.toFixed(2)}</tspan><tspan fill="#536b7d">, </tspan><tspan fill="#d66c62">${s.toFixed(2)})</tspan></text><text x="${(cx+px)/2}" y="${cy+16}" fill="#2878c8">cos θ</text><text x="${px+8}" y="${(py+cy)/2}" fill="#b34">sin θ</text>`;av.textContent=deg+'°';v.innerHTML=`点 A 坐标 <span class="cos-value">(cos θ = ${c.toFixed(3)}</span><span class="sin-value">, sin θ = ${s.toFixed(3)})</span>；半径 = 1`;if(revealed)ans.textContent=`sin²θ + cos²θ = ${(s*s+c*c).toFixed(3)} = 1`;}function setAngle(e){const p=pos(e);const deg=Math.atan2(160-p.y,p.x-210)*180/Math.PI;angle.value=(deg+360)%360;draw();}svg.onpointerdown=e=>{const p=pos(e);if(Math.hypot(p.x-210,p.y-160)<145){clearTimeout(pressTimer);pressTimer=setTimeout(()=>{dragging=true;svg.setPointerCapture?.(e.pointerId);setAngle(e);},350);}};svg.onpointermove=e=>{if(dragging)setAngle(e)};svg.onpointerup=svg.onpointerleave=svg.onpointercancel=()=>{clearTimeout(pressTimer);pressTimer=null;dragging=false;draw()};svg.onkeydown=e=>{if(!e.target.matches('[role="slider"]')||!['ArrowUp','ArrowRight','ArrowDown','ArrowLeft'].includes(e.key))return;e.preventDefault();angle.value=(+angle.value+(e.key==='ArrowUp'||e.key==='ArrowRight'?5:-5)+360)%360;draw();};angle.oninput=draw;$('show').onclick=()=>{revealed=true;draw()};$('guess').onclick=()=>ans.textContent='先猜一猜：角度变大时，横向和纵向投影怎样变化？';$('reset').onclick=()=>{angle.value=45;revealed=false;ans.textContent='';draw()};draw();'''
    body += _exercise_html("θ = 150° 时，sin θ 和 cos θ 的符号分别是什么？它们的平方和是多少？", "点 A 在第二象限，所以 sin θ > 0、cos θ < 0；半径 OA = 1，因此 sin²θ + cos²θ = 1。")
    return _shell(title, chapter, point, accent, body, script)


def _area_html(title, chapter, point, accent):
    body = '''<style>
.area-board{display:grid;gap:14px}.area-svg{width:100%;max-height:380px;touch-action:none;background:linear-gradient(145deg,#fcfffd,#f5faf8)}
.area-grid{stroke:#cfe4da;stroke-width:1}.area-outline{fill:#cfeee0;fill-opacity:.82;stroke:#438c72;stroke-width:3}.area-handle{fill:#d66c62;stroke:#fff;stroke-width:4;cursor:grab;transition:transform .18s ease,filter .18s ease;transform-box:fill-box;transform-origin:center}.area-handle.is-dragging{transform:scale(1.35);filter:drop-shadow(0 3px 5px #315f8c55)}
.area-metric{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.area-metric div{padding:10px 12px;border:1px solid #dbece3;border-radius:10px;background:#fbfefc}.area-metric b{display:block;color:#627789;font-size:12px}.area-metric strong{font-size:20px;color:#3f8b70}@media(max-width:640px){.area-metric{grid-template-columns:1fr 1fr}.area-metric div:last-child{grid-column:1/-1}}
</style><p class="hint">观察方格中的长方形；拖动右上角点 A，长和宽、面积与周长会一起变化。</p><p class="direct-tip">长方形内部的每一个小格代表 1 个平方单位。长按点 A 后拖动可直接改变两条边。</p><div class="board area-board"><div class="controls"><label>长 <input id="width" type="range" min="2" max="12" step="1" value="8"><output id="widthValue">8</output></label><label>宽 <input id="height" type="range" min="2" max="10" step="1" value="5"><output id="heightValue">5</output></label></div><svg id="area" class="area-svg" viewBox="0 0 520 330" role="img" aria-label="单位方格中的可拖动长方形"></svg><div class="area-metric"><div><b>面积</b><strong id="areaValue">40</strong> 平方单位</div><div><b>周长</b><strong id="perimeterValue">26</strong> 单位</div><div><b>状态</b><strong id="shapeValue">长方形</strong></div></div><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),svg=$('area'),answer=$('answer'),wInput=$('width'),hInput=$('height');let w=8,h=5,revealed=false,dragging=false,pressTimer=null;const ox=82,oy=265,scale=25;function point(e){const r=svg.getBoundingClientRect();return{x:(e.clientX-r.left)/r.width*520,y:(e.clientY-r.top)/r.height*330};}function clamp(v,min,max){return Math.max(min,Math.min(max,v));}function draw(){const rw=w*scale,rh=h*scale,x=ox,y=oy-rh;let grid='';for(let i=0;i<=w;i++)grid+=`<line class="area-grid" x1="${x+i*scale}" y1="${y}" x2="${x+i*scale}" y2="${oy}"/>`;for(let j=0;j<=h;j++)grid+=`<line class="area-grid" x1="${x}" y1="${y+j*scale}" x2="${x+rw}" y2="${y+j*scale}"/>`;svg.innerHTML=`<defs><marker id="areaArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#438c72"/></marker></defs>${grid}<rect class="area-outline" x="${x}" y="${y}" width="${rw}" height="${rh}" rx="6"/><line x1="${x}" y1="${oy+16}" x2="${x+rw}" y2="${oy+16}" stroke="#438c72" marker-end="url(#areaArrow)"/><line x1="${x-16}" y1="${oy}" x2="${x-16}" y2="${y}" stroke="#438c72" marker-end="url(#areaArrow)"/><text x="${x+rw/2}" y="${oy+34}" text-anchor="middle" fill="#438c72" font-weight="700">长 = ${w}</text><text x="${x-28}" y="${y+rh/2}" text-anchor="middle" fill="#438c72" font-weight="700" transform="rotate(-90 ${x-28} ${y+rh/2})">宽 = ${h}</text><circle class="area-handle${dragging?' is-dragging':''}" cx="${x+rw}" cy="${y}" r="11" tabindex="0" role="slider" aria-label="长方形顶点 A" aria-valuemin="2" aria-valuemax="12" aria-valuenow="${w}"/><text x="${x+rw+13}" y="${y-11}" fill="#a84d43" font-size="16" font-weight="700">A</text>`;wInput.value=w;hInput.value=h;$('widthValue').textContent=w;$('heightValue').textContent=h;$('areaValue').textContent=w*h;$('perimeterValue').textContent=2*(w+h);$('shapeValue').textContent=w===h?'正方形':'长方形';if(revealed)answer.textContent=`面积 = ${w}×${h} = ${w*h}；周长 = 2×(${w}+${h}) = ${2*(w+h)}。`;}function setByPoint(e){const p=point(e);w=clamp(Math.round((p.x-ox)/scale),2,12);h=clamp(Math.round((oy-p.y)/scale),2,10);draw();}svg.onpointerdown=e=>{const p=point(e),hx=ox+w*scale,hy=oy-h*scale;if(Math.hypot(p.x-hx,p.y-hy)<28){clearTimeout(pressTimer);pressTimer=setTimeout(()=>{dragging=true;svg.setPointerCapture?.(e.pointerId);setByPoint(e);},350);}};svg.onpointermove=e=>{if(dragging)setByPoint(e)};svg.onpointerup=svg.onpointercancel=svg.onpointerleave=()=>{clearTimeout(pressTimer);pressTimer=null;if(dragging){dragging=false;draw();}};svg.onkeydown=e=>{if(!e.target.matches('.area-handle'))return;const step=e.shiftKey?2:1;if(['ArrowRight','ArrowLeft'].includes(e.key)){e.preventDefault();w=clamp(w+(e.key==='ArrowRight'?step:-step),2,12);draw();}if(['ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();h=clamp(h+(e.key==='ArrowUp'?step:-step),2,10);draw();}};wInput.oninput=()=>{w=+wInput.value;draw()};hInput.oninput=()=>{h=+hInput.value;draw()};$('guess').onclick=()=>{answer.textContent='先猜一猜：长增加 1 格时，面积和周长各增加多少？'};$('show').onclick=()=>{revealed=true;draw()};$('reset').onclick=()=>{w=8;h=5;revealed=false;answer.textContent='';draw()};draw();'''
    body += _exercise_html("把长方形改成 6×4，面积和周长各是多少？", "面积 6×4=24 平方单位；周长 2×(6+4)=20 单位。")
    return _shell(title, chapter, point, accent, body, script)


def _transformations_html(title, chapter, point, accent):
    body = '''<style>.transform-board{display:grid;gap:14px}.transform-svg{width:100%;max-height:430px;touch-action:none;background:#f8fbfe}.grid-line{stroke:#d9e4ed;stroke-width:1}.axis-line{stroke:#71869a;stroke-width:1.8}.original{fill:#d9e9f8;stroke:#2878c8;stroke-width:3}.moved{fill:#f7d8d4;fill-opacity:.9;stroke:#c95c57;stroke-width:3}.vertex-label{font-size:12px;font-weight:700}.transform-values{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.transform-values div{padding:10px 12px;border:1px solid #dbe5ef;border-radius:10px;background:#fbfdff}.transform-values b{display:block;color:#627789;font-size:12px}.transform-values strong{font-size:16px;color:#315f8c}@media(max-width:640px){.transform-values{grid-template-columns:1fr 1fr}.transform-values div:last-child{grid-column:1/-1}}.transform-buttons{display:flex;gap:8px;flex-wrap:wrap}.transform-buttons button.active{outline:3px solid #315f8c44}</style><p class="hint">观察蓝色原图形的顶点数对；切换变换或调整参数，红色图形与坐标同步变化。</p><p class="direct-tip">平移改变位置；轴对称和绕原点旋转只改变方向/位置，不改变边长和形状。</p><div class="board transform-board"><div class="transform-buttons" role="group" aria-label="选择图形变换"><button type="button" data-transform="translation">平移</button><button type="button" data-transform="reflectX">关于 x 轴对称</button><button type="button" data-transform="reflectY">关于 y 轴对称</button><button type="button" data-transform="rotate">绕原点旋转</button></div><div class="controls"><label>横向位移 dx <input id="dx" type="range" min="-4" max="4" step="1" value="2"><output id="dxValue">2</output></label><label>纵向位移 dy <input id="dy" type="range" min="-4" max="4" step="1" value="1"><output id="dyValue">1</output></label><label>旋转角 <select id="angle"><option value="90">90°</option><option value="180">180°</option><option value="270">270°</option></select></label></div><svg id="transform" class="transform-svg" viewBox="0 0 560 390" role="img" aria-label="坐标网格中的图形变换" tabindex="0"></svg><div class="transform-values"><div><b>原图形顶点 A</b><strong id="originalValue"></strong></div><div><b>变换后顶点 A′</b><strong id="movedValue"></strong></div><div><b>规律</b><strong id="ruleValue"></strong></div></div><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),svg=$('transform'),answer=$('answer');const base=[[1,1],[4,1],[1,3]];let state={transform:'translation',dx:2,dy:1,angle:90,revealed:false,dragging:false},pressTimer=null;const cx=280,cy=200,scale=38;function mapPoint(p){const x=p[0],y=p[1];if(state.transform==='translation')return[x+state.dx,y+state.dy];if(state.transform==='reflectX')return[x,-y];if(state.transform==='reflectY')return[-x,y];if(state.angle===90)return[-y,x];if(state.angle===180)return[-x,-y];return[y,-x];}function fmt(p){return '('+p[0]+', '+p[1]+')';}function px(p){return[cx+p[0]*scale,cy-p[1]*scale];}function draw(){const moved=base.map(mapPoint);let grid='';for(let i=-6;i<=6;i++)grid+='<line class="grid-line" x1="'+(cx+i*scale)+'" y1="'+(cy-5*scale)+'" x2="'+(cx+i*scale)+'" y2="'+(cy+5*scale)+'"/><line class="grid-line" x1="'+(cx-6*scale)+'" y1="'+(cy-i*scale)+'" x2="'+(cx+6*scale)+'" y2="'+(cy-i*scale)+'"/>';const poly=pts=>pts.map(p=>px(p).join(',')).join(' ');const vertex=(pts,prefix,color)=>pts.map((p,i)=>{const q=px(p);return '<circle cx="'+q[0]+'" cy="'+q[1]+'" r="7" fill="'+color+'"/><text class="vertex-label" x="'+(q[0]+9)+'" y="'+(q[1]-8)+'" fill="'+color+'">'+prefix+'ABC'[i]+' '+fmt(p)+'</text>';}).join('');let rule=state.transform==='translation'?'(x+'+state.dx+', y+'+state.dy+')':state.transform==='reflectX'?'(x, −y)':state.transform==='reflectY'?'(−x, y)':state.angle===90?'(−y, x)':state.angle===180?'(−x, −y)':'(y, −x)';svg.innerHTML=grid+'<line class="axis-line" x1="'+(cx-6*scale)+'" y1="'+cy+'" x2="'+(cx+6*scale)+'" y2="'+cy+'"/><line class="axis-line" x1="'+cx+'" y1="'+(cy+5*scale)+'" x2="'+cx+'" y2="'+(cy-5*scale)+'"/><text x="'+(cx+6*scale-10)+'" y="'+(cy-8)+'" fill="#536b7d">x</text><text x="'+(cx+8)+'" y="'+(cy-5*scale+14)+'" fill="#536b7d">y</text><polygon class="original" points="'+poly(base)+'"/><polygon class="moved" points="'+poly(moved)+'"/>'+vertex(base,'','#2878c8')+vertex(moved,'′','#c95c57');$('dx').value=state.dx;$('dy').value=state.dy;$('dxValue').textContent=state.dx;$('dyValue').textContent=state.dy;$('angle').value=state.angle;$('originalValue').textContent=fmt(base[0]);$('movedValue').textContent=fmt(moved[0]);$('ruleValue').textContent=rule;document.querySelectorAll('[data-transform]').forEach(b=>b.classList.toggle('active',b.dataset.transform===state.transform));if(state.revealed)answer.textContent='坐标规则：'+rule+'；三角形三边长度和形状保持不变。'}function dragPoint(e){const r=svg.getBoundingClientRect();const x=(e.clientX-r.left)/r.width*560,y=(e.clientY-r.top)/r.height*390;state.dx=Math.max(-4,Math.min(4,Math.round((x-cx)/scale-base[0][0])));state.dy=Math.max(-4,Math.min(4,Math.round((cy-y)/scale-base[0][1])));draw();}document.querySelectorAll('[data-transform]').forEach(b=>b.onclick=()=>{state.transform=b.dataset.transform;draw();});$('dx').oninput=()=>{state.dx=+$('dx').value;draw();};$('dy').oninput=()=>{state.dy=+$('dy').value;draw();};$('angle').onchange=()=>{state.angle=+$('angle').value;draw();};svg.onpointerdown=e=>{if(state.transform==='translation'){clearTimeout(pressTimer);pressTimer=setTimeout(()=>{state.dragging=true;svg.setPointerCapture&&svg.setPointerCapture(e.pointerId);dragPoint(e);},350);}};svg.onpointermove=e=>{if(state.dragging)dragPoint(e);};svg.onpointerup=svg.onpointercancel=svg.onpointerleave=()=>{clearTimeout(pressTimer);pressTimer=null;state.dragging=false;};svg.onkeydown=e=>{if(state.transform!=='translation')return;if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();state.dx=Math.max(-4,Math.min(4,state.dx+(e.key==='ArrowRight'?1:-1)));draw();}if(e.key==='ArrowUp'||e.key==='ArrowDown'){e.preventDefault();state.dy=Math.max(-4,Math.min(4,state.dy+(e.key==='ArrowUp'?1:-1)));draw();}};$('guess').onclick=()=>{answer.textContent='先猜一猜：平移 (dx,dy) 后，点 (x,y) 的新坐标怎样写？';};$('show').onclick=()=>{state.revealed=true;draw();};$('reset').onclick=()=>{state={transform:'translation',dx:2,dy:1,angle:90,revealed:false,dragging:false};answer.textContent='';draw();};draw();'''
    body += _exercise_html("点 A(2,1) 关于 y 轴对称后的坐标是什么？点 (1,2) 绕原点逆时针旋转 90° 后呢？", "关于 y 轴对称为 (−2,1)；绕原点逆时针 90° 为 (−2,1)。")
    return _shell(title, chapter, point, accent, body, script)


def _quadratic_html(title, chapter, point, accent):
    vendor_dir = os.path.join(os.path.dirname(__file__), "courseware_vendor")
    with open(os.path.join(vendor_dir, "jsxgraphcore.js"), "r", encoding="utf-8") as handle:
        jsxgraph_js = handle.read()
    with open(os.path.join(vendor_dir, "jsxgraph.css"), "r", encoding="utf-8") as handle:
        jsxgraph_css = handle.read()
    body = f'''<style>{jsxgraph_css}.quadratic-board{{display:grid;gap:12px}}#jxgbox{{width:100%;height:390px;border:1px solid #dbe6ed;border-radius:14px;background:#fff;touch-action:none;user-select:none;-webkit-user-select:none;box-shadow:0 8px 24px #23405b16}}.jxgbox svg{{border:0;box-shadow:none}}.quadratic-values{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.quadratic-values div{{padding:10px 12px;border:1px solid #e8dfcf;border-radius:10px;background:#fffaf3}}.quadratic-values b{{display:block;color:#92704a;font-size:12px}}.quadratic-values strong{{font-size:18px;color:#a66b32}}@media(max-width:640px){{.quadratic-values{{grid-template-columns:1fr 1fr}}.quadratic-values div:last-child{{grid-column:1/-1}}}}</style><p class="hint">观察抛物线的顶点 V 和对称轴；用参数条改变 a、h、k，长按顶点 V 后拖动。</p><p class="direct-tip">JSXGraph 提供坐标和绘图底层；长按顶点 V 后拖动，公式、顶点和图像都读取同一个状态。</p><div class="board quadratic-board"><div class="controls"><label>a（开口） <input id="a" type="range" min="-2" max="2" step="0.1" value="1"><output id="aValue">1.0</output></label><label>h（顶点横坐标） <input id="h" type="range" min="-4" max="4" step="0.1" value="0"><output id="hValue">0.0</output></label><label>k（顶点纵坐标） <input id="k" type="range" min="-4" max="4" step="0.1" value="0"><output id="kValue">0.0</output></label></div><div id="jxgbox" class="jxgbox" role="img" aria-label="二次函数坐标系和抛物线"></div><div class="quadratic-values"><div><b>顶点 V</b><strong id="vertexValue">(0.0, 0.0)</strong></div><div><b>对称轴</b><strong id="axisValue">x = 0.0</strong></div><div><b>开口</b><strong id="openingValue">向上</strong></div></div><p id="formula" class="formula"></p><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p><p class="footer-note">图形引擎：JSXGraph 1.10.1（MIT License；版权与许可证随项目分发）。</p></div>'''
    script = jsxgraph_js + '''\nconst $=id=>document.getElementById(id),aInput=$('a'),hInput=$('h'),kInput=$('k'),answer=$('answer');let state={a:1,h:0,k:0,revealed:false},dragReady=false,pressTimer=null;const box=$('jxgbox');box.onpointerdown=()=>{};box.onkeydown=()=>{};const board=JXG.JSXGraph.initBoard('jxgbox',{boundingbox:[-6,6,6,-6],axis:true,showCopyright:false,showNavigation:false,pan:{enabled:false},zoom:{enabled:false},keyboard:{enabled:true}});const graph=board.create('functiongraph',[x=>state.a*(x-state.h)*(x-state.h)+state.k,-6,6],{strokeColor:'#a66b32',strokeWidth:4,highlightStrokeColor:'#c78342'});const axis=board.create('line',[[()=>state.h,-6],[()=>state.h,6]],{straightFirst:false,straightLast:false,strokeColor:'#d6a36d',dash:2,strokeWidth:2,fixed:true});const vertex=board.create('point',[0,0],{name:'V',size:6,strokeColor:'#d66c62',fillColor:'#d66c62',highlightFillColor:'#e4877f',showInfobox:false,showLabel:true,withLabel:true,snapToGrid:true,snapSizeX:0.1,snapSizeY:0.1});vertex.on('down',()=>{clearTimeout(pressTimer);dragReady=false;pressTimer=setTimeout(()=>{dragReady=true;},350);});vertex.on('up',()=>{clearTimeout(pressTimer);pressTimer=null;dragReady=false;});vertex.on('drag',()=>{if(!dragReady){vertex.moveTo([state.h,state.k],0);board.update();return;}state.h=Math.max(-4,Math.min(4,vertex.X()));state.k=Math.max(-4,Math.min(4,vertex.Y()));syncInputs();draw();});function fmt(v){return Number(v).toFixed(1);}function syncInputs(){aInput.value=state.a;hInput.value=state.h;kInput.value=state.k;}function draw(){const a=Math.abs(state.a)<0.1?(state.a<0?-0.1:0.1):state.a;state.a=a;vertex.moveTo([state.h,state.k],0);board.update();$('aValue').textContent=fmt(state.a);$('hValue').textContent=fmt(state.h);$('kValue').textContent=fmt(state.k);$('vertexValue').textContent=`(${fmt(state.h)}, ${fmt(state.k)})`;$('axisValue').textContent=`x = ${fmt(state.h)}`;$('openingValue').textContent=state.a>0?'向上':'向下';$('formula').textContent=`y = ${fmt(state.a)}(x − ${fmt(state.h)})² ${state.k>=0?'+':'−'} ${fmt(Math.abs(state.k))}`;if(state.revealed)answer.textContent=`a ${state.a>0?'大于 0，开口向上':'小于 0，开口向下'}；顶点 V(${fmt(state.h)},${fmt(state.k)})，对称轴为 x=${fmt(state.h)}。`;}[['a',aInput],['h',hInput],['k',kInput]].forEach(([key,input])=>input.oninput=()=>{state[key]=+input.value;draw();});$('guess').onclick=()=>{answer.textContent='先猜一猜：只改变 h，抛物线的开口方向会不会改变？'};$('show').onclick=()=>{state.revealed=true;draw()};$('reset').onclick=()=>{state={a:1,h:0,k:0,revealed:false};syncInputs();answer.textContent='';draw()};draw();'''
    body += _exercise_html("y=−(x−2)²+3 的顶点、对称轴和开口方向是什么？", "顶点 (2,3)，对称轴 x=2；a=−1<0，所以开口向下。")
    return _shell(title, chapter, point, accent, body, script)


def _vectors_html(title, chapter, point, accent):
    body = '''<style>.vector-board{display:grid;gap:14px}.vector-svg{width:100%;max-height:390px;touch-action:none;user-select:none;-webkit-user-select:none;background:linear-gradient(145deg,#fcfdff,#f5f8fc)}.vector-grid{stroke:#d9e3ed;stroke-width:1}.vector-axis{stroke:#71869a;stroke-width:1.6}.vector-main{stroke:#d66c62;stroke-width:4;stroke-linecap:round}.vector-proj-x{stroke:#4d9f87;stroke-width:2.5;stroke-dasharray:7 5}.vector-proj-y{stroke:#2878c8;stroke-width:2.5;stroke-dasharray:7 5}.vector-handle{fill:#d66c62;stroke:#fff;stroke-width:3;cursor:grab;transition:transform .18s ease,filter .18s ease;transform-box:fill-box;transform-origin:center}.vector-hit{fill:transparent;stroke:transparent;cursor:grab;touch-action:none}.vector-handle.is-dragging{transform:scale(1.25);filter:drop-shadow(0 3px 5px #315f8c55)}.vector-values{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.vector-values div{padding:10px 12px;border:1px solid #dce7f3;border-radius:10px;background:#fbfdff}.vector-values b{display:block;color:#627789;font-size:12px}.vector-values strong{font-size:18px;color:#bd5d57}@media(max-width:640px){.vector-values{grid-template-columns:1fr 1fr}.vector-values div:last-child{grid-column:1/-1}}</style><p class="hint">观察从原点 O 出发的向量 OA；教师长按点 A 后拖动，坐标分量、投影和长度同步变化。</p><p class="direct-tip">红色是向量本身，绿色和蓝色虚线分别是 x、y 投影；正负号由箭头方向决定。</p><div class="board vector-board"><div class="controls"><label>x 分量 <input id="x" type="range" min="-5" max="5" step="0.1" value="4"><output id="xValue">4.0</output></label><label>y 分量 <input id="y" type="range" min="-5" max="5" step="0.1" value="3"><output id="yValue">3.0</output></label></div><svg id="vector" class="vector-svg" viewBox="0 0 520 360" role="img" aria-label="平面向量坐标与投影"></svg><div class="vector-values"><div><b>坐标</b><strong id="coordValue">(4.0, 3.0)</strong></div><div><b>长度</b><strong id="lengthValue">5.000</strong></div><div><b>方向</b><strong id="angleValue">36.9°</strong></div></div><p id="formula" class="formula"></p><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),svg=$('vector'),xInput=$('x'),yInput=$('y'),answer=$('answer');let x=4,y=3,revealed=false,dragging=false,pressTimer=null;const cx=260,cy=190,scale=28;function clamp(v,min,max){return Math.max(min,Math.min(max,v));}function p(e){const r=svg.getBoundingClientRect();return{x:(e.clientX-r.left)/r.width*520,y:(e.clientY-r.top)/r.height*360};}function draw(){const ax=cx+x*scale,ay=cy-y*scale;let grid='';for(let i=-5;i<=5;i++){grid+=`<line class="vector-grid" x1="${cx+i*scale}" y1="${cy-5*scale}" x2="${cx+i*scale}" y2="${cy+5*scale}"/>`;grid+=`<line class="vector-grid" x1="${cx-5*scale}" y1="${cy-i*scale}" x2="${cx+5*scale}" y2="${cy-i*scale}"/>`;}const ticks=[-5,-4,-3,-2,-1,1,2,3,4,5].map(n=>`<text x="${cx+n*scale-4}" y="${cy+17}" font-size="10" fill="#71869a">${n}</text><text x="${cx+8}" y="${cy-n*scale+4}" font-size="10" fill="#71869a">${n}</text>`).join('');svg.innerHTML=`<defs><marker id="vArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#d66c62"/></marker><marker id="xArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#71869a"/></marker></defs>${grid}<line class="vector-axis" x1="100" y1="${cy}" x2="420" y2="${cy}" marker-end="url(#xArrow)"/><line class="vector-axis" x1="${cx}" y1="330" x2="${cx}" y2="30" marker-end="url(#xArrow)"/>${ticks}<text x="${cx+8}" y="${cy+18}" fill="#536b7d" font-weight="700">O</text><text x="425" y="${cy-8}" fill="#536b7d" font-weight="700">x</text><text x="${cx+10}" y="28" fill="#536b7d" font-weight="700">y</text><line class="vector-proj-x" x1="${cx}" y1="${cy}" x2="${ax}" y2="${cy}"/><line class="vector-proj-y" x1="${ax}" y1="${cy}" x2="${ax}" y2="${ay}"/><line class="vector-main" x1="${cx}" y1="${cy}" x2="${ax}" y2="${ay}" marker-end="url(#vArrow)"/><circle class="vector-hit" cx="${ax}" cy="${ay}" r="24" tabindex="0" aria-label="长按拖动向量终点 A"/><circle class="vector-handle${dragging?' is-dragging':''}" cx="${ax}" cy="${ay}" r="8" tabindex="0" role="slider" aria-label="向量终点 A" aria-valuemin="-5" aria-valuemax="5" aria-valuenow="${x},${y}"/><text x="${ax+13}" y="${ay-13}" fill="#bd5d57" font-weight="700" font-size="15">A</text><text x="${(cx+ax)/2}" y="${cy+18}" fill="#4d9f87">x = ${x.toFixed(1)}</text><text x="${ax+10}" y="${(cy+ay)/2}" fill="#2878c8">y = ${y.toFixed(1)}</text>`;xInput.value=x;yInput.value=y;$('xValue').textContent=x.toFixed(1);$('yValue').textContent=y.toFixed(1);$('coordValue').textContent=`(${x.toFixed(1)}, ${y.toFixed(1)})`;$('lengthValue').textContent=Math.hypot(x,y).toFixed(3);$('angleValue').textContent=`${(Math.atan2(y,x)*180/Math.PI).toFixed(1)}°`;$('formula').textContent=`|a| = √(x²+y²) = √(${x.toFixed(1)}² + ${y.toFixed(1)}²) = ${Math.hypot(x,y).toFixed(3)}`;if(revealed)answer.textContent=`向量 a=(${x.toFixed(1)},${y.toFixed(1)}) 的两个坐标分量就是 x、y 投影，长度满足 |a|²=x²+y²。`;}function setPoint(e){const q=p(e);x=clamp(Math.round((q.x-cx)/scale*10)/10,-5,5);y=clamp(Math.round((cy-q.y)/scale*10)/10,-5,5);draw();}function begin(e){e.preventDefault();const q=p(e),ax=cx+x*scale,ay=cy-y*scale;if(!e.target.closest('.vector-hit,.vector-handle')&&Math.hypot(q.x-ax,q.y-ay)>=34)return;clearTimeout(pressTimer);pressTimer=setTimeout(()=>{dragging=true;svg.setPointerCapture?.(e.pointerId);setPoint(e);},350);}svg.onpointerdown=begin;svg.onpointermove=e=>{if(dragging)setPoint(e)};svg.onpointerup=svg.onpointercancel=()=>{clearTimeout(pressTimer);pressTimer=null;if(dragging){dragging=false;draw();}};svg.oncontextmenu=e=>e.preventDefault();svg.onkeydown=e=>{if(!e.target.matches('.vector-handle'))return;const step=e.shiftKey?0.5:0.1;if(['ArrowRight','ArrowLeft'].includes(e.key)){e.preventDefault();x=clamp(x+(e.key==='ArrowRight'?step:-step),-5,5);draw();}if(['ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();y=clamp(y+(e.key==='ArrowUp'?step:-step),-5,5);draw();}};xInput.oninput=()=>{x=+xInput.value;draw()};yInput.oninput=()=>{y=+yInput.value;draw()};$('guess').onclick=()=>{answer.textContent='先猜一猜：把 y 分量变成负数，向量会落在哪个象限？'};$('show').onclick=()=>{revealed=true;draw()};$('reset').onclick=()=>{x=4;y=3;revealed=false;answer.textContent='';draw()};draw();'''
    body += _exercise_html("向量 a=(3,−4) 的长度、x 投影和 y 投影分别是多少？", "长度为 5；x 投影为 3，y 投影为 −4。")
    return _shell(title, chapter, point, accent, body, script)


# 示例卡已下线（2026-09-14）：这 13 个知识点均由 v4 完整章节覆盖
# （courseware/catalog_v4.json），保留会与章节卡在课件库里重复出现。
# 历史样例文件与登记行已删除；如需恢复演示，可 git 取回文件后重新登记。
DEMO_COURSEWARE_BY_STAGE = {
    "primary": (),
    "junior": (),
    "senior": (),
}

# Compatibility alias for callers that only need a representative tuple.
DEMO_COURSEWARE = DEMO_COURSEWARE_BY_STAGE["primary"]


def demo_courseware_for_stage(stage: str):
    return DEMO_COURSEWARE_BY_STAGE.get(stage, DEMO_COURSEWARE)


def _statistics_html(title, chapter, point, accent):
    body = '''<p class="hint">观察四类数据的条形图；拖动滑块，比较总和与平均数怎样变化。</p><div class="board"><div class="controls"><label>A <input id="a" type="range" min="0" max="10" value="3"><output id="aValue">3</output></label><label>B <input id="b" type="range" min="0" max="10" value="5"><output id="bValue">5</output></label><label>C <input id="c" type="range" min="0" max="10" value="4"><output id="cValue">4</output></label><label>D <input id="d" type="range" min="0" max="10" value="6"><output id="dValue">6</output></label></div><svg id="stats" viewBox="0 0 520 290" role="img" aria-label="四类数据条形图"></svg><p id="summary" class="formula"></p><p id="formula" class="formula"></p><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),svg=$('stats'),answer=$('answer');let state={a:3,b:5,c:4,d:6,revealed:false};const labels=['A','B','C','D'];function draw(){const vals=labels.map(k=>state[k.toLowerCase()]),sum=vals.reduce((a,b)=>a+b,0),mean=sum/4;svg.innerHTML=vals.map((v,i)=>'<rect x="'+(70+i*105)+'" y="'+(230-v*18)+'" width="56" height="'+(v*18)+'" fill="'+(i===vals.indexOf(Math.max(...vals))?'#d66c62':'#6fbea2')+'"/><text x="'+(98+i*105)+'" y="252" text-anchor="middle">'+labels[i]+'</text><text x="'+(98+i*105)+'" y="'+(222-v*18)+'" text-anchor="middle">'+v+'</text>').join('');labels.forEach(k=>{$(k.toLowerCase()+'Value').textContent=state[k.toLowerCase()]});$('summary').textContent='总和 = '+sum+'；平均数 = '+mean.toFixed(2);$('formula').textContent='平均数 = ('+vals.join(' + ')+') ÷ 4 = '+mean.toFixed(2);if(state.revealed)answer.textContent='平均数是'+mean.toFixed(2)+'；一项数据增加 1，平均数增加 0.25。';}labels.forEach(k=>$(k.toLowerCase()).oninput=e=>{state[k.toLowerCase()]=+e.target.value;draw()});$('guess').onclick=()=>answer.textContent='先猜一猜：只把 A 增加 1，平均数会增加多少？';$('show').onclick=()=>{state.revealed=true;draw()};$('reset').onclick=()=>{state={a:3,b:5,c:4,d:6,revealed:false};draw();answer.textContent=''};draw();'''
    body += _exercise_html("数据 3、5、4、6 的平均数是多少？如果把 6 改成 10，平均数是多少？", "原平均数为 4.5；改为 10 后为 5.5。")
    return _shell(title, chapter, point, accent, body, script)


def _derivative_html(title, chapter, point, accent):
    body = '''<p class="hint">观察 y=x² 在 x₀ 处的割线；减小 h，割线斜率逼近切线斜率，也就是导数。</p><div class="board"><div class="controls"><label>x₀ <input id="x0" type="range" min="-3" max="3" step="0.1" value="1"><output id="x0Value">1.0</output></label><label>h（间隔） <input id="h" type="range" min="0.1" max="2" step="0.1" value="1"><output id="hValue">1.0</output></label></div><svg id="derivative" viewBox="0 0 520 300" role="img" aria-label="y等于x平方的割线与切线"></svg><p id="formula" class="formula"></p><button id="guess">先猜一下</button><button id="show">显示规律</button><button id="reset">重置</button><p id="answer" class="answer" aria-live="polite"></p></div>'''
    script = '''const $=id=>document.getElementById(id),svg=$('derivative'),answer=$('answer');let state={x0:1,h:1,revealed:false};const sx=x=>260+x*55,sy=y=>230-y*18;function draw(){const x0=state.x0,h=state.h,y0=x0*x0,x1=x0+h,y1=x1*x1,sec=(y1-y0)/h,der=2*x0;let curve='';for(let x=-4;x<=4;x+=0.1)curve+=(x===-4?'M':'L')+sx(x).toFixed(1)+' '+sy(x*x).toFixed(1)+' ';const ty1=y0+der*(-4-x0),ty2=y0+der*(4-x0);svg.innerHTML='<line x1="40" y1="'+sy(0)+'" x2="480" y2="'+sy(0)+'" stroke="#71869a"/><path d="'+curve+'" fill="none" stroke="#6f5aa8" stroke-width="4"/><line x1="'+sx(-4)+'" y1="'+sy(ty1)+'" x2="'+sx(4)+'" y2="'+sy(ty2)+'" stroke="#4b9278" stroke-width="3"/><line x1="'+sx(x0)+'" y1="'+sy(y0)+'" x2="'+sx(x1)+'" y2="'+sy(y1)+'" stroke="#d66c62" stroke-width="3" stroke-dasharray="8 5"/><circle cx="'+sx(x0)+'" cy="'+sy(y0)+'" r="7" fill="#d66c62"/>';$('x0Value').textContent=x0.toFixed(1);$('hValue').textContent=h.toFixed(1);$('formula').textContent='割线斜率 = '+sec.toFixed(2)+'；切线斜率 y′(x₀)=2x₀='+der.toFixed(2);if(state.revealed)answer.textContent='当 h='+h.toFixed(1)+' 时，割线与切线斜率相差 '+Math.abs(sec-der).toFixed(2)+'；h 越接近 0，差值越接近 0。';}$('x0').oninput=e=>{state.x0=+e.target.value;draw()};$('h').oninput=e=>{state.h=+e.target.value;draw()};$('guess').onclick=()=>answer.textContent='先猜一猜：把 h 减半，割线斜率会更接近哪一个数？';$('show').onclick=()=>{state.revealed=true;draw()};$('reset').onclick=()=>{state={x0:1,h:1,revealed:false};draw();answer.textContent=''};draw();'''
    body += _exercise_html("函数 f(x)=x² 在 x₀=2 处的导数是多少？", "f′(x)=2x，所以 f′(2)=4。")
    return _shell(title, chapter, point, accent, body, script)
