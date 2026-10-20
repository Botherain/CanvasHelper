"""把 Canvas 的内容映射到本地已有的课程目录结构。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .canvas_api import Item
from .coursecode import dir_key, normalize_key
from .paths import DEFAULT_CATEGORIES, get_logger

log = get_logger()

# 磁盘目录前缀 -> 逻辑分类
PREFIX_KEY = {"1": "lecture", "2": "tutorial", "3": "assignment", "4": "project", "5": "book", "6": "other"}

ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 关键词规则：按优先级从上到下，命中即返回。
# 全部使用「词边界 + 归一化（下划线/连字符转空格）」匹配，避免 syllabus 命中 lab、
# past_paper 命中 paper 这类子串误判。
_RULES: list[tuple[str, str]] = [
    ("project", r"\b(project|projects|proposal|final presentation|poster|group work|milestone)\b|课题|项目"),
    ("assignment",
     r"\b(assignments?|homework|hw|quizzes|quiz|labs?|lab work|essay|submissions?|deliverable)\b|作业|提交"),
    ("other",
     r"\b(past paper|past papers|old exam|exams?|midterm|final exam|syllabus|outline|policies|policy|"
     r"grading|grade|course info|announcements?|faq|schedule|handouts?|template|orientation)\b|考卷|考试|大纲"),
    ("tutorial",
     r"\b(tutorials?|recitation|workshops?|exercises?|solutions?|drills?|colab)\b|习题|上机"),
    ("lecture",
     r"\b(lectures?|weeks?|slides?|notes?|chapters?|topics?)\b|课件|第.{0,3}周"),
    ("book",
     r"\b(books?|readings?|papers?|references?|textbooks?|bibliograph\w*|surveys?)\b|阅读|参考"),
]

WORD_RULES: list[tuple[str, re.Pattern]] = [
    (key, re.compile(pattern, re.IGNORECASE)) for key, pattern in _RULES
]

# 各分类内部允许的数字子目录样式（正则前缀）
_SUB_STYLE: dict[str, tuple[str, ...]] = {
    "tutorial": ("week", "tutorial", "recitation", "session"),
    "assignment": ("assignment", "hw", "homework", "lab"),
    "project": ("project", "proposal", "phase"),
    # 讲义按章/周分组：`Lecture1` / `Week3` 这类既有目录就是样式样本
    "lecture": ("lecture", "week", "chapter", "topic", "unit", "lec", "session"),
    "book": (),
    "other": (),
}

# 例如 Assignment_1 / Week1 / Tutorial 3 -> (词, 分隔符, 数字)
SUBDIR_RE = re.compile(r"^([A-Za-z]+)([\s_\-]*)(\d+)")

# 章节识别：关键字 + 编号（Lecture3a / Week 2 / chapter-4 / Ch3）。
# 只认「词 + 数字」这种强信号——用「名字里第一个数字」会让
# `CS0003 - Lecture slides.pdf` 被误分成 Lecture54。
_CHAPTER_KW = r"(?:lectures?|lecs?|weeks?|wk|chapters?|chap|ch|topics?|units?|sessions?)"
CHAPTER_RE = re.compile(rf"\b({_CHAPTER_KW})[\s_\-]*(\d{{1,2}})\b", re.IGNORECASE)

# 纯数字开头：`1.Review of Algorithms.pptx`、`0.Introduction.pptx`、`5.3. Network…`
NUM_PREFIX_RE = re.compile(r"^(\d{1,2})(?=[\s_\-.])")


def sanitize(name: str) -> str:
    name = ILLEGAL.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name or "未命名"


def course_dirs(root: Path) -> dict[str, Path]:
    """扫描 classes 根目录，返回 课程代码 -> 本地文件夹。

    键用与课程代码**同一套**提取规则（见 :mod:`canvas_helper.coursecode`），
    这样别的学校（``MATH 101 - Calculus``）也能匹配上，而不会每次同步都
    新建一个重复文件夹。
    """
    out: dict[str, Path] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        out[dir_key(p.name)] = p
    return out


def match_folder(code: str, folders: dict[str, Path]) -> Path | None:
    """课程代码匹配本地文件夹，例如 CS0001 -> 'CS0001 - Vision and Image'。

    比较时忽略空格 / 连字符 / 下划线的差异（``MATH 101`` 与 ``MATH101`` 视为
    同一个），并保留「前缀匹配」的老行为（目录键比课程代码更长时也算命中）。
    """
    key = normalize_key(code)
    if not key:
        return None
    for k, v in folders.items():
        if normalize_key(k) == key:
            return v
    for k, v in folders.items():
        if normalize_key(k).startswith(key):
            return v
    return None


@dataclass
class CategoryDirs:
    """一门课的分类目录（磁盘上真实存在的优先）。"""

    course_dir: Path
    paths: dict[str, Path]

    @classmethod
    def scan(cls, course_dir: Path) -> CategoryDirs:
        found: dict[str, Path] = {}
        if course_dir.is_dir():
            for p in sorted(course_dir.iterdir()):
                if not p.is_dir():
                    continue
                m = re.match(r"^(\d)\.", p.name)
                if m:
                    key = PREFIX_KEY.get(m.group(1))
                    if key and key not in found:
                        found[key] = p
        for key, default in DEFAULT_CATEGORIES.items():
            if key not in found:
                found[key] = course_dir / default
        return cls(course_dir=course_dir, paths=found)

    def get(self, key: str) -> Path:
        return self.paths.get(key, self.course_dir / DEFAULT_CATEGORIES[key])

    def existing(self, key: str) -> Path | None:
        p = self.paths.get(key)
        return p if p and p.is_dir() else None


def _norm(text: str) -> str:
    """归一化：下划线/连字符转空格，字母与数字断开，压缩空白。

    `Assignment1` / `Week2` 这类没有分隔符的名字必须断开，否则
    `\\bassignment\\b` 匹配不到（字母和数字之间不存在词边界）。
    """
    text = re.sub(r"[_\-/]+", " ", text or "")
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# 文件夹名本身就是分类词时，不再重复建一层子目录
_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "tutorial": ("tutorial", "tutorials", "recitation", "workshop", "习题"),
    "assignment": ("assignment", "assignments", "homework", "作业"),
    "project": ("project", "projects", "课题"),
    "lecture": ("lecture", "lectures"),
    "book": ("book", "books", "reading", "readings"),
    "other": (),
}

DOC_SUFFIXES = {".ppt", ".pptx", ".pdf", ".doc", ".docx", ".key", ".odp", ".odt", ".txt", ".md"}


def folder_segments(folder_path: str) -> list[str]:
    """`course files/Lecture/Week3` -> ['Lecture', 'Week3']。"""
    parts = [p.strip() for p in (folder_path or "").split("/") if p.strip()]
    while parts and parts[0].lower().startswith("course files"):
        parts.pop(0)
    return parts


def keyword_match(text: str) -> str | None:
    for key, pattern in WORD_RULES:
        if pattern.search(text):
            return key
    return None


def category_of(folder_path: str, title: str, kind: str = "", module_name: str = "") -> str:
    """判定内容属于哪个分类。

    优先级：**文件夹路径（结构信号）> 文件名/模块名 > 类型兜底 > 根目录演示文档**。
    唯一例外：文件夹只是泛泛的 `Week N` 讲义目录，而文件名明确是作业/项目时，
    以文件名（更具体）为准。
    """
    segs = folder_segments(folder_path)
    folder_key = keyword_match(_norm(" ".join(segs))) if segs else None
    title_key = keyword_match(_norm(f"{module_name} {title}"))

    if folder_key:
        if folder_key == "lecture" and title_key in ("assignment", "project"):
            return title_key
        return folder_key
    if title_key:
        return title_key
    if kind in ("Assignment", "Quiz"):
        return "assignment"
    # 整门课就堆在根目录几个 pptx/pdf 的情况（CS0006/CS0004）：当讲义处理
    if not segs and Path(title or "").suffix.lower() in DOC_SUFFIXES:
        return "lecture"
    return "other"


def _number(module_name: str, title: str) -> int | None:
    for src in (module_name, title):
        m = re.search(r"(?<!\d)(\d{1,2})(?!\d)", src or "")
        if m:
            n = int(m.group(1))
            if 1 <= n <= 60:
                return n
    return None


def _chapter_hit(text: str) -> tuple[int, str] | None:
    """「关键字 + 编号」命中 -> (编号, 命中的词)；第 0 章也算。"""
    m = CHAPTER_RE.search(_norm(text or ""))
    if m and 0 <= int(m.group(2)) <= 60:
        return int(m.group(2)), m.group(1)
    return None


def _num_prefix_hit(text: str) -> int | None:
    """文件名纯数字开头 -> 编号（`1.Review…` / `0.Introduction…`）。"""
    m = NUM_PREFIX_RE.match((text or "").strip())
    if m and 0 <= int(m.group(1)) <= 60:
        return int(m.group(1))
    return None


def _singular(word: str) -> str:
    """Lectures -> Lecture、Weeks -> Week，并统一成 Title 大小写。"""
    w = word or ""
    if len(w) > 3 and w.lower().endswith("s") and not w.lower().endswith("ss"):
        w = w[:-1]
    return w[:1].upper() + w[1:].lower()


def lecture_dir(cat_dir: Path, folder_path: str, module_name: str, title: str) -> Path:
    """讲义的章节目录；识别不到章节号时返回分类目录本身（保持平铺）。

    编号取值优先级：**Canvas 文件夹里的编号 > 模块名/文件名的关键字+编号 >
    文件名纯数字开头**。目录名以本地既有样式为准（`Lecture1` 样本 -> 一律
    `Lecture{n}`），没有样本才用命中的词，再没有就默认 `Lecture`。
    """
    segs = folder_segments(folder_path)
    hit: tuple[int, str] | None = None
    num: int | None = None

    # ① 结构信号：Canvas 文件夹里的编号（关键字优先，纯数字其次）
    for seg in segs:
        hit = _chapter_hit(seg)
        if hit:
            num = hit[0]
            break
    if num is None:
        for seg in segs:
            num = _num_prefix_hit(seg)
            if num is not None:
                break

    # ② 关键字 + 编号：模块名 -> 文件名
    word = hit[1] if hit else ""
    if num is None:
        for src in (module_name, title):
            hit = _chapter_hit(src)
            if hit:
                num, word = hit
                break

    # ③ 纯数字开头的文件名
    if num is None:
        num = _num_prefix_hit(title)
    if num is None:
        return cat_dir  # 识别不到章节（data.zip / jupyter-shortcuts.pdf）-> 平铺

    sample = _style_sample(cat_dir, "lecture")
    if sample:
        out_word, sep = sample          # 跟随本地既有样式，如 Lecture1 -> Lecture{n}
    elif word:
        out_word, sep = _singular(word), ""
    else:
        out_word, sep = "Lecture", ""
    return _find_numbered(cat_dir, out_word, num) or (cat_dir / f"{out_word}{sep}{num}")


def _style_sample(cat_dir: Path, key: str) -> tuple[str, str] | None:
    """在分类目录里找一个已有的数字子目录，返回 (词, 分隔符) 作为样式样本。"""
    prefixes = _SUB_STYLE.get(key, ())
    if not prefixes or not cat_dir.is_dir():
        return None
    for p in sorted(cat_dir.iterdir()):
        if not p.is_dir():
            continue
        m = SUBDIR_RE.match(p.name)
        if m and m.group(1).lower() in prefixes:
            return m.group(1), m.group(2)
    return None


def _find_numbered(cat_dir: Path, word: str, n: int) -> Path | None:
    if not cat_dir.is_dir():
        return None
    for p in cat_dir.iterdir():
        if p.is_dir():
            m = SUBDIR_RE.match(p.name)
            if m and m.group(1).lower() == word.lower() and int(m.group(3)) == n:
                return p
    return None


def _by_number(cat_dir: Path, key: str, module_name: str, title: str) -> Path:
    """没有文件夹结构时，退回「模块名/标题里的编号 + 本地已有目录样式」。"""
    n = _number(module_name, title)
    sample = _style_sample(cat_dir, key)
    if n is None or sample is None:
        return cat_dir
    word, sep = sample
    return _find_numbered(cat_dir, word, n) or (cat_dir / f"{word}{sep}{n}")


def _is_category_word(seg: str, key: str) -> bool:
    """文件夹名本身就是分类（Tutorial / Tutorials）时应丢掉，避免重复一层。"""
    if SUBDIR_RE.match(seg):
        return False  # 带编号的 Assignment1 要保留，后面再统一成既有样式
    s = _norm(seg).lower()
    if not s:
        return False
    return any(s == w or s.startswith(w) for w in _CATEGORY_WORDS.get(key, ()))


def resolve_dir(cats: CategoryDirs, folder_path: str, module_name: str,
                title: str, kind: str) -> Path:
    """返回最终目录（可能尚未创建）。

    优先**镜像 Canvas 文件夹结构**，例如
    `course files/Assignment1/CV_guideline/data` -> `3.Assignments/Assignment_1/CV_guideline/data`；
    讲义分类按章/周归组到 `1.Lecture session/Lecture3/`，识别不到章节号的
    （`data.zip` 这类散件）保持平铺，见 `lecture_dir`。
    """
    key = category_of(folder_path, title, kind, module_name)
    cat_dir = cats.get(key)
    if key == "lecture":
        return lecture_dir(cat_dir, folder_path, module_name, title)

    segs = folder_segments(folder_path)
    if segs and _is_category_word(segs[0], key):
        segs = segs[1:]
    if not segs:
        return _by_number(cat_dir, key, module_name, title)

    first = sanitize(segs[0])
    sample = _style_sample(cat_dir, key)
    if sample:
        m = SUBDIR_RE.match(first)
        word, sep = sample
        if m and m.group(1).lower() == word.lower():
            n = int(m.group(3))
            existing = _find_numbered(cat_dir, word, n)
            first = existing.name if existing else f"{word}{sep}{n}"
    target = cat_dir / first
    for seg in segs[1:]:
        target = target / sanitize(seg)
    return target


def unique_path(path: Path) -> Path:
    """重名时追加 (1)/(2)。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def anchor_key(name: str) -> str:
    """文件名归一化：忽略大小写、前后缀编号、改名后缀与标点差异。

    `1.Review of Algorithms.pptx` 与 Canvas 的 `1. Review of Algorithms.pptx`、
    `0.Introduction.pptx` 与 `Introduction.pptx` 应视为同一份文件。

    同时忽略 `unique_path` 追加的 ` (1)` / ` (2)`：同名不同大小时本地会存成
    `x (1).pptx`，下次同步**不能**因为名字变了就再下一遍。

    但 `0.jpg` / `1.jpg` 这类**纯数字名**绝不能合并——编号后面必须还有实质内容，
    否则整个数据集的图片会坍缩成同一个 key，导致漏下。
    """
    raw = (name or "").strip()
    raw = re.sub(r"\s*\(\d+\)(?=\.[^.]+$)", "", raw)  # 去掉 (1)/(2) 改名后缀
    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, ""
    m = re.match(r"^\d+[\s_\-.]+(.*)$", stem)
    if m and m.group(1).strip():
        stem = m.group(1)
    return re.sub(r"[^0-9a-z]", "", f"{stem}.{ext}".lower())


class LocalIndex:
    """课程目录里已有内容的索引，用于判断「这份内容其实已经在本地了」。

    同一个归一化名可能对应多份不同大小的文件（例如本地旧版 `x.pptx` 与
    上次同步存下的 `x (1).pptx`），所以按名字存列表，再用**大小**挑出正确那份。
    """

    def __init__(self, course_dir: Path) -> None:
        self.by_name: dict[str, list[tuple[int, Path]]] = {}
        if not course_dir.is_dir():
            return
        for p in course_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            self.by_name.setdefault(anchor_key(p.name), []).append((st.st_size, p))

    def find(self, name: str, size: int | None) -> Path | None:
        """返回本地已有的同内容文件路径；找不到返回 None。"""
        rows = self.by_name.get(anchor_key(name))
        if not rows:
            return None
        if size is None:
            return rows[0][1]
        for sz, path in rows:
            if sz == size:
                return path
        return None


def build_local_index(course_dir: Path) -> LocalIndex:
    """扫描课程目录，构建 `LocalIndex`。"""
    return LocalIndex(course_dir)


def plan_item(course_dir: Path, item: Item) -> Path:
    """计算某个内容项的落盘路径（不创建目录）。

    只有**真实的 Canvas 文件夹**才镜像成子目录；模块名只参与分类与编号推断，
    否则模块会被误当成一层物理目录。
    """
    cats = CategoryDirs.scan(course_dir)
    target_dir = resolve_dir(cats, item.folder_path, item.module_name,
                             item.display_name, item.kind)
    return target_dir / sanitize(item.display_name)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def categorize_items(course_dir: Path, items: Iterable[Item]) -> dict[str, list[Item]]:
    """按分类分组，用于预览。"""
    groups: dict[str, list[Item]] = {}
    for it in items:
        key = category_of(it.folder_path, it.display_name, it.kind, it.module_name)
        groups.setdefault(key, []).append(it)
    return groups
