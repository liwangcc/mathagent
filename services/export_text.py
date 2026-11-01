"""Shared text cleanup for teacher-facing exports.

Model and OCR output can contain HTML entities, LaTeX delimiters, or common
LaTeX commands. Exporters should preserve the mathematical meaning while
producing readable plain text when a full equation renderer is unavailable.
"""

from __future__ import annotations

import html
import re
from typing import Any


_COMMAND_REPLACEMENTS = (
    (r"\times", "×"),
    (r"\cdot", "·"),
    (r"\div", "÷"),
    (r"\pm", "±"),
    (r"\mp", "∓"),
    (r"\neq", "≠"),
    (r"\ne", "≠"),
    (r"\geq", "≥"),
    (r"\leq", "≤"),
    (r"\ge", "≥"),
    (r"\le", "≤"),
    (r"\approx", "≈"),
    (r"\equiv", "≡"),
    (r"\propto", "∝"),
    (r"\infty", "∞"),
    (r"\notin", "∉"),
    (r"\in", "∈"),
    (r"\subseteq", "⊆"),
    (r"\supseteq", "⊇"),
    (r"\subset", "⊂"),
    (r"\supset", "⊃"),
    (r"\cup", "∪"),
    (r"\cap", "∩"),
    (r"\forall", "∀"),
    (r"\exists", "∃"),
    (r"\therefore", "∴"),
    (r"\because", "∵"),
    (r"\perp", "⊥"),
    (r"\parallel", "∥"),
    (r"\angle", "∠"),
    (r"\rightarrow", "→"),
    (r"\Rightarrow", "⇒"),
    (r"\leftrightarrow", "↔"),
    (r"\pi", "π"),
    (r"\alpha", "α"),
    (r"\beta", "β"),
    (r"\gamma", "γ"),
    (r"\theta", "θ"),
    (r"\lambda", "λ"),
    (r"\mu", "μ"),
    (r"\sigma", "σ"),
    (r"\Delta", "Δ"),
)

_SUPERSCRIPT_MAP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUBSCRIPT_MAP = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")

_BROKEN_JSON_LATEX_ESCAPES = (
    ("\x08egin", r"\begin"),
    ("\x08eta", r"\beta"),
    ("\x08inom", r"\binom"),
    ("\x0crac", r"\frac"),
    ("\x0dight", r"\right"),
    ("\x0dho", r"\rho"),
    ("\x09heta", r"\theta"),
    ("\x09imes", r"\times"),
    ("\x09ext", r"\text"),
    ("\x09an", r"\tan"),
    ("\x0aeq", r"\neq"),
    ("\x0aotin", r"\notin"),
)


def _repair_latex_source(text: str) -> str:
    """Repair safe, unambiguous notation damage before any renderer sees it."""
    for invisible in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(invisible, "")
    text = re.sub(r"[＼﹨](?=[A-Za-z])", lambda _match: "\\", text)
    for damaged, command in _BROKEN_JSON_LATEX_ESCAPES:
        text = text.replace(damaged, command)
    # Some OCR/model outputs emit an optional radical degree with no content.
    # KaTeX tolerates it, but Word/WPS exposes it as a dotted square placeholder.
    return re.sub(r"\\sqrt\s*\[\s*\]", lambda _match: r"\sqrt", text)


def decode_export_text(value: Any) -> str:
    """Decode up to three layers of entities without looping indefinitely."""
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return _repair_latex_source(text)


def _replace_nth_root(match: re.Match[str]) -> str:
    degree = match.group(1).strip()
    radicand = match.group(2)
    display_degree = degree.translate(_SUPERSCRIPT_MAP) if all(
        ord(character) in _SUPERSCRIPT_MAP for character in degree
    ) else f"[{degree}]"
    return f"{display_degree}√({radicand})"


def _replace_braced_commands(value: str) -> str:
    wrappers = r"(?:text|mathrm|mathbf|mathit|operatorname|overline|underline)"
    for _ in range(8):
        previous = value
        value = re.sub(rf"\\{wrappers}\s*\{{([^{{}}]*)\}}", r"\1", value)
        value = re.sub(
            r"\\sqrt\s*\[([^\[\]]+)\]\s*\{([^{}]*)\}",
            _replace_nth_root,
            value,
        )
        value = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", value)
        value = re.sub(
            r"\\(?:dfrac|tfrac|frac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
            r"(\1)/(\2)",
            value,
        )
        value = re.sub(r"\\binom\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"C(\1, \2)", value)
        if value == previous:
            break
    return value


def _replace_script(match: re.Match[str], *, superscript: bool) -> str:
    content = match.group(1)
    mapping = _SUPERSCRIPT_MAP if superscript else _SUBSCRIPT_MAP
    if all(ord(character) in mapping for character in content):
        return content.translate(mapping)
    marker = "^" if superscript else "_"
    return f"{marker}({content})"


def normalize_export_text(value: Any) -> str:
    """Return readable, non-evaluated text suitable for PDF, HTML, and DOCX."""
    text = decode_export_text(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\\begin\s*\{(?:aligned|array|cases|matrix|pmatrix|bmatrix)\}", "", text)
    text = re.sub(r"\\end\s*\{(?:aligned|array|cases|matrix|pmatrix|bmatrix)\}", "", text)
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = text.replace(r"\lvert", "|").replace(r"\rvert", "|")
    text = text.replace(r"\langle", "<").replace(r"\rangle", ">")
    text = _replace_braced_commands(text)

    for command, replacement in _COMMAND_REPLACEMENTS:
        text = re.sub(re.escape(command) + r"(?![A-Za-z])", replacement, text)

    text = re.sub(r"\\(sin|cos|tan|cot|sec|csc|log|ln|lim|max|min|gcd)(?![A-Za-z])", r"\1", text)
    text = re.sub(r"\^\{([^{}]+)\}", lambda match: _replace_script(match, superscript=True), text)
    text = re.sub(r"_\{([^{}]+)\}", lambda match: _replace_script(match, superscript=False), text)
    text = re.sub(
        r"\^([0-9+\-=()n])",
        lambda match: match.group(1).translate(_SUPERSCRIPT_MAP),
        text,
    )
    text = re.sub(
        r"_([0-9+\-=()])",
        lambda match: match.group(1).translate(_SUBSCRIPT_MAP),
        text,
    )

    text = text.replace(r"\(", "").replace(r"\)", "")
    text = text.replace(r"\[", "").replace(r"\]", "")
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\(?:qquad|quad|,|;|:|!)(?![A-Za-z])", " ", text)
    text = re.sub(r"\\\\(?:\[[^\]]*\])?", "\n", text)
    text = re.sub(r"\s*&\s*(?=[=<>≤≥])", " ", text)
    text = text.replace("~", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def escape_export_html(value: Any) -> str:
    """Normalize export text, HTML-escape it, and retain intentional lines."""
    escaped = html.escape(normalize_export_text(value), quote=True)
    return escaped.replace("\n", "<br>")
