"""公共工具函数 — 文本规范化等"""

import re


def normalize_text(text: str) -> str:
    """统一的文本规范化函数：去空白、全角转半角、转小写。

    各 matcher 共用此函数，避免重复定义导致行为不一致。
    """
    if not text:
        return ""
    # 全角字母/数字 → 半角
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(' ')
        else:
            result.append(ch)
    s = ''.join(result)
    # 去除所有空白字符，转小写
    s = re.sub(r'\s+', '', s)
    return s.lower()
