"""课程代码提取与归一化：让不同院校的课程代码都能对上本地目录。

背景：早期实现把课程代码写死成 ``CS`` + 4~5 位数字（一家院校的写法），换学校
就匹配不上本地课程目录，同步时每次都新建一个重复文件夹。这里改成通用规则。

**课程代码字段**（``course_code``）按优先级：

1. 去掉开头的学期数字前缀后，整体形如「字母 + 3~5 位数字」就采用
   （``202609CS5187`` -> ``CS5187``、``MATH 101`` -> ``MATH101``、``ECON2201``）。
2. 否则在文本里找「字母(1~8) + 可选分隔 + 数字(3~5)」的片段，但**不允许它
   出现在下划线分段内部**——``SD_GEN_SFTY_CS_MCS_2026`` 这种多段码不拆开。
3. 都没有 -> 回落：``course_code`` -> 课程名 -> 课程 id，截断 20 字符。

**课程名**只认**开头**的片段（``CS5187 Vision and Image`` -> ``CS5187``），
避免把名字中间的「Algorithms 2024」当成课程号。

**可选自定义正则**：配置项 ``course_code_pattern``，给命名古怪的院校兜底
（取第一个捕获组，没有捕获组则取整个匹配）。
"""

from __future__ import annotations

import re

from .paths import cfg_str

# 「字母 + 数字」片段。字母最多 8 个；两侧必须是**完整词**边界——否则
# 「Algorithms 2024」会被截成「GORITHMS2024」这种半截词。
_FRAG_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,8})\s*[-_]?\s*(\d{3,5})(?![0-9])")
# 开头锚定的同一形态：用于课程名
_HEAD_RE = re.compile(r"^([A-Za-z]{1,8})\s*[-_]?\s*(\d{3,5})(?![0-9])")
# 整体形态：字母 + 数字
_WHOLE_RE = re.compile(r"^[A-Za-z]{1,8}\d{3,5}$")
# 开头的学期数字前缀：202609 / 2026-27 / 202509 等
_TERM_PREFIX_RE = re.compile(r"^\d{4,8}[-_]?\s*")
# 含下划线的多段码（SD_GEN_SFTY_CS_MCS_2026）：整体不拆开当课程号
_MULTISEG_RE = re.compile(r"^[A-Za-z0-9]+(_[A-Za-z0-9-]+)+$")
# 比较用的分隔符（空格 / 连字符 / 下划线）
_SEP_RE = re.compile(r"[\s\-_]+")


def normalize_key(text: str) -> str:
    """比较用的归一化：大写 + 去掉空格 / 连字符 / 下划线。"""
    return _SEP_RE.sub("", text or "").upper()


def pattern() -> str:
    """配置里的自定义课程代码正则（空 = 用通用规则）。"""
    return cfg_str("course_code_pattern")


def _by_pattern(text: str, pat: str) -> str:
    """按自定义正则提取；正则非法或没命中都返回空串。

    有捕获组时取**第一个非空捕获组**（``XY-(\\d+)`` -> ``9999``）；没有捕获组
    则取整个匹配（``XY-\\d+`` -> ``XY9999``）。两种写法都允许。
    """
    if not text or not pat:
        return ""
    try:
        m = re.search(pat, text, re.IGNORECASE)
    except re.error:
        return ""
    if not m:
        return ""
    if m.groups():
        for g in m.groups():
            if g:
                return normalize_key(g)
    return normalize_key(m.group(0))


def _strip_term(text: str) -> str:
    """去掉开头的学期数字前缀与其后分隔符（``202609 - CS5187`` -> ``CS5187``）。"""
    return _TERM_PREFIX_RE.sub("", (text or "").strip()).lstrip(" -_.")


def _fragment(text: str) -> str:
    """在文本里找课程代码片段。

    整串是**下划线多段码**时不拆（``SD_ANTI_DECEPTION_2026-27_0`` 里的数字
    属于编码本身，不是课程号）；否则取第一个「完整字母词 + 数字」的片段。
    """
    text = text or ""
    if _MULTISEG_RE.match(text.strip()):
        return ""
    m = _FRAG_RE.search(text)
    if not m:
        return ""
    return normalize_key(m.group(1) + m.group(2))


def _extract_code_field(code: str) -> str:
    """从 ``course_code`` 字段提取课程代码（整体形态优先，其次片段）。"""
    if not code:
        return ""
    stripped = _strip_term(code)
    whole = normalize_key(stripped)
    if _WHOLE_RE.match(whole):
        return whole
    return _fragment(code)


def _extract_name_head(name: str) -> str:
    """从课程名提取课程代码。

    先认**开头**的片段（``CS5187 Vision and Image`` -> ``CS5187``）；开头
    没有再看名字里独立的片段（``Introduction to CS101`` -> ``CS101``）——
    名字中段的「Algorithms 2024」这类年份不是课程号，靠下划线分段规则排除。
    """
    if not name:
        return ""
    stripped = _strip_term(name)
    m = _HEAD_RE.match(stripped)
    if m:
        return normalize_key(m.group(1) + m.group(2))
    whole = normalize_key(stripped)
    if _WHOLE_RE.match(whole):
        return whole
    return _fragment(stripped)


def course_code(code: str = "", name: str = "", pat: str | None = None, fallback: str = "") -> str:
    """提取规范化课程代码（跨院校通用）。

    优先级：自定义正则 > ``course_code`` 字段 > 课程名开头 > 兜底
    （``code`` -> ``name`` -> ``fallback``，截断 20 字符，与历史行为一致）。

    >>> course_code("202609CS5187")
    'CS5187'
    >>> course_code("MATH 101")
    'MATH101'
    >>> course_code("SD_GEN_SFTY_CS_MCS_2026")
    'SD_GEN_SFTY_CS_MCS_2'
    """
    pat = pattern() if pat is None else pat
    if pat:
        got = _by_pattern(code, pat) or _by_pattern(name, pat)
        if got:
            return got

    found = _extract_code_field(code)
    if found:
        return found
    found = _extract_name_head(name)
    if found:
        return found
    return (code or name or fallback or "").strip()[:20]


def dir_key(name: str, pat: str | None = None) -> str:
    """本地课程目录 -> 匹配用的键。

    能提取出课程代码就用代码（``CS5187 - Vision and Image`` -> ``CS5187``），
    提取不出就退回目录名大写（与历史行为一致）。
    """
    pat = pattern() if pat is None else pat
    if pat:
        got = _by_pattern(name, pat)
        if got:
            return got
    found = _extract_code_field(name) or _extract_name_head(name)
    if found:
        return found
    return (name or "").upper()
