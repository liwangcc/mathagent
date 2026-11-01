"""数学表达式等价判断 — 符号规范化 + 变体生成"""

import re
from typing import Optional

from services.numeric_answers import parse_number


class MathMatcher:
    SYMBOL_MAP: dict[str, str] = {
        "²": "^2", "³": "^3", "⁴": "^4", "⁵": "^5", "⁶": "^6",
        "⁷": "^7", "⁸": "^8", "⁹": "^9", "⁰": "^0", "¹": "^1",
        "√": "sqrt", "π": "pi", "θ": "theta", "α": "alpha",
        "β": "beta", "γ": "gamma", "Δ": "delta", "∑": "sum",
        "×": "*", "÷": "/", "≤": "<=", "≥": ">=", "≠": "!=",
        "∞": "inf", "°": "deg",
    }

    @staticmethod
    def normalize(expr: str) -> str:
        if expr is None:
            return ""
        s = expr.strip()
        for full, repl in MathMatcher.SYMBOL_MAP.items():
            s = s.replace(full, repl)
        s = re.sub(r"\s+", "", s)
        s = s.replace("**", "^")
        s = s.replace("根号", "sqrt")
        s = re.sub(r"sqrt\^(\d+)", r"sqrt\1", s)  # sqrt^2 → sqrt2
        return s

    @staticmethod
    def generate_variants(expr: str) -> set[str]:
        """Generate common equivalent forms by commuting and expanding."""
        norm = MathMatcher.normalize(expr)
        variants = {norm}

        # Strip outer parens
        if norm.startswith("(") and norm.endswith(")"):
            variants.add(norm[1:-1])

        # Commutation for addition: a+b ↔ b+a
        for sep in ("+", "*"):
            if sep in norm and norm.count(sep) == 1:
                left, right = norm.split(sep, 1)
                variants.add(f"{right}{sep}{left}")

        # Fraction variants: a/b ↔ a÷b
        variants.add(norm.replace("/", "÷"))
        variants.add(norm.replace("÷", "/"))

        # Multiply implicit: ab ↔ a*b
        letters = re.findall(r"[a-zA-Z]+", norm)
        if len(letters) >= 2 and "*" not in norm:
            for i in range(len(letters) - 1):
                for j in range(i + 1, len(letters)):
                    if letters[i] + letters[j] in norm:
                        v = norm.replace(letters[i] + letters[j], f"{letters[i]}*{letters[j]}")
                        variants.add(v)

        # Sign variants
        variants.add(norm.replace("+-", "-").replace("-+", "-"))
        variants.add(norm.replace("--", "+"))

        return variants

    @classmethod
    def is_equivalent(
        cls,
        student_answer: str,
        correct_answer: str,
        variants: Optional[list[str]] = None,
    ) -> bool:
        # Numeric literals compare by exact value before whitespace is removed:
        # "25 2/5" is the mixed number 127/5 = 25.4, never "252/5".
        student_value = parse_number(student_answer)
        if student_value is not None and parse_number(correct_answer) is not None:
            return any(
                parse_number(candidate) == student_value
                for candidate in [correct_answer, *(variants or [])]
            )

        norm_student = cls.normalize(student_answer)
        norm_correct = cls.normalize(correct_answer)

        if norm_student == norm_correct:
            return True

        student_variants = cls.generate_variants(student_answer)
        correct_variants = cls.generate_variants(correct_answer)

        if student_variants & correct_variants:
            return True

        if variants:
            for v in variants:
                if cls.normalize(v) in student_variants:
                    return True

        return False
