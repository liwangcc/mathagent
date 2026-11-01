"""Transcription format used to re-typeset verified past papers (tooling, pure Python)."""
from __future__ import annotations

import pytest

from scripts.past_papers import crosscheck
from scripts.past_papers.paper_format import PaperFormatError, paper_html, parse_paper, question_numbers

SAMPLE = r"""%paper_id gk2024-xgk1
%title 2024年普通高等学校招生全国统一考试（新课标Ⅰ卷）
%subject 数学
%meta 本试卷共19小题，满分150分．

# 一、选择题：本题共8小题，每小题5分，共40分．

1. 已知集合 $A=\{x\mid -5<x^3<5\}$，$B=\{-3,-1,0,2,3\}$，则 $A\cap B=$（　　）
A. $\{-1,0\}$
B. $\{2,3\}$
C. $\{-3,-1,0\}$
D. $\{-1,0,2\}$

11. 造型{{fig:q11-icon}}可以做成美丽的丝带，则（　　）
@fig q11-fig right 40
A. $a=-2$
B. 点 $(2\sqrt{2},0)$ 在 $C$ 上

# 三、填空题：本题共3小题，每小题5分，共15分．

13. 则 $a=$____．

# 四、解答题：本题共5小题，共77分．

15. （13分）记 $\triangle ABC$ 的内角 $A$，$B$，$C$ 的对边分别为 $a$，$b$，$c$．
(1) 求 $B$；
| $x$ | 1 | 2 |
$$ a^2+b^2=c^2 $$
"""
FIGURES = {"q11-icon": {"file": "figures/q11-icon.svg", "height_mm": 4},
           "q11-fig": {"file": "figures/q11-fig.svg", "width_mm": 40}}


def test_parses_sections_questions_choices_and_figures():
    paper = parse_paper(SAMPLE)
    assert paper.meta["paper_id"] == "gk2024-xgk1"
    assert [s.heading[:5] for s in paper.sections] == ["一、选择题", "三、填空题", "四、解答题"]
    assert question_numbers(paper) == ["1", "11", "13", "15"]
    q1 = paper.sections[0].questions[0]
    assert [label for label, _ in q1.choices] == ["A", "B", "C", "D"]
    assert paper.figure_ids() == ["q11-fig", "q11-icon"] or sorted(paper.figure_ids()) == ["q11-fig", "q11-icon"]
    q15 = paper.sections[2].questions[0]
    assert [b.kind for b in q15.blocks] == ["para", "table", "display"]
    assert q15.blocks[1].rows == [["$x$", "1", "2"]]


@pytest.mark.parametrize("source, message", [
    ("%title t\n%subject s\n1. x", "missing metadata"),
    ("%paper_id p\n%title t\n%subject s\n1. 若 $x>0，则", "unbalanced"),
    ("%paper_id p\n%title t\n%subject s\n1. 若 $$x$$ 则", "inline $$"),
    ("%paper_id p\n%title t\n%subject s\n1. 题\nA. 1\nC. 2", "consecutive"),
    ("%paper_id p\n%title t\n%subject s\n1. 题\nA. 1\n补充段落", "paragraph after choices"),
    ("%paper_id p\n%title t\n%subject s\n@fig bad id here", "malformed @fig"),
])
def test_rejects_ambiguous_or_broken_transcriptions(source, message):
    with pytest.raises(PaperFormatError, match=message.replace("$", r"\$")):
        parse_paper(source)


def test_html_escapes_text_and_keeps_tex_for_katex():
    source = "%paper_id p\n%title t\n%subject 数学\n1. 文本<script>alert(1)</script> 与 $x<y$，结束\n"
    html = paper_html(parse_paper(source), figures={}, katex_dir="file:///katex")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert 'data-tex="x&lt;y"' in html
    # A formula and the punctuation after it never split across lines.
    assert '<span class="nb"><span class="m" data-tex="x&lt;y"></span>，</span>' in html


ANSWER_KEY = r"""%paper_id gk2021-xgk1
%title 2021年普通高等学校招生全国统一考试（新高考Ⅰ卷）
%subject 数学参考答案

# 一、选择题

1. B
2. C

# 四、解答题

17. (1) $b_1=2$，$b_2=5$，$b_n=3n-1$；(2) $300$
18. CD
"""


def test_answer_keys_pack_short_answers_into_rows_and_keep_long_ones_whole():
    html_text = paper_html(parse_paper(ANSWER_KEY), figures={}, katex_dir="file:///katex")
    assert html_text.count('<div class="ans-grid">') == 2  # one run per section, interrupted by Q17
    first_grid = html_text.split('<div class="ans-grid">')[1]
    assert 'data-q="1"' in first_grid and 'data-q="2"' in first_grid
    assert '<section class="q" data-q="17">' in html_text  # long answer keeps its own row
    # Student papers never use the answer grid, even for one-letter stems.
    student = ANSWER_KEY.replace("%subject 数学参考答案", "%subject 数学")
    assert "ans-grid\">" not in paper_html(parse_paper(student), figures={}, katex_dir="file:///katex")


def test_undefined_figure_fails_the_render():
    source = "%paper_id p\n%title t\n%subject 数学\n1. 如图\n@fig q1 center 30\n"
    with pytest.raises(PaperFormatError, match="figures.json"):
        paper_html(parse_paper(source), figures={}, katex_dir="file:///katex")
    html = paper_html(parse_paper(SAMPLE), figures=FIGURES, katex_dir="file:///katex")
    assert 'src="figures/q11-icon.svg"' in html and 'class="fig fig-right"' in html


def test_crosscheck_counts_reveal_changed_words_and_numbers():
    ours = crosscheck.features(crosscheck.latex_plain(r"(1) 求 $C$ 的离心率；$\dfrac{3}{2}$"))
    theirs = crosscheck.features("(1) 求 C 的率心率; 3 2")
    diff = crosscheck.diff(ours["cjk"], theirs["cjk"])
    assert diff == {"only_in_other": "率", "only_in_ours": "离"}
    assert not any(crosscheck.diff(ours["digits"], theirs["digits"]).values())
    # LaTeX command names are not letters of the problem text.
    assert crosscheck.features(crosscheck.latex_plain(r"$\sqrt{3}\cdot\boldsymbol{a}$"))["letters"] == {"a": 1}
