"""Line-oriented source format for faithfully re-typeset exam papers.

A ``.paper.txt`` file is a transcription of one exam paper, checked against an
independent original.  It is parsed into a small document tree and rendered to
print HTML; KaTeX typesets the math in the browser (see ``render_paper.py``).

Syntax (one paragraph per line, blank lines ignored)::

    %key value                 metadata (paper_id, title, subject, meta, ...)
    ## 第一部分（选择题 共40分）  part heading (centred, bold)
    # 一、选择题：本题共8小题……  section heading, verbatim from the original
    1. 已知集合 $A=\\{x\\mid -5<x^3<5\\}$ ……（　　）
    A. $\\{-1,0\\}$              choice; must follow a question
    (1) 求 $B$；                 any other line is a paragraph of the question
    $$ x^2+y^2=1 $$            display math paragraph
    @fig q11-1 right 38        figure id, placement (right|center), width in mm
    | $x$ | 1 | 2 |            table row; consecutive rows form one table
    @answers-only ...          (reserved; answer keys use their own file)

Outside math, ``____`` (three or more underscores) is a fill-in blank,
``{{fig:id}}`` an inline figure (an icon inside a sentence) and ``\\$`` a
literal dollar sign.  Nothing is inferred or rewritten: the text of
every line is emitted exactly as transcribed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import re

QUESTION_RE = re.compile(r"^(\d{1,2}[A-Z]?)[.．]\s*(.*)$")
CHOICE_RE = re.compile(r"^([A-H])[.．]\s+(.*)$")
META_RE = re.compile(r"^%([a-z_]+)\s+(.*)$")
FIG_RE = re.compile(r"^@fig\s+([A-Za-z0-9_.-]+)(?:\s+(right|center))?(?:\s+(\d+(?:\.\d+)?))?\s*$")
BLANK_RE = re.compile(r"_{3,}")
INLINE_FIG_RE = re.compile(r"\{\{fig:([A-Za-z0-9_.-]+)\}\}")
REQUIRED_META = ("paper_id", "title", "subject")


class PaperFormatError(ValueError):
    pass


@dataclass
class Block:
    kind: str  # para | display | figure | table
    text: str = ""
    figure_id: str = ""
    placement: str = "center"
    width_mm: float = 0.0
    rows: list = field(default_factory=list)


@dataclass
class Question:
    number: str
    stem: str
    line: int
    choices: list = field(default_factory=list)  # [(label, text)]
    blocks: list = field(default_factory=list)


@dataclass
class Section:
    heading: str
    level: int  # 1 = section, 2 = part
    intro: list = field(default_factory=list)
    questions: list = field(default_factory=list)


@dataclass
class Paper:
    meta: dict
    sections: list

    def questions(self):
        for section in self.sections:
            yield from section.questions

    def figure_ids(self):
        ids = []
        for section in self.sections:
            texts = [section.heading]
            blocks = list(section.intro)
            for question in section.questions:
                texts.append(question.stem)
                texts.extend(text for _, text in question.choices)
                blocks.extend(question.blocks)
            for block in blocks:
                if block.kind == "figure":
                    ids.append(block.figure_id)
                texts.append(block.text)
                for row in block.rows:
                    texts.extend(row)
            for text in texts:
                ids.extend(INLINE_FIG_RE.findall(text or ""))
        return ids


def split_math(text: str):
    """Split a paragraph into ("text", s) and ("math", tex) segments.

    ``$$`` is not allowed inline; a lone ``$`` must be balanced.  ``\\$`` is a
    literal dollar sign outside math.
    """
    segments = []
    buf = []
    in_math = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "$":
            buf.append("\\$" if in_math else "$")
            i += 2
            continue
        if ch == "$":
            if text.startswith("$$", i):
                raise PaperFormatError("inline $$ is not allowed; put display math on its own line")
            segments.append(("math" if in_math else "text", "".join(buf)))
            buf = []
            in_math = not in_math
            i += 1
            continue
        buf.append(ch)
        i += 1
    if in_math:
        raise PaperFormatError("unbalanced $ in: " + text[:60])
    segments.append(("text", "".join(buf)))
    return [(kind, value) for kind, value in segments if value or kind == "math"]


def parse_paper(source: str) -> Paper:
    meta: dict = {}
    sections: list = []
    question = None
    table = None

    def current_section():
        if not sections:
            sections.append(Section(heading="", level=1))
        return sections[-1]

    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line:
            table = None
            continue
        if not line.startswith(("%", "@fig", "$$")):
            try:
                split_math(line)  # every text line: balanced $, no inline $$
            except PaperFormatError as exc:
                raise PaperFormatError(f"line {lineno}: {exc}") from exc
        if line.startswith("%"):
            match = META_RE.match(line)
            if not match:
                raise PaperFormatError(f"line {lineno}: malformed metadata")
            meta[match.group(1)] = match.group(2).strip()
            continue
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            target = question.blocks if question else current_section().intro
            if table is None:
                table = Block(kind="table")
                target.append(table)
            table.rows.append(cells)
            continue
        table = None
        if line.startswith("## "):
            sections.append(Section(heading=line[3:].strip(), level=2))
            question = None
            continue
        if line.startswith("# "):
            sections.append(Section(heading=line[2:].strip(), level=1))
            question = None
            continue
        if line.startswith("@fig"):
            match = FIG_RE.match(line)
            if not match:
                raise PaperFormatError(f"line {lineno}: malformed @fig")
            block = Block(kind="figure", figure_id=match.group(1),
                          placement=match.group(2) or "center",
                          width_mm=float(match.group(3) or 0))
            (question.blocks if question else current_section().intro).append(block)
            continue
        match = QUESTION_RE.match(line)
        if match:
            question = Question(number=match.group(1), stem=match.group(2), line=lineno)
            current_section().questions.append(question)
            continue
        match = CHOICE_RE.match(line)
        if match and question is not None:
            question.choices.append((match.group(1), match.group(2)))
            continue
        block_kind = "display" if line.startswith("$$") else "para"
        text = line
        if block_kind == "display":
            if not (line.endswith("$$") and len(line) > 4):
                raise PaperFormatError(f"line {lineno}: display math must be $$...$$ on one line")
            text = line[2:-2].strip()
        else:
            split_math(text)  # validate balance early with a line number
        block = Block(kind=block_kind, text=text)
        if question is not None:
            if question.choices:
                raise PaperFormatError(f"line {lineno}: paragraph after choices of question {question.number}")
            question.blocks.append(block)
        else:
            current_section().intro.append(block)
    missing = [key for key in REQUIRED_META if not meta.get(key)]
    if missing:
        raise PaperFormatError("missing metadata: " + ", ".join(missing))
    for q in (q for s in sections for q in s.questions):
        labels = [label for label, _ in q.choices]
        if labels and labels != [chr(ord("A") + i) for i in range(len(labels))]:
            raise PaperFormatError(f"question {q.number}: choices must be consecutive from A, got {labels}")
    try:
        split_math(meta.get("title", ""))
    except PaperFormatError as exc:
        raise PaperFormatError("title: " + str(exc)) from exc
    return Paper(meta=meta, sections=sections)


def question_numbers(paper: Paper) -> list:
    return [q.number for q in paper.questions()]


# ---------------------------------------------------------------- HTML output

def _inline_figure(figure_id: str, figures: dict) -> str:
    figure = figures.get(figure_id)
    if not figure:
        raise PaperFormatError(f"figure {figure_id} is not defined in figures.json")
    height = float(figure.get("height_mm") or 4)
    src = html.escape(figure["file"], quote=True)
    return f'<img class="ifig" src="{src}" alt="" style="height:{height}mm">'


TRAILING_PUNCT_RE = re.compile(r"^[，。．；：、！？）)\]】”’,.;:!?]+")


def _text_html(text: str, figures: dict) -> str:
    out = []
    for index, piece in enumerate(INLINE_FIG_RE.split(text)):
        if index % 2:
            out.append(_inline_figure(piece, figures))
            continue
        escaped = html.escape(piece)
        # A blank and the punctuation after it stay on one line.
        escaped = re.sub(r"_{3,}([，。．；：、！？）)]*)",
                         lambda m: f'<span class="nb"><span class="blank"></span>{m.group(1)}</span>', escaped)
        out.append(escaped)
    return "".join(out)


def _inline_html(text: str, figures: dict | None = None) -> str:
    figures = figures or {}
    out = []
    pending_math = None
    for kind, value in split_math(text):
        if kind == "math":
            if pending_math is not None:
                out.append(pending_math)
            pending_math = f'<span class="m" data-tex="{html.escape(value, quote=True)}"></span>'
            continue
        if pending_math is not None:
            # Chinese typesetting never starts a line with closing punctuation:
            # keep it attached to the formula it follows.
            match = TRAILING_PUNCT_RE.match(value)
            glue = match.group(0) if match else ""
            if glue:
                out.append(f'<span class="nb">{pending_math}{html.escape(glue)}</span>')
                value = value[len(glue):]
            else:
                out.append(pending_math)
            pending_math = None
        out.append(_text_html(value, figures))
    if pending_math is not None:
        out.append(pending_math)
    return "".join(out)


def _block_html(block: Block, figures: dict) -> str:
    if block.kind == "para":
        cls = "para"
        if re.match(r"^(\(\d+\)|（\d+）)", block.text):
            cls += " part"
        return f'<p class="{cls}">{_inline_html(block.text, figures)}</p>'
    if block.kind == "display":
        return f'<div class="dm" data-tex="{html.escape(block.text, quote=True)}"></div>'
    if block.kind == "table":
        rows = "".join(
            "<tr>" + "".join(f"<td>{_inline_html(cell, figures)}</td>" for cell in row) + "</tr>"
            for row in block.rows
        )
        return f'<table class="grid"><tbody>{rows}</tbody></table>'
    if block.kind == "figure":
        figure = figures.get(block.figure_id)
        if not figure:
            raise PaperFormatError(f"figure {block.figure_id} is not defined in figures.json")
        width = block.width_mm or float(figure.get("width_mm") or 40)
        src = html.escape(figure["file"], quote=True)
        return (f'<figure class="fig fig-{block.placement}" style="width:{width}mm">'
                f'<img src="{src}" alt=""></figure>')
    raise PaperFormatError("unknown block " + block.kind)


SHORT_ANSWER_MAX_SOURCE = 24  # e.g. "BCD", "$x=-\dfrac{3}{2}$"


def _is_short_answer(q: Question) -> bool:
    return not q.choices and not q.blocks and len(q.stem) <= SHORT_ANSWER_MAX_SOURCE


def paper_html(paper: Paper, *, figures: dict, katex_dir: str, font_stack: str | None = None) -> str:
    """Return a self-contained print HTML document (math typeset by KaTeX)."""
    title = paper.meta["title"]
    subject = paper.meta.get("subject", "数学")
    meta_line = paper.meta.get("meta", "")
    answer_key = subject.endswith("参考答案")
    parts = []
    for section in paper.sections:
        if section.heading:
            tag = "h2" if section.level == 1 else "h3"
            parts.append(f'<{tag} class="sec sec-{section.level}">{_inline_html(section.heading, figures)}</{tag}>')
        for block in section.intro:
            parts.append(_block_html(block, figures))
        grid = []  # answer keys: consecutive short answers share rows

        def flush_grid():
            if grid:
                parts.append(f'<div class="ans-grid">{"".join(grid)}</div>')
                grid.clear()

        for q in section.questions:
            if answer_key and _is_short_answer(q):
                grid.append(f'<section class="q ans-cell" data-q="{html.escape(q.number)}">'
                            f'<p class="stem"><span class="qno">{html.escape(q.number)}.</span>'
                            f'{_inline_html(q.stem, figures)}</p></section>')
                continue
            flush_grid()
            body = [f'<p class="stem"><span class="qno">{html.escape(q.number)}.</span>'
                    f'{_inline_html(q.stem, figures)}</p>']
            floats = [b for b in q.blocks if b.kind == "figure" and b.placement == "right"]
            # A right-floated figure is emitted before the paragraphs it sits beside.
            for block in floats:
                body.insert(0, _block_html(block, figures))
            for block in q.blocks:
                if block in floats:
                    continue
                body.append(_block_html(block, figures))
            if q.choices:
                items = "".join(
                    f'<div class="choice"><span class="clabel">{label}.</span>{_inline_html(text, figures)}</div>'
                    for label, text in q.choices
                )
                body.append(f'<div class="choices">{items}</div>')
            parts.append(f'<section class="q" data-q="{html.escape(q.number)}">{"".join(body)}'
                         '<div class="clear"></div></section>')
        flush_grid()
    fonts = font_stack or '"Times New Roman", "SimSun", "宋体", serif'
    katex_css = f"{katex_dir}/katex.min.css"
    katex_js = f"{katex_dir}/katex.min.js"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(re.sub(r'[$]', '', title))} {html.escape(subject)}</title>
<link rel="stylesheet" href="{html.escape(katex_css, quote=True)}">
<style>
@page {{ size: A4; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: {fonts}; font-size: 10.5pt; line-height: 1.75; color: #000; }}
.head {{ text-align: center; margin-bottom: 4mm; }}
.head .t {{ font-family: "SimHei", "黑体", sans-serif; font-size: 14pt; line-height: 1.5; }}
.head .s {{ font-family: "SimHei", "黑体", sans-serif; font-size: 20pt; letter-spacing: 0.6em; margin: 1mm 0 1mm 0.6em; }}
.head .meta {{ font-size: 10.5pt; }}
h2.sec, h3.sec {{ font-size: 10.5pt; margin: 3mm 0 1.5mm; line-height: 1.75; font-weight: normal; }}
h2.sec {{ font-family: "SimHei", "黑体", sans-serif; }}
h3.sec {{ font-family: "SimHei", "黑体", sans-serif; text-align: center; }}
section.q {{ margin: 0 0 0.5mm; padding-top: 1.5mm; break-inside: avoid-page; }}
/* Padding, unlike margin, survives a page break: a tall formula on a question's
   first line must not overflow into the previous page's bottom margin. */
p {{ margin: 0; }}
p.stem {{ padding-left: 1.5em; text-indent: -1.5em; }}
p.stem .qno {{ display: inline-block; min-width: 1.5em; text-indent: 0; }}
p.para {{ padding-left: 1.5em; }}
p.para.part {{ padding-left: 3.4em; text-indent: -1.9em; }}
.dm {{ margin: 1mm 0 1mm 1.5em; text-align: center; }}
.choices {{ display: flex; flex-wrap: wrap; padding-left: 1.5em; }}
.choice {{ box-sizing: border-box; padding-right: 0.8em; }}
.choice .clabel {{ display: inline-block; min-width: 1.6em; }}
.cols-4 .choice {{ width: 25%; }}
.cols-2 .choice {{ width: 50%; }}
.cols-1 .choice {{ width: 100%; }}
.blank {{ display: inline-block; width: 4em; border-bottom: 0.6pt solid #000; height: 1em; vertical-align: -0.15em; margin: 0 0.15em; }}
figure.fig {{ margin: 1mm 0; }}
figure.fig img {{ width: 100%; height: auto; display: block; }}
figure.fig-center {{ margin-left: auto; margin-right: auto; }}
figure.fig-right {{ float: right; margin: 0 0 1mm 4mm; }}
.clear {{ clear: both; }}
img.ifig {{ display: inline-block; vertical-align: -0.2em; margin: 0 0.15em; }}
.nb {{ white-space: nowrap; }}
table.grid {{ border-collapse: collapse; margin: 1mm auto; }}
table.grid td {{ border: 0.6pt solid #000; padding: 0.5mm 2.5mm; text-align: center; }}
.katex {{ font-size: 1.05em; }}
/* Lines holding \\dfrac grow to exactly the fraction's height, so two such lines
   touch; a little padding gives them leading without changing ordinary lines. */
.katex .base {{ padding: 0.15em 0; }}
.ans-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); column-gap: 4mm; align-items: baseline; }}
.ans-grid section.q.ans-cell {{ padding-top: 1mm; }}
</style></head>
<body>
<div class="head"><div class="t">{_inline_html(title)}</div><div class="s">{html.escape(subject)}</div>
{f'<div class="meta">{_inline_html(meta_line)}</div>' if meta_line else ''}</div>
{''.join(parts)}
<script src="{html.escape(katex_js, quote=True)}"></script>
<script>
window.__renderErrors = [];
document.querySelectorAll('[data-tex]').forEach(function (el) {{
  var display = el.classList.contains('dm');
  try {{
    katex.render(el.getAttribute('data-tex'), el, {{ displayMode: display, throwOnError: true, strict: 'error', output: 'html' }});
  }} catch (err) {{
    window.__renderErrors.push(String(err && err.message || err) + ' :: ' + el.getAttribute('data-tex'));
  }}
}});
// Lay out options like the printed paper: 4 per row, else 2, else 1.  An
// option "fits" only if its whole text stays on one line in that column.
// Measure only after fonts and floated figures exist: both change widths.
function layoutChoices() {{
  document.querySelectorAll('.choices').forEach(function (box) {{
    var items = Array.from(box.querySelectorAll('.choice'));
    function fits(n) {{
      box.className = 'choices cols-' + n;
      return items.every(function (it) {{
        it.style.whiteSpace = 'nowrap';
        var ok = it.scrollWidth <= it.clientWidth + 0.5;
        it.style.whiteSpace = '';
        return ok;
      }});
    }}
    if (!fits(4)) {{ if (!fits(2)) {{ box.className = 'choices cols-1'; }} }}
  }});
}}
var images = Array.from(document.images).map(function (img) {{
  return img.complete ? Promise.resolve() : new Promise(function (done) {{ img.onload = img.onerror = done; }});
}});
Promise.all([document.fonts.ready].concat(images)).then(function () {{
  layoutChoices();
  window.__layoutDone = true;
}});
</script>
</body></html>
"""
