"""从上传文件名中解析学生身份线索。

场景：老师用收集类小工具收集作业，被收集者自己填姓名/学号，工具再按
"姓名学号 + 自己的随机后缀"命名文件，例如 ``马飞2021000008_1eax24.jpg``。
文件名里的姓名学号是学生**打字填写**的，比手写 OCR 更干净，可用来锚定身份。

设计要点
--------
1. **不做"先剥后缀再解析"**：噪声后缀与真实学号在*位置*上无法区分
   （``张三_2021123456`` 的尾段就是学号本身），只能靠*形状*区分。
   因此改为：整名分词 → 给每个候选打分 → 取最高分。
2. **数字占比是判据**：收集工具的随机后缀把字母穿插在数字里（``1eax24``
   占比 3/6），真实学号是纯数字或数字主导（``2021A12345`` 占比 9/10）。
3. **少于 6 位的数字一律不当学号**：文件名里的 ``_1`` ``_2`` 绝大多数是页码
   或序号。宁可不给，也不猜——这样多页同学的文件名反而能靠同一个学号归到
   一起（页码被忽略）。
4. 本模块只*解析*，不判定归属。是否落到某个学生由
   [services.student_service.resolve_student_identity] 按班级名单收窄 +
   唯一性闸门决定；姓名与学号互相矛盾时它会返回 needs_confirmation。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# CJK 统一表意文字基本区。学生姓名用字都在此范围内。
_CJK = "一-鿿"

# 前端预处理会把文件改名为 "<原名>.ocr-ready-v1.jpg"（见
# frontend/src/utils/uploadValidation.js），原名整体保留，故先摘掉这层再摘扩展名。
_OCR_READY_RE = re.compile(r"\.ocr-ready-v1\.jpg$", re.IGNORECASE)
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")

# 分词：连续汉字算一个词，连续字母数字算一个词，其余字符（-_ 空格等）都是分隔符。
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[A-Za-z0-9]+")

# 学号的位数下限/上限。低于下限不猜（见设计要点 3）。
# 上限 12 与 services/student_service.py 的"入学年前缀 + 班内座号"口径一致：
# 再长的连续数字是时间戳或哈希，不是学号。
_MIN_NO_DIGITS = 6
_MAX_NO_DIGITS = 12

# 数字主导阈值：区分 "2021A12345"(0.9) 与 "1eax24"(0.5)。
_DIGIT_DOMINANT_RATIO = 0.7

# 姓名长度：中文姓名 2-4 字。
_MIN_NAME_CHARS = 2
_MAX_NAME_CHARS = 4

# 学科/作业类词汇不是姓名。收集工具常把它们拼进文件名
# （如 "数学作业_马飞2021000008"）。命中后从汉字词里剔除再取姓名。
_NAME_STOPWORDS = frozenset({
    "作业", "试卷", "答卷", "答题", "答题卡", "练习", "测试", "测验", "考试",
    "月考", "周测", "期中", "期末", "单元", "随堂", "课后", "预习", "复习",
    "数学", "语文", "英语", "物理", "化学", "生物", "政治", "历史", "地理",
    "年级", "班级", "初一", "初二", "初三", "高一", "高二", "高三",
    "上册", "下册", "第一", "第二", "第三", "第四", "第五", "上传", "提交",
    "图片", "照片", "扫描", "副本", "正面", "反面", "第一页", "第二页",
    "未命名", "新建", "微信", "图像", "文档",
})

# 虚词/代词用字。中文姓名里不会出现这些字，一旦片段含有就不是姓名。
# 用途：剔除学科词后可能剩下语法碎片（"我的作业" 去掉 "作业" 剩 "我的"），
# 这种假姓名会与 OCR 正确读到的姓名撞成 conflict，反而逼老师多做一次确认，
# 比没有文件名线索更糟，故必须拒掉。
_NON_NAME_CHARS = frozenset("的了是在和与及或我你他她它们这那些")


@dataclass(frozen=True)
class FilenameIdentity:
    """一个文件名解析出的身份线索。``name`` / ``student_no`` 可能为空串。"""

    name: str
    student_no: str
    confidence: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class _Token:
    text: str
    is_cjk: bool


def _strip_suffixes(basename: str) -> str:
    """摘掉 OCR-ready 包装层和最后一个扩展名。"""
    stem = _OCR_READY_RE.sub("", basename)
    return _EXTENSION_RE.sub("", stem)


def _basename(filename: str) -> str:
    """取纯文件名，兼容 POSIX 与 Windows 分隔符（zip 内相对路径可能混用）。"""
    return filename.replace("\\", "/").rsplit("/", 1)[-1]


def _tokenize(stem: str) -> list[_Token]:
    return [
        _Token(text=match.group(0), is_cjk=bool(re.match(rf"[{_CJK}]", match.group(0))))
        for match in _TOKEN_RE.finditer(stem)
    ]


def _digits_of(text: str) -> str:
    return re.sub(r"\D", "", text)


def _looks_like_year(digits: str) -> bool:
    """恰好 4 位且落在 1900-2099：更可能是年份而非学号。"""
    return len(digits) == 4 and 1900 <= int(digits) <= 2099


def _looks_like_date(digits: str) -> bool:
    """8 位 YYYYMMDD：收集工具常把收集日期拼进文件名。"""
    if len(digits) != 8:
        return False
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
    return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31


def _looks_like_timestamp(digits: str) -> bool:
    """9 位以上且以合法 YYYYMMDD 开头：微信/收集工具的时间戳文件名。

    ``微信图片_20240315103022`` 必须整体判为噪声。学号最长 12 位，与 8 位日期
    前缀撞长度的区间只有 9-12 位，故此处只看日期前缀、不看总长。
    """
    return len(digits) > 8 and _looks_like_date(digits[:8])


def _name_pieces(token_text: str) -> list[str]:
    """从一段连续汉字里剔除学科/作业类词汇，返回剩下的 2-4 字片段。

    ``数学作业马飞`` → 去掉 ``数学`` ``作业`` → ``马飞``。
    剔除停用词后剩下的残渣未必是姓名（``我的作业`` → ``我的``），故再过一道
    虚词闸门，见 [_NON_NAME_CHARS]。
    """
    remaining = token_text
    for stopword in _NAME_STOPWORDS:
        if stopword in remaining:
            remaining = remaining.replace(stopword, "\x00")
    return [
        piece for piece in remaining.split("\x00")
        if _MIN_NAME_CHARS <= len(piece) <= _MAX_NAME_CHARS
        and not (_NON_NAME_CHARS & set(piece))
    ]


def _score_number_candidate(
    token: _Token,
    index: int,
    tokens: list[_Token],
    digits: str,
) -> int:
    """给一个数字候选打分。分数只用于同一文件名内部比较，不跨文件比较。"""
    # 位数上限由 [_MAX_NO_DIGITS] 在候选闸门处保证，此处只分档打分。
    score = 60 if len(digits) in range(8, _MAX_NO_DIGITS + 1) else 40

    # 紧邻汉字姓名（如 "马飞2021000008"、"张三_2021123456"）是很强的正信号。
    neighbours = [tokens[i] for i in (index - 1, index + 1) if 0 <= i < len(tokens)]
    if any(neighbour.is_cjk and _name_pieces(neighbour.text) for neighbour in neighbours):
        score += 25

    if _looks_like_date(digits):
        score -= 40
    elif _looks_like_year(digits):
        score -= 30

    if digits != token.text:
        score -= 5          # 含字母（2021A12345）仍可用，但纯数字更可信

    return score


def parse_filename_identity(filename: str | None) -> FilenameIdentity | None:
    """解析一个文件名的身份线索；解析不出任何线索时返回 None。

    >>> parse_filename_identity("马飞2021000008_1eax24.jpg")
    FilenameIdentity(name='马飞', student_no='2021000008', confidence='high')
    >>> parse_filename_identity("张三_2021123456.png").student_no
    '2021123456'
    >>> parse_filename_identity("IMG_0421.jpg") is None
    True
    """
    if not filename or not isinstance(filename, str):
        return None

    stem = _strip_suffixes(_basename(filename.strip()))
    if not stem:
        return None

    tokens = _tokenize(stem)

    best_no = ""
    best_score = 0
    best_is_pure = False
    for index, token in enumerate(tokens):
        if token.is_cjk:
            continue
        digits = _digits_of(token.text)
        if not (_MIN_NO_DIGITS <= len(digits) <= _MAX_NO_DIGITS):
            continue
        # 时间戳（微信图片_20240315103022）与学号撞长度，必须整体排除。
        if _looks_like_timestamp(digits):
            continue
        # 数字占比过低说明是随机后缀（1eax24 → 0.5），不是学号。
        if len(digits) / len(token.text) < _DIGIT_DOMINANT_RATIO:
            continue
        score = _score_number_candidate(token, index, tokens, digits)
        if score > best_score:
            best_no = token.text
            best_score = score
            best_is_pure = digits == token.text

    # 姓名：优先取紧邻学号的那段汉字，否则取第一段合法汉字。
    name_candidates: list[tuple[int, str]] = []
    for index, token in enumerate(tokens):
        if not token.is_cjk:
            continue
        for piece in _name_pieces(token.text):
            name_candidates.append((index, piece))

    name = ""
    if name_candidates:
        no_index = next(
            (i for i, token in enumerate(tokens) if not token.is_cjk and token.text == best_no),
            None,
        )
        if no_index is not None:
            adjacent = [
                piece for index, piece in name_candidates
                if abs(index - no_index) == 1
            ]
            if adjacent:
                name = adjacent[0]
        if not name:
            name = name_candidates[0][1]

    if not name and not best_no:
        return None

    return FilenameIdentity(
        name=name,
        student_no=best_no,
        confidence=_confidence_of(name, best_no, best_score, best_is_pure),
    )


def _confidence_of(name: str, student_no: str, score: int, pure_digits: bool) -> str:
    """姓名+学号互相印证时最高——它让下游能做一次免费的交叉校验。"""
    digits = _digits_of(student_no)
    if name and student_no and score >= 80 and pure_digits and len(digits) in range(8, 13):
        return "high"
    if name and student_no:
        return "medium"
    if student_no and len(digits) in range(8, 13):
        return "medium"
    return "low"
