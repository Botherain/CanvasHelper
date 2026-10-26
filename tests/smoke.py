"""离线自检：分类规则、目录推断、压缩包解压、讲义整理、讨论区链接、待办合并、登录过期契约、打包元数据。

两种入口共用同一套断言，因此不会出现「脚本绿、pytest 红」的分叉：

* ``python tests/smoke.py`` —— 逐条打印 PASS/FAIL，任一失败退出码 1；
* ``python -m pytest tests`` —— 按小节拆成独立用例，失败时直接给出该小节明细。

不联网；真实 ``state.json`` / ``pending_todos.json`` 由 conftest 备份还原。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

# Windows 控制台可能是 cp1252/GBK，中文 print 会直接 UnicodeEncodeError。
# 必须在 import canvas_helper 之前切到 UTF-8，保证在任何控制台（含 CI）都能跑。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from canvas_helper.canvas_api import Course, Item, decode_name  # noqa: E402
from canvas_helper.classify import (  # noqa: E402
    CategoryDirs,
    anchor_key,
    category_of,
    course_dirs,
    match_folder,
    resolve_dir,
    sanitize,
)
from canvas_helper.coursecode import course_code, dir_key  # noqa: E402
from canvas_helper.deskbox import (  # noqa: E402
    TodoDraft,
    build_drafts,
    dedupe_completed,
    ensure_course_colors,
    load_doc,
    merge,
    prepare,
    preview_status,
    recolor_items,
    reorder_by_due,
)
from canvas_helper.paths import DEFAULT_CONFIG, load_config, to_local  # noqa: E402

PASS = 0
FAIL = 0
#: 失败明细。脚本模式用于收尾汇总，pytest 模式由 test_smoke 断言为空。
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(f"{name}" + (f"  [{detail}]" if detail else ""))


# 真实课程目录的结构蓝本。本机有 classes_root 时用真的（更贴近实战），
# 干净的 CI 机器上按这个造目录——[3] 的断言本质是「跟随本地结构」，
# 所以必须给出结构，而不是给一个空目录。
COURSE_FIXTURE = {
    "CS0001 - Vision and Image": (
        "1.Lecture session",
        "2.Tutorial session/Week1",
        "2.Tutorial session/Week10",
        "3.Assignments/Assignment_1",
        "3.Assignments/Assignment_2",
        "4.Projects",
        "5.Useful links",
    ),
    "CS0002 - Intelligent Systems": (
        "1.Lecture session",
        "2.Tutorial session",          # 平铺、无编号子目录 -> 保持平铺
        "3.Assignments/Assignment1",
        "4.Projects",
        "5.Books",
    ),
    "CS0003 - Machine Learning": (
        "1.Lecture session",
        "2.Tutorial session/Tutorial1",   # 编号样式 Tutorial + 空分隔 -> Tutorial5
        "3.Assignments/Assignment1",
        "4.Projects",
    ),
    "CS0004 - Generative AI": (
        "1.Lecture session", "2.Tutorial session", "3.Assignments",
        "4.Projects", "5.Books", "6.Others",
    ),
    "CS0005 - Explainable AI": (
        "1.Lecture session", "2.Tutorial session", "3.Assignments",
        "4.Projects", "5.Books", "6.Others",
    ),
    "CS0006 - Algorithm Analysis & Game Theory": (
        "1.Lecture session", "2.Tutorial session", "3.Assignments", "4.Projects",
    ),
}


# --------------------------------------------------------------------------- #
# 共用准备（pytest 用 fixture 调，脚本用 main() 调；断言与准备分开，
# 这样 [1]/[3] 能共享 folders、[5]/[7] 能共享 draft 上下文而不重复跑断言）
# --------------------------------------------------------------------------- #
def build_course_root() -> tuple[Path, Path]:
    """返回 (课程根, 临时蓝本根)，两者相同，由调用方负责删除。

    始终按 ``COURSE_FIXTURE`` 造临时根：测试必须在任何机器上自洽——
    真实课程根的目录名因人而异（课程号、命名习惯都不同），
    不能进断言；识别真实目录的能力由历史标定保证。
    """
    fixture_root = Path(tempfile.mkdtemp(prefix="ch_courses_"))
    for course, subs in COURSE_FIXTURE.items():
        for sub in subs:
            (fixture_root / course / sub).mkdir(parents=True, exist_ok=True)
    return fixture_root, fixture_root


def build_drafts_ctx(cfg: dict) -> tuple:
    """[5] 与 [7] 共用的准备：构造课程 + 3 条作业 + build_drafts 结果。"""
    c = Course(id=1, name="Vision and Image", code="CS0001")
    items = [
        Item(course_id=1, module_name="Assignment 2", title="Assignment 2: Segmentation",
             kind="Assignment", html_url="https://canvas.example.edu/courses/1/assignments/9001",
             due_at="2026-11-20T15:59:00Z",
             extra={"description": "<p>完成 <b>U-Net</b> 模型的训练。</p>"}),
        Item(course_id=1, module_name="Project", title="Final Project Report",
             kind="Assignment", html_url="https://canvas.example.edu/courses/1/assignments/9002",
             due_at="2026-12-10T15:59:00Z", extra={}),
        Item(course_id=1, module_name="Past", title="Old assignment",
             kind="Assignment", html_url="https://canvas.example.edu/courses/1/assignments/1",
             due_at="2020-01-01T15:59:00Z", extra={}),
    ]
    res = build_drafts({1: c}, {1: items})
    return c, res


# --------------------------------------------------------------------------- #
# 小节
# --------------------------------------------------------------------------- #
def s1_folders(folders: dict) -> None:
    print(f"\n[1] 识别本地课程文件夹 ({len(folders)})")
    for k, v in folders.items():
        print(f"      {k} -> {v.name}")
    check("识别到 6 门课", len(folders) >= 6, str(len(folders)))
    check("匹配 CS0001", match_folder("CS0001", folders) is not None)
    check("匹配 CS0006", match_folder("CS0006", folders) is not None)


def s2_classify() -> None:
    print("\n[2] 分类规则（文件夹优先 > 文件名 > 类型 > 根目录演示稿）")
    cases = [
        # (Canvas 文件夹路径, 文件名, 类型, 期望分类)
        ("course files/Lecture/Week 3", "slides.pdf", "File", "lecture"),
        ("course files/Assignment1", "handout.pdf", "File", "assignment"),
        ("course files/Tutorial", "Week4 solution.pdf", "File", "tutorial"),
        ("course files/Project", "Milestone 1 Report.pdf", "File", "project"),
        ("course files/Reading List", "paper.pdf", "File", "book"),
        ("course files/Course Info", "Syllabus.pdf", "File", "other"),
        ("course files", "past_paper_2025.pdf", "File", "other"),
        # 泛泛的 Week 目录 + 明确的作业名 -> 以文件名为准
        ("course files/Week 6", "Assignment 2 due.pdf", "File", "assignment"),
        # 整门课堆在根目录的演示稿 -> 当讲义
        ("course files", "Introduction.pptx", "File", "lecture"),
        ("course files/Assignment1/CV_guideline/data/gallery", "0.jpg", "File", "assignment"),
        ("", "", "Assignment", "assignment"),               # 类型兜底
        ("course files/Assignment1", "x.pdf", "File", "assignment"),  # Assignment1 无分隔符
        ("", "Tutorial+1.pdf", "File", "tutorial"),
        # 讨论区帖子标题（CS0004 的 "Colab sharing for weekN"）按教程处理
        ("", "Colab sharing for week7", "Discussion", "tutorial"),
    ]
    for folder, title, kind, want in cases:
        got = category_of(folder, title, kind)
        check(f"{folder or '(根)'}/{title} -> {want}", got == want, f"got={got}")


def s3_resolve(folders: dict) -> None:
    print("\n[3] 目录解析（镜像 Canvas 文件夹 + 跟随本地现状）")
    d5187 = match_folder("CS0001", folders)
    cats = CategoryDirs.scan(d5187)
    p = resolve_dir(cats, "course files/Lecture/Week3", "", "week3.pdf", "File")
    check("讲义按章归组到 Week3", p.name == "Week3" and p.parent == cats.get("lecture"), str(p))
    p = resolve_dir(cats, "course files/Tutorial/Week2", "", "sol.pdf", "File")
    check("Tutorial 镜像到已有 Week2", p.name.lower() == "week2", str(p))
    p = resolve_dir(cats, "course files/Assignment1/CV_guideline/data/gallery", "", "0.jpg", "File")
    rel = p.relative_to(d5187).parts
    check("Assignment1 -> 既有 Assignment_1 并镜像下级",
          rel == ("3.Assignments", "Assignment_1", "CV_guideline", "data", "gallery"), str(rel))
    p = resolve_dir(cats, "Tutorial", "", "Week5 solution.pdf", "File")
    check("无文件夹时用模块名/编号推断 Week5",
          p.name.lower() in ("week5", "week 5"), str(p))
    p = resolve_dir(cats, "Assignment 3", "", "A3.pdf", "File")
    check("CS0001 assignment -> Assignment_3", p.name == "Assignment_3", str(p))
    p = resolve_dir(cats, "Course Info", "", "Syllabus.pdf", "File")
    check("无关键词 -> 6.Others（保留文件夹）",
          p.relative_to(d5187).parts[0].startswith("6."), str(p))

    d5486 = match_folder("CS0002", folders)
    cats2 = CategoryDirs.scan(d5486)
    p = resolve_dir(cats2, "course files/Tutorials", "", "Tutorial 3.pdf", "File")
    # CS0002 的 tutorial 目录里只有平铺 PDF、没有数字子目录 -> 保持平铺
    check("CS0002 tutorial 保持平铺", p == cats2.get("tutorial"), str(p))
    d5489 = match_folder("CS0003", folders)
    cats3 = CategoryDirs.scan(d5489)
    p = resolve_dir(cats3, "course files/Tutorial", "", "Week5.pdf", "File")
    check("CS0003 tutorial -> Tutorial5",
          p.name.lower().replace(" ", "") == "tutorial5", str(p))
    d6382 = match_folder("CS0006", folders)
    cats4 = CategoryDirs.scan(d6382)
    p = resolve_dir(cats4, "course files/Lecture notes", "", "1. Review of Algorithms.pptx", "File")
    check("CS0006 纯数字开头 -> Lecture1", p.name == "Lecture1"
          and p.parent == cats4.get("lecture"), str(p))
    p = resolve_dir(cats4, "course files", "", "Introduction.pptx", "File")
    check("CS0006 根目录 pptx -> 讲义", p == cats4.get("lecture"), str(p))
    p = resolve_dir(cats4, "course files", "", "Syllabus.pdf", "File")
    check("CS0006 根目录 syllabus -> 6.Others", p == cats4.get("other"), str(p))
    p = resolve_dir(cats4, "", "", "jupyter-shortcuts.pdf", "File")
    check("无编号讲义保持平铺", p == cats4.get("lecture"), str(p))
    p = resolve_dir(cats4, "", "", "0.Introduction.pptx", "File")
    check("第 0 章归到 Lecture0", p.name == "Lecture0", str(p))
    p = resolve_dir(cats4, "", "Week 2", "slides for week.pdf", "File")
    # 编号必须生效；目录名跟随本地样式——真实库整理后是 LectureN，蓝本里无样本才是 Week2
    check("模块名里的 Week 编号生效",
          p.name.lower().replace(" ", "") in ("week2", "lecture2"), str(p))


def s4_sanitize() -> None:
    print("\n[4] 文件名清洗 / 编码 / 归一化")
    check("清洗非法字符", sanitize('a/b:c*?.pptx') == "a_b_c__.pptx", sanitize('a/b:c*?.pptx'))
    check("空名兜底", sanitize("   ") == "未命名", sanitize("   "))
    check("解码 + 号", decode_name("Tutorial+1.pdf") == "Tutorial 1.pdf",
          decode_name("Tutorial+1.pdf"))
    check("解码 % 编码", decode_name("2.+Approximation+Algorithms+%28new%29.pptx")
          == "2. Approximation Algorithms (new).pptx",
          decode_name("2.+Approximation+Algorithms+%28new%29.pptx"))
    check("保留连续加号", decode_name("C++.pdf") == "C++.pdf", decode_name("C++.pdf"))
    check("归一化忽略点后编号",
          anchor_key("1.Review of Algorithms.pptx") == anchor_key("1. Review of Algorithms.pptx"))
    check("归一化忽略前导 0.",
          anchor_key("0.Introduction.pptx") == anchor_key("Introduction.pptx"))
    check("归一化区分不同周",
          anchor_key("Week1.pdf") != anchor_key("Week2.pdf"))
    check("纯数字文件名不合并",
          anchor_key("0.npy") != anchor_key("1.npy"), f"{anchor_key('0.npy')!r}")
    check("纯数字图片不合并",
          anchor_key("0.jpg") != anchor_key("19403.jpg"),
          f"{anchor_key('0.jpg')!r} / {anchor_key('19403.jpg')!r}")


def s5_drafts(c: Course, res, cfg: dict) -> None:
    from datetime import datetime

    print("\n[5] 待办草稿 + 合并（副本，不碰真实文件）")
    check("跳过已过期", res.skipped_past == 1, str(res.skipped_past))
    check("生成 2 条草稿", len(res.drafts) == 2, str(len(res.drafts)))
    d0 = res.drafts[0]
    # 断言与机器时区无关：草稿的截止时间必须**按本地时区**呈现（配置 timezone
    # 优先，否则跟随系统），而不是写死 +08:00——否则换时区的机器/CI 会误报。
    check("截止时间按本地时区呈现",
          d0.due.utcoffset() == to_local(d0.due).utcoffset()
          and d0.due.strftime("%z") == to_local(d0.due).strftime("%z"),
          str(d0.due))
    check("截止时间与 Canvas 给的绝对时刻一致",
          d0.due == datetime.fromisoformat("2026-11-20T15:59:00+00:00"), str(d0.due))
    # 3.11 之前 fromisoformat 不认结尾的 Z，Canvas 恰好爱用这种写法；解析不出来
    # 就等于「这条作业没有截止时间」，且不会报错——只能靠断言盯着。
    from canvas_helper.deskbox import _parse_due

    check("Z 结尾的截止时间也能解析",
          _parse_due("2026-11-20T15:59:00Z")
          == datetime.fromisoformat("2026-11-20T15:59:00+00:00"),
          str(_parse_due("2026-11-20T15:59:00Z")))
    check("文本带课程代码", d0.text.startswith("CS0001"), d0.text)
    check("作业标红", d0.color == "red", d0.color)
    check("项目标橙", res.drafts[1].color == "orange", res.drafts[1].color)
    check("备注含 canvas 链接", "canvas.example.edu" in d0.notes, d0.notes)

    src = Path(cfg["deskbox"]["todo_path"])
    tmpdir = Path(tempfile.mkdtemp(prefix="ch_test_"))
    fake_todo = tmpdir / "todo.json"
    if src.is_file():
        shutil.copy2(src, fake_todo)
    else:
        # 机器上没有 DeskBox 数据时造一份 3 条的种子库，断言与真实数据解耦
        fake_todo.write_text(json.dumps({
            "version": 3,
            "items": [
                {"id": f"seed{i}", "text": f"Seed item {i}", "isCompleted": False,
                 "dueDate": f"2026-01-0{i}T00:00:00+08:00",
                 "notes": "", "sortOrder": i}
                for i in range(1, 4)
            ],
        }, ensure_ascii=False), encoding="utf-8")
    doc = load_doc(fake_todo)
    before = len(doc["items"])
    doc, added = merge(doc, res.drafts, 1440)
    check("合并新增 2 条", len(added) == 2, f"added={len(added)} before={before}")
    check("总条目 +2", len(doc["items"]) == before + 2)
    doc2, added2 = merge(doc, res.drafts, 1440)
    check("重复合并不产生重复", len(added2) == 0, str(len(added2)))
    # 按绝对时刻找新条目（不写死日期前缀：写进去的 dueDate 带本地时区偏移）
    new_items = [i for i in doc2["items"]
                 if str(i.get("dueDate", "")).startswith(
                     to_local(datetime.fromisoformat("2026-11-20T15:59:00+00:00")).strftime("%Y-%m-%d"))]
    check("新条目字段齐全",
          all(k in new_items[0] for k in
              ("id", "text", "isCompleted", "colorMarker", "dueDate", "notes",
               "attachments", "reminderOffsetMinutes", "sortOrder", "createdAt", "updatedAt")),
          json.dumps(new_items[0], ensure_ascii=False)[:200] if new_items else "missing")
    check("sortOrder 递增", new_items and new_items[0]["sortOrder"] >= before,
          str(new_items[0]["sortOrder"]) if new_items else "")
    shutil.rmtree(tmpdir, ignore_errors=True)


def s5b_completed() -> None:
    from datetime import datetime

    print("\n[5b] 已完成作业的处理（不写正式文件）")

    def _draft(title: str, url: str, due_iso: str) -> TodoDraft:
        return TodoDraft(
            item=Item(course_id=1, module_name="", title=title, kind="Assignment"),
            course=Course(id=1, name="Vision", code="CS0001"),
            text=f"CS0001 · {title}",
            due=datetime.fromisoformat(due_iso),
            kind="assignment", url=url, color="red", notes="",
        )

    url_a = "https://canvas.example.edu/courses/1/assignments/7001"
    due_a = "2026-11-01T23:59:00+08:00"

    # (1) 清单里有，且已完成 -> 不重复添加
    doc = {"version": 3, "items": [{
        "id": "a1", "text": "CS0001 · Assignment A", "isCompleted": True,
        "dueDate": due_a, "notes": f"打开：{url_a}", "sortOrder": 1,
    }]}
    d1 = _draft("Assignment A", url_a, due_a)
    st: dict = {}
    doc, added = merge(doc, [d1], 1440, stats=st)
    check("已完成的不重复添加", len(added) == 0 and st.get("completed") == 1, str(st))
    check("草稿标记为已完成", d1.status == "completed" and d1.existing, d1.status)
    check("条目数不变", len(doc["items"]) == 1, str(len(doc["items"])))

    # (2) 完成后被删除 -> 靠记录不再复活
    done_idx = {url_a: {"dueDate": due_a, "completedAt": "2026-10-01T00:00:00+00:00"}}
    d2 = _draft("Assignment A", url_a, due_a)
    st2: dict = {}
    _doc2, added2 = merge({"version": 3, "items": []}, [d2], 1440,
                          done_index=done_idx, stats=st2)
    check("完成并删除后不再冒出来", len(added2) == 0 and st2.get("completed") == 1, str(st2))
    check("状态也是已完成", d2.status == "completed", d2.status)

    # (3) 截止时间变了 -> 视为新截止，重新提醒
    d3 = _draft("Assignment A", url_a, "2026-11-08T23:59:00+08:00")
    st3: dict = {}
    _doc3, added3 = merge({"version": 3, "items": []}, [d3], 1440,
                          done_index=done_idx, stats=st3)
    check("截止时间更新则重新添加", len(added3) == 1 and st3.get("reopened") == 1, str(st3))
    check("状态=截止已更新", d3.status == "reopened", d3.status)

    # (4) 没有链接、文本一致但截止变了 -> 归一化文本兜底匹配
    doc4 = {"version": 3, "items": [{
        "id": "b1", "text": "CS0001 · Quiz 1", "isCompleted": True,
        "dueDate": "2026-11-02T23:59:00+08:00", "notes": None, "sortOrder": 1,
    }]}
    d4 = _draft("Quiz 1", "", "2026-11-03T23:59:00+08:00")
    st4: dict = {}
    merge(doc4, [d4], 1440, stats=st4)
    check("文本兜底也能认出已完成", st4.get("completed") == 1, str(st4))

    # (5) 无链接草稿的 key 用统一时间格式（否则永远匹配不上）
    check("草稿 key 与 todo.json 时间格式一致",
          d4.key == "CS0001 · Quiz 1|2026-11-03T23:59:00+08:00", d4.key)

    # (6) 去重：只删「已完成条目」的未完成重复项
    doc5 = {"version": 3, "items": [
        {"id": "c1", "text": "CS0001 · A", "isCompleted": True, "dueDate": due_a},
        {"id": "c2", "text": "CS0001 · A", "isCompleted": False, "dueDate": due_a},
        {"id": "c3", "text": "整理每周课程文件", "isCompleted": True, "dueDate": due_a},
        {"id": "c4", "text": "整理每周课程文件", "isCompleted": False,
         "dueDate": "2026-11-08T23:59:00+08:00"},
    ]}
    removed = dedupe_completed(doc5)
    check("删除未完成的重复项", len(removed) == 1 and len(doc5["items"]) == 3,
          str([i["id"] for i in doc5["items"]]))
    check("保留已完成那条",
          any(i["id"] == "c1" for i in doc5["items"])
          and not any(i["id"] == "c2" for i in doc5["items"]))
    check("截止不同的同名任务不动", any(i["id"] == "c4" for i in doc5["items"]))
    doc6 = {"version": 3, "items": [
        {"id": "d1", "text": "X", "isCompleted": False, "dueDate": due_a},
        {"id": "d2", "text": "X", "isCompleted": False, "dueDate": due_a},
    ]}
    check("没有完成记录的重复不删", len(dedupe_completed(doc6)) == 0 and len(doc6["items"]) == 2)

    # (7) 只读预判
    prev = Path(tempfile.mkdtemp(prefix="ch_prev_")) / "todo.json"
    prev.write_text(json.dumps(doc4, ensure_ascii=False), encoding="utf-8")
    got = preview_status(prev, [d4, _draft("Nothing",
                                           "https://canvas.example.edu/courses/1/assignments/9999",
                                           due_a)])
    check("预判已完成", got.get(d4.key) == "completed", str(got))
    check("预判未知项", list(got.values()).count("") == 0 and len(got) == 1, str(got))
    shutil.rmtree(prev.parent, ignore_errors=True)


def s5c_cleaner() -> None:
    from canvas_helper import cleaner

    print("\n[5c] 一键清理缓存（只扫描，不删除）")
    targets = cleaner.scan()
    keys = {t.key for t in targets}
    check("扫描出主要缓存项", {"state", "log", "browser"} <= keys, str(sorted(keys)))
    check("登录态默认不勾", all(not t.default for t in targets if t.key == "login"),
          str([(t.key, t.default) for t in targets]))
    check("排队待办默认不勾", all(not t.default for t in targets if t.key == "pending"))
    check("体积格式化", cleaner.fmt_size(0) == "0 B" and cleaner.fmt_size(2048) == "2.0 KB",
          f"{cleaner.fmt_size(0)} / {cleaner.fmt_size(2048)}")
    check("扫描出体积", all(t.bytes >= 0 and t.files >= 0 for t in targets),
          str([(t.key, t.bytes) for t in targets]))
    cp = cleaner.browser_cache_paths()
    check("缓存路径不含 Cookie / 登录数据",
          not any(("cookie" in str(p).lower()) or ("login data" in str(p).lower()) for p in cp),
          str([str(p) for p in cp[:3]]))
    empty = cleaner.run(set())
    check("空勾选不产生任何清理", empty["cleaned"] == [] and empty["bytes"] == 0, str(empty))


def s5d_sort_and_color() -> None:
    print("\n[5d] 待办排序与课程颜色")

    cfg = {"deskbox": {"course_colors": {}, "course_labels": {}, "manual_color": "pink"}}
    courses = [
        Course(id=30, name="C", code="CS6382"),
        Course(id=10, name="A", code="CS5187"),
        Course(id=20, name="B", code="CS5486"),
    ]
    colors = ensure_course_colors(cfg, courses, persist=False)
    check("首次按识别顺序分配颜色",
          colors == {"30": "red", "10": "orange", "20": "yellow"}, str(colors))
    check("课程不使用自建待办颜色", "pink" not in colors.values(), str(colors))
    check("保存课程显示名",
          cfg["deskbox"]["course_labels"]["10"].startswith("CS5187"),
          cfg["deskbox"]["course_labels"].get("10", ""))

    # 用户改过颜色后，新增课程只补空位，不覆盖自定义值
    cfg["deskbox"]["course_colors"]["10"] = "teal"
    colors = ensure_course_colors(
        cfg, [*courses, Course(id=40, name="D", code="CS5494")], persist=False)
    check("已有自定义颜色不被覆盖", colors["10"] == "teal", str(colors))
    check("新课程拿到未占用的颜色",
          colors["40"] not in {colors["30"], colors["10"], colors["20"], "pink"},
          str(colors))

    item = Item(
        course_id=10, module_name="Assignment", title="A1", kind="Assignment",
        html_url="https://canvas.example.edu/courses/10/assignments/1",
        due_at="2026-11-20T15:59:00Z", extra={},
    )
    drafts = build_drafts({10: courses[1]}, {10: [item]}, cfg=cfg)
    check("草稿使用课程颜色", drafts.drafts[0].color == "teal", drafts.drafts[0].color)

    doc = {"items": [
        {"id": "a", "text": "A", "dueDate": "2026-11-03T23:59:00+08:00", "sortOrder": 0},
        {"id": "b", "text": "B", "dueDate": "2026-11-01T23:59:00+08:00", "sortOrder": 1},
        {"id": "c", "text": "C", "dueDate": None, "sortOrder": 2},
        {"id": "d", "text": "D", "dueDate": "2026-11-01T23:59:00+08:00", "sortOrder": 3},
    ]}
    moved = reorder_by_due(doc)
    check("按截止时间升序",
          [i["id"] for i in doc["items"]] == ["b", "d", "a", "c"],
          str([i["id"] for i in doc["items"]]))
    check("无截止时间排最后", doc["items"][-1]["id"] == "c", str(doc["items"][-1]))
    check("同截止时间保持原顺序",
          [i["id"] for i in doc["items"][:2]] == ["b", "d"],
          str([i["id"] for i in doc["items"][:2]]))
    check("sortOrder 连续",
          [i["sortOrder"] for i in doc["items"]] == [0, 1, 2, 3],
          str([i["sortOrder"] for i in doc["items"]]))
    check("记录位置变化数", moved == 4, str(moved))
    check("重复排序幂等", reorder_by_due(doc) == 0, str(reorder_by_due(doc)))

    db = cfg["deskbox"]
    colored = {"items": [
        {"id": "1", "text": "CS5187 · A",
         "notes": "打开：https://canvas.example.edu/courses/10/assignments/1",
         "colorMarker": "yellow"},
        {"id": "2", "text": "CS5486-Assignment", "notes": None, "colorMarker": "red"},
        {"id": "3", "text": "整理每周课程文件", "notes": None, "colorMarker": None},
        {"id": "4", "text": "CS9999 · X", "notes": None, "colorMarker": "blue"},
    ]}
    changed = recolor_items(colored, db)
    check("按 Canvas 链接上课程色", colored["items"][0]["colorMarker"] == "teal",
          str(colored["items"][0]))
    check("旧条目按课程代码兜底", colored["items"][1]["colorMarker"] == "yellow",
          str(colored["items"][1]))
    check("自建待办使用额外颜色", colored["items"][2]["colorMarker"] == "pink",
          str(colored["items"][2]))
    check("未知课程条目不误改", colored["items"][3]["colorMarker"] == "blue",
          str(colored["items"][3]))
    check("颜色改动数", changed == 3, str(changed))

    # 课程代码里带空格（MATH 101）、以及「更像代码」的条目不互相误吞
    spaced = {"items": [
        {"id": "5", "text": "MATH 101 - Problem Set 3", "notes": None, "colorMarker": "red"},
        {"id": "6", "text": "MATH1015 Lab", "notes": None, "colorMarker": "blue"},
    ]}
    db2 = {
        "course_colors": {"900": "green"},
        "course_labels": {"900": "MATH 101 - Calculus"},
        "manual_color": "pink",
    }
    n2 = recolor_items(spaced, db2)
    check("带空格的课程代码也认", spaced["items"][0]["colorMarker"] == "green",
          str(spaced["items"][0]))
    check("代码前缀更长时不误吞", spaced["items"][1]["colorMarker"] == "blue",
          str(spaced["items"][1]))
    check("带空格课程的改动数", n2 == 1, str(n2))

    tmpdir = Path(tempfile.mkdtemp(prefix="ch_sort_"))
    try:
        todo = tmpdir / "todo.json"
        todo.write_text(json.dumps({"version": 3, "items": [
            {"id": "x1", "text": "CS5187 · A", "isCompleted": False,
             "dueDate": "2026-11-03T23:59:00+08:00", "colorMarker": "yellow",
             "notes": "打开：https://canvas.example.edu/courses/10/assignments/1",
             "sortOrder": 0},
            {"id": "x2", "text": "整理文件", "isCompleted": False,
             "dueDate": "2026-11-01T23:59:00+08:00", "colorMarker": None,
             "notes": None, "sortOrder": 1},
        ]}, ensure_ascii=False), encoding="utf-8")
        prep_cfg = {
            "todo_path": str(todo), "sort_by_due": True,
            "remove_duplicates": True, "fix_attachment_paths": True,
            "reminder_offset_minutes": 1440,
            "course_colors": {"10": "teal"},
            "course_labels": {"10": "CS5187 - Vision and Image"},
            "manual_color": "pink",
        }
        _tmp, added, st = prepare(todo, [], prep_cfg)
        check("无新增时排序/改色也标记 changed",
              not added and st.get("changed")
              and st.get("resorted", 0) > 0 and st.get("recolored", 0) == 2,
              str(st))
        # 把整理结果当成磁盘现状再跑一遍：不应该再产生任何改动（否则每次同步都会
        # 无谓地关一次 DeskBox）
        shutil.copy2(_tmp, todo)
        _tmp2, _added2, st2 = prepare(todo, [], prep_cfg)
        check("整理过的待办重复写入不产生改动", st2.get("changed") is False, str(st2))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def s6_plan() -> None:
    from canvas_helper.filesync import build_plan, describe

    print("\n[6] 下载计划（dry-run，不写盘）")
    tmpdir = Path(tempfile.mkdtemp(prefix="ch_plan_"))
    (tmpdir / "3.Assignments").mkdir()
    (tmpdir / "3.Assignments" / "Assignment_1").mkdir()
    items = [
        Item(course_id=9, module_name="Week 2", title="lecture2.pdf", kind="File",
             file_id=101, file_name="lecture2.pdf", file_url="http://x/1", file_size=1000),
        Item(course_id=9, module_name="Assignment 1", title="A1 handout.pdf", kind="File",
             file_id=102, file_name="A1 handout.pdf", file_url="http://x/2", file_size=2000),
        Item(course_id=9, module_name="Course Info", title="Rules.pdf", kind="File",
             file_id=103, file_name="Rules.pdf", file_url="http://x/3", file_size=500),
        Item(course_id=9, module_name="Week 2", title="Week 2 Overview", kind="Page",
             extra={"content_details": {"body": "<p>本周讲义正文</p>"}}),
        Item(course_id=9, module_name="Project", title="Dataset.zip", kind="File",
             file_id=104, file_name="Dataset.zip", file_url="http://x/4", file_size=999),
    ]
    st = {"files": {}}
    plan = build_plan(Course(id=9, name="Demo", code="CS9999"), tmpdir, items, st)
    names = [str(p.target.relative_to(tmpdir)).replace("\\", "/") for p in plan.files]
    print("      计划:")
    for n in names:
        print("        -", n)
    check("计划 5 项", len(plan.files) == 5, str(names))
    check("课件按周归组到 Week2", "1.Lecture session/Week2/lecture2.pdf" in names, str(names))
    check("作业进已有子目录", "3.Assignments/Assignment_1/A1 handout.pdf" in names, str(names))
    check("未匹配进 6.Others", "6.Others/Rules.pdf" in names, str(names))
    check("项目进 4.Projects", "4.Projects/Dataset.zip" in names, str(names))
    check("页面加 html 后缀",
          any(n.endswith("Week 2 Overview.html") for n in names), str(names))
    # 已存在同名（就压在计划的章节目录里）-> 改名
    (tmpdir / "1.Lecture session" / "Week2").mkdir(parents=True)
    (tmpdir / "1.Lecture session" / "Week2" / "lecture2.pdf").write_bytes(b"x" * 999)
    plan2 = build_plan(Course(id=9, name="Demo", code="CS9999"), tmpdir, items, {"files": {}})
    renamed = [p for p in plan2.files if p.action == "rename"]
    check("重名改名", len(renamed) == 1 and "(1)" in renamed[0].target.name,
          str([p.target.name for p in plan2.files]))
    txt = describe([plan])
    check("预览文本可渲染", "合计" in txt and "CS9999" in txt, txt[:80])
    shutil.rmtree(tmpdir, ignore_errors=True)


def s6b_local() -> None:
    from canvas_helper.filesync import build_plan

    print("\n[6b] 本地已有内容的识别（不重复下载 / 纯数字名不误判）")
    tmp2 = Path(tempfile.mkdtemp(prefix="ch_anchor_"))
    (tmp2 / "6.Others").mkdir()
    (tmp2 / "6.Others" / "0.Introduction.pptx").write_bytes(b"z" * 777)
    items2 = [
        Item(course_id=9, module_name="", title="Introduction.pptx", kind="File",
             file_id=111, file_name="Introduction.pptx", file_url="http://x/5", file_size=777),
        Item(course_id=9, module_name="", title="1.npy", kind="File",
             file_id=112, file_name="1.npy", file_url="http://x/6", file_size=111),
    ]
    plan3 = build_plan(Course(id=9, name="Demo", code="CS9999"), tmp2, items2, {"files": {}})
    got = {p.item.title: (p.action, p.note, str(p.target)) for p in plan3.files}
    print("      计划:", got)
    check("同内容换名 -> 已存在",
          got["Introduction.pptx"][0] == "exists"
          and got["Introduction.pptx"][1] == "本地已有同内容文件", str(got))
    check("纯数字名不被误判为已存在",
          got["1.npy"][0] == "download", str(got))
    (tmp2 / "1.Lecture session").mkdir()
    (tmp2 / "1.Lecture session" / "Introduction.pptx").write_bytes(b"z" * 777)
    plan4 = build_plan(Course(id=9, name="Demo", code="CS9999"), tmp2, items2[:1],
                       {"files": {}})
    check("同路径优先报本地已存在",
          plan4.files[0].action == "exists" and plan4.files[0].note == "本地已存在",
          plan4.files[0].note)
    shutil.rmtree(tmp2, ignore_errors=True)


def s6c_rename() -> None:
    from canvas_helper.filesync import build_plan

    print("\n[6c] 改名后缀 (1)/(2) 不会导致重复下载")
    tmp3 = Path(tempfile.mkdtemp(prefix="ch_rename_"))
    (tmp3 / "1.Lecture session").mkdir()
    # 本地：旧版 x.pptx（大小 A），上次同步存下的新版 x (1).pptx（大小 B）
    (tmp3 / "1.Lecture session" / "Lecture2-2.pptx").write_bytes(b"a" * 100)
    (tmp3 / "1.Lecture session" / "Lecture2-2 (1).pptx").write_bytes(b"b" * 250)
    item = Item(course_id=9, module_name="", title="Lecture2-2.pptx", kind="File",
                 file_id=121, file_name="Lecture2-2.pptx",
                 file_url="http://x/7", file_size=250,
                 folder_path="course files/Lecture")
    p6 = build_plan(Course(id=9, name="Demo", code="CS9999"), tmp3, [item], {"files": {}})
    pf = p6.files[0]
    print("      计划:", pf.action, pf.note, pf.target.name)
    check("改名后的副本算已存在", pf.action == "exists" and pf.target.name == "Lecture2-2 (1).pptx",
          f"{pf.action} {pf.target.name}")
    check("归一化忽略 (1) 后缀",
          anchor_key("Lecture2-2.pptx") == anchor_key("Lecture2-2 (1).pptx"))
    check("但不忽略非数字括号",
          anchor_key("3. Online Algorithms (new).pptx") != anchor_key("3. Online Algorithms.pptx"))
    # 内容真的变了（大小不同且没有副本）-> 仍然要下载并改名
    (tmp3 / "1.Lecture session" / "Lecture3").mkdir()
    (tmp3 / "1.Lecture session" / "Lecture3" / "Lecture3.pptx").write_bytes(b"a" * 100)
    item2 = Item(course_id=9, module_name="", title="Lecture3.pptx", kind="File",
                  file_id=122, file_name="Lecture3.pptx",
                  file_url="http://x/8", file_size=999,
                  folder_path="course files/Lecture")
    p7 = build_plan(Course(id=9, name="Demo", code="CS9999"), tmp3, [item2], {"files": {}})
    check("内容变了仍改名下载",
          p7.files[0].action == "rename" and p7.files[0].target.name == "Lecture3 (1).pptx",
          f"{p7.files[0].action} {p7.files[0].target.name}")
    shutil.rmtree(tmp3, ignore_errors=True)


def s7_extras(c: Course, drafts: list) -> None:
    from canvas_helper.extras import build_ics, update_grade_file, weekly_report

    print("\n[7] 附加功能：GradeNeed / .ics / 每周摘要")
    gdir = Path(tempfile.mkdtemp(prefix="ch_grade_"))
    gdir.mkdir(exist_ok=True)
    (gdir / "GradeNeed.txt").write_text("本课程成绩需要\nExam+Assignment\n", encoding="utf-8")
    update_grade_file(gdir, c, {"current": 0.785, "rows": [
        type("R", (), {"label": "A1: 85 / 100"})(),
        type("R", (), {"label": "A2: 未出分 / 50"})(),
    ]})
    txt1 = (gdir / "GradeNeed.txt").read_text(encoding="utf-8")
    check("GradeNeed 保留原文", "本课程成绩需要" in txt1, txt1[:60])
    check("写入总评分", "78.5%" in txt1, txt1)
    update_grade_file(gdir, c, {"current": 0.8, "rows": []})
    txt2 = (gdir / "GradeNeed.txt").read_text(encoding="utf-8")
    check("成绩块不重复追加", txt2.count("Canvas 成绩") == 1, str(txt2.count("Canvas 成绩")))
    check("刷新为最新", "80.0%" in txt2, txt2)

    ics = build_ics(drafts)
    check("ics 头尾", ics.startswith("BEGIN:VCALENDAR") and ics.endswith("END:VCALENDAR\r\n"))
    check("ics 两条事件", ics.count("BEGIN:VEVENT") == 2, str(ics.count("BEGIN:VEVENT")))
    check("ics 含提前一天提醒", "TRIGGER:-P1D" in ics)
    check("ics 换行 CRLF", "\r\n" in ics)

    rep = weekly_report(gdir, c, [gdir / "x.pdf"], [], drafts)
    body = rep.read_text(encoding="utf-8")
    check("周报含截止项", "CS0001" in body and "本周截止" in body, body[:80])
    check("周报文件名规范", rep.name.startswith("本周摘要-"), rep.name)
    shutil.rmtree(gdir, ignore_errors=True)


def s8_auth() -> None:
    from canvas_helper import paths as _paths
    from canvas_helper.runner import _relogin_headless

    print("\n[8] 登录过期不再静默失败")
    check("默认弹窗自愈", DEFAULT_CONFIG["auth"]["popup_on_expiry"] is True)
    check("默认弹窗 180 秒", DEFAULT_CONFIG["auth"]["popup_timeout_s"] == 180)

    # 标记的写入 / 清除 + headless 同步的契约：先备份真实 state.json，跑完原样还原
    state_bak = _paths.STATE_PATH.read_bytes() if _paths.STATE_PATH.exists() else None
    try:
        _paths.clear_auth_needed()
        check("无标记时清除返回 False", _paths.clear_auth_needed() is False)
        _paths.mark_auth_needed("sync --headless")
        got = _paths.load_state().get("auth_needed") or {}
        check("过期标记写进状态", got.get("source") == "sync --headless", str(got))
        check("标记带时间戳", bool(got.get("at")), str(got))
        check("清除标记生效", _paths.clear_auth_needed() is True)
        check("清除后状态里没有残留", not _paths.load_state().get("auth_needed"))

        # 真正的契约：后台同步发现过期 -> 必须置 auth_required 并留下标记，
        # 而不是只往 sync.log 里丢一行没人看的 warning
        from canvas_helper.runner import run_sync

        class _DeadSession:
            """登录态已失效的会话桩。"""

            def start(self, headless: bool = True) -> None:
                pass

            def is_logged_in(self) -> bool:
                return False

        res8 = run_sync(
            cfg={"auth": {"popup_on_expiry": False, "popup_timeout_s": 180},
                 "selected_courses": []},
            session=_DeadSession(), headless=True, progress=lambda m: None,
        )
        check("后台同步置 auth_required", res8.auth_required is True, str(res8.errors))
        check("后台同步留下过期标记",
              (_paths.load_state().get("auth_needed") or {}).get("source")
              == "sync --headless",
              str(_paths.load_state().get("auth_needed")))
        check("错误信息人话可读",
              any("登录已过期" in e for e in res8.errors), str(res8.errors))
        check("退出码可区分（auth_required 字段存在）",
              hasattr(res8, "auth_required"))
    finally:
        if state_bak is None:
            _paths.STATE_PATH.unlink(missing_ok=True)
        else:
            _paths.STATE_PATH.write_bytes(state_bak)

    # 后台自愈：是否弹窗、弹多久、没有交互会话时会不会白等
    class _Stub:
        def __init__(self, ok: bool) -> None:
            self.ok = ok
            self.timeouts: list[int] = []

        def ensure_login(self, on_status=None, timeout_s: int = 1200) -> bool:
            self.timeouts.append(timeout_s)
            return self.ok

    # 会话判定：0 = 服务（没有桌面），1+ = 交互会话
    from canvas_helper.runner import _has_interactive_session, _probe_session_id
    check("会话 0（服务）判为不可见", _has_interactive_session(0) is False)
    check("会话 1（交互）判为可见", _has_interactive_session(1) is True)
    check("会话 4（RDP / 多会话）判为可见", _has_interactive_session(4) is True)
    if os.name == "nt":
        _sid = _probe_session_id()
        check("能探测到本机会话号", _sid is not None, f"sid={_sid!r}")

    _log: list[str] = []
    say = _log.append
    env_bak = os.environ.get("SESSIONNAME")
    import canvas_helper.runner as _runner

    orig_probe = _runner._has_interactive_session
    try:
        off = {"auth": {"popup_on_expiry": False, "popup_timeout_s": 99}}
        s1 = _Stub(True)
        check("配置关掉时不自愈", _relogin_headless(off, s1, say) is False)
        check("关闭时根本不碰会话", not s1.timeouts, str(s1.timeouts))

        _runner._has_interactive_session = lambda: True  # 固定在交互会话
        on = {"auth": {"popup_on_expiry": True, "popup_timeout_s": 123}}
        s2 = _Stub(True)
        check("配置开启时尝试自愈", _relogin_headless(on, s2, say) is True)
        check("按配置的超时弹窗", s2.timeouts == [123], str(s2.timeouts))

        s3 = _Stub(False)
        check("用户没登录则如实返回失败",
              _relogin_headless(on, s3, say) is False)
        check("失败路径同样采用配置超时", s3.timeouts == [123], str(s3.timeouts))

        # 超时下限：配成 1 秒也得抬到 10 秒，免得弹窗一闪而过没人来得及点
        s3b = _Stub(False)
        tiny = {"auth": {"popup_on_expiry": True, "popup_timeout_s": 1}}
        _relogin_headless(tiny, s3b, say)
        check("超时下限抬到 10 秒", s3b.timeouts == [10], str(s3b.timeouts))

        # 回归：计划任务环境里 SESSIONNAME 是 None，但确实在交互会话 -> 必须弹。
        # 拿 SESSIONNAME 判定的老写法在这里返回 False，正是实机踩到的 bug。
        os.environ.pop("SESSIONNAME", None)
        s5 = _Stub(True)
        check("计划任务环境（无 SESSIONNAME）仍然弹窗（回归）",
              _relogin_headless(on, s5, say) is True and s5.timeouts == [123],
              str(s5.timeouts))

        # 服务 / 会话 0：没有桌面，弹了也没人看得见，别白等满超时
        _runner._has_interactive_session = lambda: False
        s4 = _Stub(True)
        check("无交互会话时不白等",
              _relogin_headless(on, s4, say) is False and not s4.timeouts,
              str(s4.timeouts))
        check("每种情况都有进度提示", len(_log) == 6, str(_log))
    finally:
        _runner._has_interactive_session = orig_probe
        if env_bak is None:
            os.environ.pop("SESSIONNAME", None)
        else:
            os.environ["SESSIONNAME"] = env_bak


def s9_packaging() -> None:
    from canvas_helper import __version__ as _ver

    print("\n[9] 打包元数据 pyproject.toml")
    pt_path = ROOT / "pyproject.toml"
    check("pyproject.toml 存在", pt_path.is_file(), str(pt_path))
    pt = pt_path.read_text(encoding="utf-8") if pt_path.is_file() else ""
    ver_line = next((ln.strip() for ln in pt.splitlines()
                     if ln.strip().startswith("version =")), "")
    got_ver = ver_line.split('"')[1] if '"' in ver_line else ""
    check("版本号与 __version__ 一致", got_ver == _ver, f"{got_ver!r} vs {_ver!r}")
    check("声明 requires-python >=3.9", 'requires-python = ">=3.9"' in pt)
    # mypy 从 1.19 起不再支持把 3.9 当目标，只能按 3.10 检查；ruff 仍按 3.9 判。
    check("静态检查目标不高于声明地板",
          'target-version = "py39"' in pt and 'python_version = "3.10"' in pt)
    check("依赖与原 requirements 同源",
          '"playwright>=1.49"' in pt and '"tzdata>=2024.1"' in pt)
    check("控制台入口指向 __main__", "canvas_helper.__main__:main" in pt)
    check("GUI 入口点存在", "canvas_helper.gui:main" in pt)
    check("只打包 canvas_helper", 'packages = ["canvas_helper"]' in pt)
    check("声明 MIT 且 LICENSE 存在",
          'license = "MIT"' in pt and (ROOT / "LICENSE").is_file())
    check("setup.py 垫片存在（旧 pip 的 editable 安装只认它）",
          (ROOT / "setup.py").is_file())
    spec = ROOT / "CanvasHelper.spec"
    spec_txt = spec.read_text(encoding="utf-8") if spec.is_file() else ""
    check("打包 spec 入库并带上 playwright driver 与 tzdata",
          "collect_all('playwright')" in spec_txt
          and "collect_data_files('tzdata')" in spec_txt, str(spec))
    # v1.2.0 的 exe 就是这么坏的：分析器只搜 scripts\，而 editable 安装不往
    # sys.path 加目录，于是 canvas_helper 整个没进包，构建却一路绿灯。
    check("打包 spec 把仓库根目录放进 pathex",
          "pathex = [str(HERE)]" in spec_txt and "pathex=pathex" in spec_txt)
    check("推 tag 自动发布",
          (ROOT / ".github" / "workflows" / "release.yml").is_file())
    # 3.9 地板：datetime.UTC 是 3.11 才有的常量。mypy 按 3.10 检查抓不到它，
    # 只有真机 3.9 会炸——所以本地静态扫一遍，别等 CI 才发现。
    offenders = [p.name for p in sorted(ROOT.glob("canvas_helper/*.py"))
                 if re.search(r"from datetime import[^\n]*\bUTC\b",
                              p.read_text(encoding="utf-8"))]
    check("没有用到 3.11 才有的 datetime.UTC", not offenders, str(offenders))
    # 静默回归：状态栏若排在标签页之后 pack，会被 notebook 的请求高度（设置页
    # 32 行撑出的 760px）挤到 3px——进度条与状态文字直接看不见，但不报任何错。
    gui_src = (ROOT / "canvas_helper" / "gui.py").read_text(encoding="utf-8")
    check("状态栏先于标签页 pack（否则进度条被挤掉）",
          gui_src.find("bottom.pack(") < gui_src.find("self.nb.pack("))
    # 更新公告窗是从 _pump 里弹的：若在里面 wait_window() 开嵌套事件循环，
    # 队列里的下载进度消息要等窗口关掉才处理，进度条会一直停在「正在下载…」。
    update_section = gui_src.split("def do_show_update", 1)
    check("更新公告窗不用嵌套事件循环（否则下载进度停摆）",
          len(update_section) == 2
          and "win.wait_window()" not in update_section[1].split("def _on_update_ready")[0])

    # ---- 免安装分发：装完就能双击运行，不靠任何 .bat / .ps1 安装脚本 ----
    bats = [p.name for p in ROOT.rglob("*.bat")]
    check("仓库里没有 .bat 启动脚本", not bats, str(bats))
    check("仓库里没有 make_shortcut.ps1（快捷方式改由界面按钮创建）",
          not (ROOT / "scripts" / "make_shortcut.ps1").exists())
    pyw = ROOT / "CanvasHelper.pyw"
    check("源码双击入口 CanvasHelper.pyw 存在", pyw.is_file())
    if pyw.is_file():
        pyw_src = pyw.read_text(encoding="utf-8")
        try:
            compile(pyw_src, str(pyw), "exec")
            ok_pyw = True
        except SyntaxError:
            ok_pyw = False
        check("CanvasHelper.pyw 语法正确", ok_pyw)
        check("CanvasHelper.pyw 启动失败时会弹消息框",
              "messagebox.showerror" in pyw_src)
    gui_scripts = pt.split("[project.gui-scripts]", 1)
    check("GUI 入口是无控制台的 gui-scripts",
          len(gui_scripts) == 2 and "canvas-helper-gui" in gui_scripts[1].split("\n[", 1)[0])
    check("打包 spec 带 exe 版本资源（文件属性里能看到版本号）",
          "version=VERSION_INFO" in spec_txt and "VSVersionInfo(" in spec_txt)
    release_yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    check("zip 里带 README.txt 并校验包内容",
          "dist/README.txt" in release_yml
          and 'CanvasHelper/CanvasHelper.exe"} - names' in release_yml)
    check("Release 正文自动追加「下载哪个文件」指引",
          "下载哪个文件" in release_yml and "CanvasHelper-v{ver}-win64.zip" in release_yml)


def s10_extract() -> None:
    import zipfile

    from canvas_helper import extract

    print("\n[10] 压缩包判定与解压（内容包解 / 数据包留 / 防 zip-slip）")
    cfg = {"extract": True, "skip_junk": True,
           "max_total_bytes": 2147483648,
           "data_name_hints": ["data", "dataset", "train", "test"]}
    tmp = Path(tempfile.mkdtemp(prefix="ch_zip_"))
    lec = tmp / "1.Lecture session"
    (lec / "Lecture3").mkdir(parents=True)
    st: dict = {"archives": {}}

    # 1) 内容包（含 ipynb + 图）：包名 == 所在目录名 -> 解到当前目录
    z1 = lec / "Lecture3" / "Lecture3.zip"
    with zipfile.ZipFile(z1, "w") as zf:
        zf.writestr("Lecture3a.ipynb", "{}")
        zf.writestr("imgs/pic.png", b"\x89PNG\r\n")
        zf.writestr("__MACOSX/._Lecture3a.ipynb", b"junk")
        zf.writestr(".DS_Store", b"j")
    rec = extract.process(z1, st, cfg)
    check("内容包判为解压", rec is not None and rec.kind == "extracted", repr(rec))
    check("解到父目录（包名==目录名）", (lec / "Lecture3" / "Lecture3a.ipynb").is_file())
    check("子目录保持", (lec / "Lecture3" / "imgs" / "pic.png").is_file())
    check("跳过 macOS 垃圾", not (lec / "Lecture3" / "__MACOSX").exists()
          and not (lec / "Lecture3" / ".DS_Store").exists(), str(list(lec.rglob("*"))[:6]))
    check("压缩包保留", z1.is_file())

    # 2) 幂等：state 记过 -> 再跑不解第二次（删掉解出的文件也不会被还原）
    (lec / "Lecture3" / "Lecture3a.ipynb").unlink(missing_ok=True)
    rec2 = extract.process(z1, st, cfg)
    check("已处理的包不再重复解压",
          rec2 is not None and not rec2.changed
          and not (lec / "Lecture3" / "Lecture3a.ipynb").exists(), repr(rec2))

    # 3) 换一份干净 state -> 重新解压，但同名同大小的条目跳过、不覆盖
    rec3 = extract.process(z1, {"archives": {}}, cfg)
    check("state 才是幂等依据、磁盘同名同大小不覆盖",
          rec3 is not None and rec3.kind == "extracted"
          and (lec / "Lecture3" / "Lecture3a.ipynb").is_file(), repr(rec3))

    # 4) 包名命中数据关键词 -> 不解压（ipynb 要读的原始数据）
    z2 = lec / "data.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("iris2.csv", "a,b\n1,2\n")
    rec4 = extract.process(z2, st, cfg)
    check("数据包不解压", rec4 is not None and rec4.kind == "data", repr(rec4))
    check("数据没有铺开", not (lec / "iris2.csv").exists()
          and not (lec / "data").exists())

    # 5) 构成判定：全是 csv、没有代码 -> 数据包
    z3 = lec / "week4.zip"
    with zipfile.ZipFile(z3, "w") as zf:
        for i in range(5):
            zf.writestr(f"t{i}.csv", "x,y\n1,2\n")
    rec5 = extract.process(z3, st, cfg)
    check("纯数据构成判为数据包", rec5 is not None and rec5.kind == "data", repr(rec5))
    check("week4 目录未产生", not (lec / "week4").exists())

    # 6) 名称关键词优先于构成：数据集名字 + 里头有 notebook 也不解
    z4 = lec / "dataset.zip"
    with zipfile.ZipFile(z4, "w") as zf:
        zf.writestr("nb.ipynb", "{}")
    rec6 = extract.process(z4, st, cfg)
    check("包名关键词压过构成", rec6 is not None and rec6.kind == "data", repr(rec6))

    # 6b/6c) 真实误判回归：README.md 与装机脚本 .sh 都不算内容信号
    # （本机 4.Projects/Dataset/archive.zip、MTNT.1.1.tar.gz 就是这么误判的）
    z4b = lec / "bundle.zip"
    with zipfile.ZipFile(z4b, "w") as zf:
        zf.writestr("README.md", "# 数据说明")
        zf.writestr("train.csv", "x,y\n1,2\n")
        zf.writestr("test.csv", "x,y\n1,2\n")
    rec6b = extract.process(z4b, st, cfg)
    check("README.md 不算内容信号", rec6b is not None and rec6b.kind == "data",
          repr(rec6b))
    z4c = lec / "corpus.zip"
    with zipfile.ZipFile(z4c, "w") as zf:
        zf.writestr("setup.sh", "echo hi")
        zf.writestr("corpus.tsv", "a\tb\n")
    rec6c = extract.process(z4c, st, cfg)
    check("装机脚本 .sh 不算内容信号", rec6c is not None and rec6c.kind == "data",
          repr(rec6c))

    # 7) zip-slip：../ 越界路径必须被拒绝（内容包得带代码/讲义后缀才会被解）
    z5 = lec / "safe.zip"
    with zipfile.ZipFile(z5, "w") as zf:
        zf.writestr("../evil.txt", "boom")
        zf.writestr("ok.md", "fine")
    rec7 = extract.process(z5, st, cfg)
    check("正常条目照常解出", (lec / "safe" / "ok.md").is_file(), repr(rec7))
    check("拒绝 zip-slip 越界", not (lec / "evil.txt").exists()
          and not (tmp / "evil.txt").exists())

    # 8) 不支持的格式：如实标注，不报错
    rar = lec / "tool.rar"
    rar.write_bytes(b"Rar!\x1a\x07\x00")
    rec8 = extract.process(rar, st, cfg)
    check("rar 标注为不支持", rec8 is not None and rec8.kind == "unsupported", repr(rec8))

    # 9) 配置关掉 -> 完全不碰
    off = dict(cfg, extract=False)
    z6 = lec / "Lecture7.zip"
    with zipfile.ZipFile(z6, "w") as zf:
        zf.writestr("nb.ipynb", "{}")
    check("配置关闭时不处理", extract.process(z6, st, off) is None
          and not (lec / "Lecture7").exists())

    # 10) 非压缩包 / 预览文案
    check("非压缩包直接返回", extract.process(lec / "a.pdf", st, cfg) is None)
    check("预览标注数据包", "数据包" in extract.plan_note(z2, st, cfg),
          extract.plan_note(z2, st, cfg))
    check("预览标注已解压", "已解压" in extract.plan_note(z1, st, cfg),
          extract.plan_note(z1, st, cfg))
    check("未下载的包标注待判定", "下载后" in extract.plan_note(lec / "Lecture6.zip", st, cfg))
    check("非压缩包无标注", extract.plan_note(lec / "a.pdf", st, cfg) == "")

    shutil.rmtree(tmp, ignore_errors=True)


def s11_tidy() -> None:
    from canvas_helper import tidy

    print("\n[11] 讲义整理：先预览再移动（只移动不删除）")
    tmp = Path(tempfile.mkdtemp(prefix="ch_tidy_"))
    course = tmp / "CS0003 - Machine Learning"
    lec = course / "1.Lecture session"
    (lec / "Lecture3").mkdir(parents=True)
    (lec / "Lecture3a.pdf").write_bytes(b"a" * 10)
    (lec / "Lecture3.zip").write_bytes(b"z" * 20)
    (lec / "data.zip").write_bytes(b"d" * 5)
    (lec / "jupyter-shortcuts.pdf").write_bytes(b"j" * 7)
    # 目标章节目录里已有同名但不同大小 -> 移动时改名
    (lec / "Lecture3" / "Lecture3a.pdf").write_bytes(b"b" * 3)

    plan = tidy.plan_course(course)
    moves = {m.src.name: m for m in plan.moves}
    txt = tidy.render([plan])
    print(txt)
    check("只动有章节号的文件", set(moves) == {"Lecture3a.pdf", "Lecture3.zip"},
          str(set(moves)))
    check("无编号散件保持原样",
          "data.zip" not in moves and "jupyter-shortcuts.pdf" not in moves)
    check("同名不同大小 -> 改名移动",
          moves["Lecture3a.pdf"].dest.name == "Lecture3a (1).pdf",
          moves["Lecture3a.pdf"].dest.name)
    check("zip 归入同名章节目录",
          moves["Lecture3.zip"].dest.parent.name == "Lecture3",
          str(moves["Lecture3.zip"].dest))
    check("预览文案列出去向", "Lecture3" in txt and "合计" in txt, txt[:160])

    state = {"files": {"file:9:x": {"path": str(lec / "Lecture3a.pdf"), "size": 10}},
             "archives": {str(lec / "Lecture3.zip"): {"kind": "extracted", "size": 20}}}
    rep = tidy.apply_plans([plan], state)
    check("移动 2 个、零失败", rep == {"moved": 2, "skipped": 0, "errors": 0}, str(rep))
    check("文件真的搬进章节目录",
          (lec / "Lecture3" / "Lecture3.zip").is_file()
          and not (lec / "Lecture3.zip").exists()
          and (lec / "Lecture3" / "Lecture3a (1).pdf").is_file())
    check("state 路径跟着回写（含改名后的去向）",
          state["files"]["file:9:x"]["path"]
          == str(lec / "Lecture3" / "Lecture3a (1).pdf"),
          state["files"]["file:9:x"]["path"])
    check("解压记录键随迁移",
          str(lec / "Lecture3" / "Lecture3.zip") in state["archives"]
          and str(lec / "Lecture3.zip") not in state["archives"])
    check("原地不动的文件还在",
          (lec / "data.zip").is_file() and (lec / "jupyter-shortcuts.pdf").is_file())
    again = tidy.plan_course(course)
    check("整理后无残留待移动", again.todo == [], str(again.moves))
    check("无可整理时给明确文案",
          tidy.render([again]) == "没有需要整理的讲义文件：各章内容都已在章节目录里。",
          tidy.render([again]))

    shutil.rmtree(tmp, ignore_errors=True)


def s12_discussions() -> None:
    from canvas_helper import filesync
    from canvas_helper.canvas_api import CanvasAPI
    from canvas_helper.filesync import build_plan, execute_plan

    print("\n[12] 讨论区：Colab 链接下载 + 帖子快照")
    # 链接解析：colab / github blob / raw 直链，剥 query、去重
    msg = (
        '<a href="https://colab.research.google.com/github/prof/cs5494/blob/main/'
        'cs5494_tutorial_1_MNIST.ipynb?usp=sharing">a</a> '
        "https://github.com/prof/cs5494/blob/main/cs5494_tutorial_1_MNIST.ipynb "
        "https://raw.githubusercontent.com/prof/cs5494/main/notes.pdf"
    )
    links = CanvasAPI._link_targets(msg)
    check("colab 转 raw 直链且剥掉 query",
          links[0] == ("https://raw.githubusercontent.com/prof/cs5494/main/"
                       "cs5494_tutorial_1_MNIST.ipynb", "cs5494_tutorial_1_MNIST.ipynb"),
          str(links))
    check("同链接去重（colab 与 blob 等价）", len(links) == 2, str(links))
    check("已是 raw 的直链原样收录",
          links[1][0] == "https://raw.githubusercontent.com/prof/cs5494/main/notes.pdf",
          str(links))
    dlinks = CanvasAPI._link_targets(
        "<p>https://colab.research.google.com/drive/"
        "1-CNfyNMUbrML8sXFCYPt3JBq9QGrVbTa?usp=sharing</p>")
    check("drive 型 colab 转下载直链、文件名留空由帖子标题补",
          dlinks == [("https://drive.google.com/uc?export=download"
                      "&id=1-CNfyNMUbrML8sXFCYPt3JBq9QGrVbTa", "")], str(dlinks))

    tmp = Path(tempfile.mkdtemp(prefix="ch_discuss_"))
    course = tmp / "CS0004 - Generative AI"
    (course / "2.Tutorial session").mkdir(parents=True)
    c = Course(id=94, name="Generative AI", code="CS0004")
    items = [
        Item(course_id=94, module_name="Week 1 Tutorial",
             title="cs5494_tutorial_1_MNIST.ipynb", kind="File",
             file_name="cs5494_tutorial_1_MNIST.ipynb",
             file_url="https://raw.githubusercontent.com/prof/cs5494/main/"
                      "cs5494_tutorial_1_MNIST.ipynb",
             extra={"link_source": "discussion"}),
        Item(course_id=94, module_name="", title="Week 1 Tutorial", kind="Discussion",
             html_url="https://canvas.example.edu/courses/94/discussion_topics/71",
             file_id=71, extra={"message": "<p>colab link above</p>", "topic_id": 71}),
    ]
    st = {"files": {}}
    plan = build_plan(c, course, items, st)
    names = [str(p.target.relative_to(course)).replace("\\", "/") for p in plan.files]
    print("      计划:", names)
    check("笔记本按帖标题分类进 2.Tutorial session",
          "2.Tutorial session/cs5494_tutorial_1_MNIST.ipynb" in names, str(names))
    check("帖子存快照为 html",
          "2.Tutorial session/Week 1 Tutorial.html" in names, str(names))
    check("预览标注讨论区来源",
          any(p.note == "讨论区链接" for p in plan.files)
          and any(p.note == "讨论帖快照" for p in plan.files),
          str([(p.target.name, p.note) for p in plan.files]))

    class _FakeAPI:
        """下载桩：只记录调用，返回一个最小 ipynb。"""

        def __init__(self) -> None:
            self.urls: list[str] = []

        def download(self, url: str):
            self.urls.append(url)
            return b'{"cells": []}', "", "application/json"

    # execute_plan 收尾会 flush state -> smoke 直跑时不能碰真实 state.json
    old_flush = filesync._flush_state
    filesync._flush_state = lambda state: None
    try:
        fake = _FakeAPI()
        stats = execute_plan([plan], fake, st)  # type: ignore[arg-type]
        check("执行：下载 1 + 快照 1",
              stats["downloaded"] == 1 and stats["pages"] == 1, str(stats))
        check("落盘内容正确",
              (course / "2.Tutorial session" / "cs5494_tutorial_1_MNIST.ipynb")
              .read_bytes().startswith(b"{")
              and (course / "2.Tutorial session" / "Week 1 Tutorial.html").is_file())
        check("快照正文不能丢（Discussion 走 extra.message）",
              "colab link above" in (course / "2.Tutorial session"
                                     / "Week 1 Tutorial.html").read_text(encoding="utf-8"))
        plan2 = build_plan(c, course, items, st)
        check("二次同步全部已存在",
              all(p.action == "exists" for p in plan2.files),
              str([(p.target.name, p.action) for p in plan2.files]))
        fake2 = _FakeAPI()
        execute_plan([plan2], fake2, st)  # type: ignore[arg-type]
        check("二次执行零下载调用", fake2.urls == [], str(fake2.urls))
    finally:
        filesync._flush_state = old_flush

    shutil.rmtree(tmp, ignore_errors=True)


def s13_universal() -> None:
    """[13] 跨院校通用化：课程代码提取 + 目录匹配 + 时区。"""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    print("\n[13] 跨院校通用化（课程代码 / 目录匹配 / 时区）")

    # ---- 课程代码提取：各校形态 ----
    cases = [
        # (course_code, 课程名, 期望)
        ("202609CS5187", "CS5187 Vision and Image", "CS5187"),
        ("202609CS5489", "CS5489 Machine Learning:Algorithms &Apns", "CS5489"),
        ("MATH 101", "Calculus I", "MATH101"),
        ("ECON2201", "Macroeconomics", "ECON2201"),
        ("BIO-220", "Cell Biology", "BIO220"),
        ("CS 114", "Programming", "CS114"),
        ("", "Introduction to CS101", "CS101"),
        ("", "MATH 202 - Advanced Calculus", "MATH202"),
        # 多段码不拆开（数字属于编码本身，不是课程号）
        ("SD_GEN_SFTY_CS_MCS_2026", "Safety Training for MSc Students (CS)",
         "SD_GEN_SFTY_CS_MCS_2"),
        ("SD_ANTI_DECEPTION_2026-27_0", "防诈反诈问卷（已完成）", "SD_ANTI_DECEPTION_20"),
    ]
    for code, name, want in cases:
        got = course_code(code, name)
        check(f"课程代码 {code or name!r} -> {want}", got == want, f"got={got!r}")

    # 课程代码在 Canvas 的 Course.short 上同样生效
    c1 = Course(id=1, name="CS5187 Vision and Image", code="202609CS5187")
    check("Course.short 剥掉学期前缀", c1.short == "CS5187", c1.short)
    c2 = Course(id=2, name="Calculus I", code="MATH 101")
    check("Course.short 兼容别的学校写法", c2.short == "MATH101", c2.short)

    # 自定义正则（配置项 course_code_pattern）：有捕获组取捕获组，无捕获组取整段
    check("自定义正则取捕获组",
          course_code("XY-9999", "任意课名", pat=r"XY-(\d+)") == "9999",
          course_code("XY-9999", "任意课名", pat=r"XY-(\d+)"))
    check("自定义正则无捕获组取整段",
          course_code("XY-9999", "任意课名", pat=r"XY-\d+") == "XY9999",
          course_code("XY-9999", "任意课名", pat=r"XY-\d+"))
    check("非法正则不炸", course_code("MATH 101", "", pat=r"([unclosed") == "MATH101",
          course_code("MATH 101", "", pat=r"([unclosed"))

    # ---- 本地目录匹配：别的学校目录名也要认 ----
    check("目录名取课程代码",
          dir_key("MATH 101 - Calculus") == "MATH101", dir_key("MATH 101 - Calculus"))
    check("目录名无代码则用整名",
          dir_key("Introduction to CS") == "INTRODUCTION TO CS",
          dir_key("Introduction to CS"))

    tmpdir = Path(tempfile.mkdtemp(prefix="ch_univ_"))
    try:
        for d in ("MATH 101 - Calculus", "ECON 2201 - Macroeconomics", "CS5187 - Vision and Image"):
            (tmpdir / d).mkdir(parents=True)
        folders = course_dirs(tmpdir)
        check("识别出 3 门课", len(folders) == 3, str(list(folders)))
        check("MATH101 匹配到 MATH 101 - Calculus",
              (match_folder("MATH101", folders) or Path()).name == "MATH 101 - Calculus",
              str(match_folder("MATH101", folders)))
        check("MATH 101（带空格）同样匹配",
              (match_folder("MATH 101", folders) or Path()).name == "MATH 101 - Calculus",
              str(match_folder("MATH 101", folders)))
        check("ECON2201 匹配到 ECON 2201 - Macroeconomics",
              (match_folder("ECON2201", folders) or Path()).name == "ECON 2201 - Macroeconomics",
              str(match_folder("ECON2201", folders)))
        check("CS5187 匹配到 CS5187 - Vision and Image",
              (match_folder("CS5187", folders) or Path()).name == "CS5187 - Vision and Image",
              str(match_folder("CS5187", folders)))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 时区：截止时间按配置时区换算 ----
    utc_due = datetime.fromisoformat("2026-11-20T15:59:00+00:00")
    check("未配置时区 -> 跟随系统本地",
          to_local(utc_due).utcoffset() == utc_due.astimezone().utcoffset(),
          str(to_local(utc_due)))
    check("配置时区生效（+08:00）",
          to_local(utc_due, ZoneInfo("Asia/Hong_Kong")).strftime("%z") == "+0800",
          str(to_local(utc_due, ZoneInfo("Asia/Hong_Kong"))))
    check("配置时区生效（美东 -05:00）",
          to_local(utc_due, ZoneInfo("America/New_York")).strftime("%z") == "-0500",
          str(to_local(utc_due, ZoneInfo("America/New_York"))))
    check("配置时区生效（内地 +08:00）",
          to_local(utc_due, ZoneInfo("Asia/Shanghai")).strftime("%z") == "+0800",
          str(to_local(utc_due, ZoneInfo("Asia/Shanghai"))))
    # 同一绝对时刻在任何时区下换算结果一致（不该因时区而错判截止时间）
    check("换时区不改变绝对时刻",
          to_local(utc_due, ZoneInfo("America/New_York")).astimezone(timezone.utc) == utc_due,
          str(to_local(utc_due, ZoneInfo("America/New_York"))))


def s14_schools() -> None:
    """[14] 院校清单与地址归一化。"""
    from canvas_helper.schoolpicker import normalize_home
    from canvas_helper.schools import SCHOOLS, regions, search

    print("\n[14] 院校清单（选学校 -> 进该校的 Canvas）")
    check("清单非空", len(SCHOOLS) > 0, str(len(SCHOOLS)))
    check("清单足够大", len(SCHOOLS) >= 50, str(len(SCHOOLS)))
    check("地区已归类", len(regions()) >= 3, str(regions()))

    # 每条都必须形如 https://主机（不含路径 / 结尾斜杠）
    bad = [f"{n} {u}" for n, u, _r in SCHOOLS
           if not u.startswith("https://") or "/" in u[len("https://"):]
           or u.endswith("/") or " " in u]
    check("地址格式统一（https + 只有主机 + 无结尾斜杠）", not bad, str(bad[:3]))

    # URL 不重复
    urls = [u.lower() for _n, u, _r in SCHOOLS]
    check("地址无重复", len(urls) == len(set(urls)),
          f"{len(urls)} 条 / {len(set(urls))} 唯一")

    # 名称不重复（同名不同址的应已合并）
    names = [n for n, _u, _r in SCHOOLS]
    check("院校名无重复", len(names) == len(set(names)),
          f"{len(names)} 条 / {len(set(names))} 唯一")

    # 搜索：按名称 / 按域名 / 查不到
    check("按名称搜索命中",
          any("香港城市大学" in n for n, _u, _r in search("香港城市大学")),
          str(search("香港城市大学")[:1]))
    check("按域名搜索命中",
          any("canvas.umich.edu" in u for _n, u, _r in search("umich")),
          str(search("umich")[:1]))
    check("搜不到时返回空", search("zzz-不存在的学校-xyzzy") == [],
          str(search("zzz-不存在的学校-xyzzy")))
    check("空关键词返回全部", len(search("")) == len(SCHOOLS), str(len(search(""))))

    # 地址归一化：用户手填的常见写法都要收拾干净
    cases = [
        ("canvas.example.edu", "https://canvas.example.edu"),
        ("https://canvas.example.edu/", "https://canvas.example.edu"),
        ("  canvas.example.edu  ", "https://canvas.example.edu"),
        ("https://canvas.example.edu/login", "https://canvas.example.edu"),
        ("https://canvas.example.edu/users/auth", "https://canvas.example.edu"),
        ("canvas.example.edu?x=1", "https://canvas.example.edu"),
        ('"https://canvas.example.edu/"', "https://canvas.example.edu"),
        ("", ""),
    ]
    for raw, want in cases:
        got = normalize_home(raw)
        check(f"地址归一化 {raw!r}", got == want, f"got={got!r}")
    check("保留 http 协议",
          normalize_home("http://canvas.example.edu") == "http://canvas.example.edu",
          normalize_home("http://canvas.example.edu"))


def s15_schoollist() -> None:
    """[15] 院校清单的在线更新与本地缓存。"""
    from canvas_helper import schoollist
    from canvas_helper.schools import SCHOOLS

    print("\n[15] 院校清单在线更新与缓存")

    # --- 合并规则：内置优先 + 在线补充，按地址去重 ---
    online = [("某在线大学 Online U", "https://online-u.example.edu"),
              ("某内置大学 Built-in U", "https://canvas.cityu.edu.hk")]
    merged = schoollist.merge_builtin(online)
    urls = [u.lower() for _n, u, _r in merged]
    check("合并后无重复地址", len(urls) == len(set(urls)),
          f"{len(urls)} 条 / {len(set(urls))} 唯一")
    check("内置条目全部保留", all(u.lower() in urls for _n, u, _r in SCHOOLS),
          str(len(SCHOOLS)))
    check("在线新增条目被补进", any(u == "https://online-u.example.edu" for _n, u, _r in merged),
          str(len(merged)))
    # 同地址两边都有 -> 保留内置的名称
    hit = [n for n, u, _r in merged if u == "https://canvas.cityu.edu.hk"]
    check("同地址保留内置名称（在线只补它没有的）",
          hit == [SCHOOLS[0][0]], str(hit))

    # --- 缓存读写与损坏降级 ---
    cache = schoollist.CACHE_PATH
    backup = cache.read_bytes() if cache.is_file() else None
    try:
        entries = [("缓存大学 A", "https://a.example.edu"),
                   ("缓存大学 B", "https://b.example.edu")]
        check("写缓存成功", schoollist._write_cache(entries), "")
        read_back, fetched = schoollist._read_cache()
        check("缓存能读回来", read_back == entries, str(read_back))
        check("缓存带时间戳", fetched > 0, str(fetched))
        check("缓存年龄可计算", schoollist.cache_age_days() >= 0,
              str(schoollist.cache_age_days()))
        check("刚写完不需要刷新", not schoollist.needs_refresh(), "")
        # 损坏的缓存 -> 回落到内置清单，不崩
        cache.write_text("{ 这不是合法 JSON", encoding="utf-8")
        read_back2, fetched2 = schoollist._read_cache()
        check("坏缓存不抛异常且读作空", read_back2 == [] and fetched2 == 0.0,
              str(read_back2))
        check("坏缓存时只剩内置清单",
              len(schoollist.current_schools()) == len(SCHOOLS),
              str(len(schoollist.current_schools())))
    finally:
        if backup is not None:
            cache.write_bytes(backup)
        else:
            cache.unlink(missing_ok=True)

    # --- 源不可达时不崩，且降级到缓存 ---
    old_url = schoollist.OFFICIAL_URL
    try:
        schoollist.OFFICIAL_URL = "https://invalid.invalid/nope"
        rep = schoollist.update(timeout=3.0)
        check("源不可达时返回失败而不是抛异常", rep.get("ok") is False, str(rep))
        check("失败时仍能用缓存/内置清单", len(schoollist.current_schools()) >= len(SCHOOLS),
              str(len(schoollist.current_schools())))
    finally:
        schoollist.OFFICIAL_URL = old_url

    # --- 过滤测试/演示租户 ---
    check("测试租户被过滤", schoollist._is_noise("09633076 instructure",
                                                 "https://mobileqa.instructure.com"))
    check("dev 租户被过滤", schoollist._is_noise("ACU-DEV", "https://acu-dev.instructure.com"))
    check("正常租户不被过滤", not schoollist._is_noise("Harvard University",
                                                      "https://canvas.harvard.edu"))
    check("官方地址是 https", schoollist.OFFICIAL_URL.startswith("https://"),
          schoollist.OFFICIAL_URL)

    # --- 地区推断 ---
    check("地区推断 .edu.hk", schoollist._guess_region("https://x.edu.hk") == "中国香港/澳门",
          schoollist._guess_region("https://x.edu.hk"))
    check("地区推断 instructure 归美国",
          schoollist._guess_region("https://x.instructure.com") == "美国",
          schoollist._guess_region("https://x.instructure.com"))

    # --- 搜索走的是合并后的清单（内置条目应在结果里） ---
    check("搜索能命中内置条目",
          any("香港城市大学" in n for n, _u, _r in schoollist.search_schools("香港城市大学")),
          "内置条目应在搜索结果里")
    check("按域名搜索命中",
          any("cityu.edu.hk" in u for _n, u, _r in schoollist.search_schools("cityu")),
          "应能按域名搜到")
    check("搜不到时返回空", schoollist.search_schools("zzz-不存在-xyzzy") == [],
          str(schoollist.search_schools("zzz-不存在-xyzzy")))


def s16_update() -> None:
    """[16] 版本校验、更新公告与一键替换（全部离线，fetcher 注入）。"""
    import zipfile
    from datetime import timedelta

    from canvas_helper import __version__ as ver
    from canvas_helper import updater
    from canvas_helper.__main__ import _SUBMODULES
    from canvas_helper.paths import now_local

    print("\n[16] 版本校验与更新公告")

    # --- 版本号比较：宁可漏报，也不能把脏 tag 当成更新 ---
    check("v 前缀与段数补齐",
          updater.parse_version("v1.4") == updater.parse_version("1.4.0"),
          f"{updater.parse_version('v1.4')} / {updater.parse_version('1.4.0')}")
    check("跨十位版本正确比较", updater.is_newer("1.10.0", "1.9.9"))
    check("同级小版本递增", updater.is_newer("1.4.1", "1.4.0"))
    check("相同版本不算更新", not updater.is_newer("v1.3.0", "1.3.0"))
    check("预发布低于同号正式版", not updater.is_newer("1.4.0-rc1", "1.4.0"))
    check("预发布高于旧正式版", updater.is_newer("1.4.0-rc1", "1.3.0"))
    check("构建元数据不算预发布",
          not updater.is_newer("1.3.0+build5", "1.3.0")
          and updater.is_newer("1.3.0+build5", "1.2.9"), "1.3.0+build5")
    check("看不懂的版本号不误报",
          updater.parse_version("abc") is None
          and not updater.is_newer("latest", "1.3.0"))

    tmp = Path(tempfile.mkdtemp(prefix="ch_update_"))
    old_cache, old_updates = updater.UPDATE_CACHE_PATH, updater.UPDATES_DIR
    updater.UPDATE_CACHE_PATH = tmp / "update_cache.json"
    updater.UPDATES_DIR = tmp / "updates"
    try:
        wheel = updater.Asset("canvas_helper-1.4.0-py3-none-any.whl",
                              "https://example.invalid/a.whl", 1000)
        z64 = updater.Asset("CanvasHelper-v1.4.0-win64.zip",
                            "https://example.invalid/a.zip", 4242, sha256="ab" * 32)

        def rel(version: str, notes: str = "", assets: tuple = ()) -> updater.Release:
            return updater.Release(
                tag=f"v{version}", version=version, title=f"CanvasHelper v{version}",
                notes=notes, url=f"https://example.invalid/releases/v{version}",
                published_at="2026-11-02T10:00:00Z", assets=list(assets),
            )

        r140 = rel("1.4.0", "### 新增 · 版本校验\n\n- **发现新版**会提示\n- 修了一个 `bug`",
                   assets=(wheel, z64))
        r131 = rel("1.3.1", "### 修复\n\n- 修了待办排序", assets=(wheel,))
        r130 = rel("1.3.0", "### 当前版本\n\n- 这一条不该出现在公告里")
        releases = [r140, r131, r130]

        status = updater.check("1.3.0", fetch=lambda: releases)
        check("联网检查成功", status.ok and not status.from_cache, str(status))
        check("最新版本正确", status.latest == "1.4.0", status.latest)
        check("落后期间的所有版本都算进来",
              [r.version for r in status.newer] == ["1.4.0", "1.3.1"],
              str([r.version for r in status.newer]))
        check("落后版本数", status.behind == 2, str(status.behind))
        check("选中 win64 zip 而不是 wheel",
              status.asset is not None and status.asset.name.endswith("win64.zip"),
              repr(status.asset))

        notes = status.notes_text
        check("公告包含每个落后版本", "v1.4.0" in notes and "v1.3.1" in notes, notes[:80])
        check("公告不含当前版本", "v1.3.0" not in notes)
        check("公告清掉 markdown 记号",
              "**" not in notes and "`" not in notes and "###" not in notes, notes[:80])
        check("公告新版本在前", notes.index("v1.4.0") < notes.index("v1.3.1"))

        crlf = updater.format_notes([rel("9.9.9", "### 标题\r\n\r\n\r\n\r\n- 一条\r\n- 两条")])
        check("CRLF 正文被归一化（不留空行与 \\r）",
              "\r" not in crlf and "\n\n\n" not in crlf and "一条" in crlf, crlf[:60])

        # --- 缓存与节流 ---
        check("检查结果写入缓存", updater.UPDATE_CACHE_PATH.is_file())
        cached = updater.cached_status("1.3.0")
        check("缓存可离线展示公告",
              cached is not None and cached.from_cache and cached.has_update
              and cached.latest == "1.4.0", repr(cached))
        check("刚检查过 24 小时内不再联网", not updater.needs_check(24))
        check("interval=0 表示每次都查", updater.needs_check(0))
        data = json.loads(updater.UPDATE_CACHE_PATH.read_text(encoding="utf-8"))
        data["attempted_at"] = (now_local() - timedelta(hours=25)).isoformat(timespec="seconds")
        updater.UPDATE_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False),
                                            encoding="utf-8")
        check("超过间隔就重新检查", updater.needs_check(24))

        # --- 离线 / 坏缓存 ---
        offline = updater.check("1.3.0", fetch=list)
        check("离线不抛异常、也不算有更新",
              (not offline.ok) and (not offline.has_update), repr(offline))
        attempt = json.loads(updater.UPDATE_CACHE_PATH.read_text(encoding="utf-8"))
        check("离线也记下尝试时间（避免每次启动都等超时）",
              bool(attempt.get("attempted_at")), str(attempt.get("attempted_at")))
        check("离线时旧公告仍可读", updater.cached_status("1.3.0") is not None)
        updater.UPDATE_CACHE_PATH.write_text("{ 坏缓存", encoding="utf-8")
        check("坏缓存读作空", updater.read_cache() == {})
        check("坏缓存当作没检查过", updater.cached_status("1.3.0") is None
              and updater.needs_check(24))

        # --- 提醒策略 ---
        again = updater.check("1.3.0", fetch=lambda: releases)
        check("发现新版 -> 横幅 + 自动公告", updater.should_notify(again, {}) == (True, True))
        check("看过公告后只留横幅",
              updater.should_notify(again, {"update": {"seen": "1.4.0"}}) == (True, False))
        check("跳过该版本后彻底安静",
              updater.should_notify(again, {"update": {"skipped": "1.4.0"}}) == (False, False))
        check("跳过旧版本不影响新版本",
              updater.should_notify(again, {"update": {"skipped": "1.3.1"}}) == (True, True))
        fresh = updater.check("1.4.0", fetch=lambda: releases)
        check("已是最新不提示",
              (not fresh.has_update) and updater.should_notify(fresh, {}) == (False, False))
        check("检查失败不提示", updater.should_notify(offline, {}) == (False, False))

        # --- 已是最新时也能看「本版新功能」 ---
        check("已是最新时也能找到本版 Release",
              fresh.current_release is not None and fresh.current_release.version == "1.4.0",
              repr(fresh.current_release))
        check("本版说明只包含当前版本",
              "v1.4.0" in fresh.current_notes and "v1.3.1" not in fresh.current_notes,
              fresh.current_notes[:60])
        cached_fresh = updater.cached_status("1.4.0")
        check("缓存也能给出本版说明（离线可用）",
              cached_fresh is not None and "发现新版" in cached_fresh.current_notes,
              repr(cached_fresh))
        unpublished = updater.cached_status("9.9.9")
        check("没发布过的版本没有本版说明",
              unpublished is not None and unpublished.current_notes == ""
              and not unpublished.has_update, repr(unpublished))

        # --- 升级后首次启动要不要弹「已更新到 vX」 ---
        check("版本变新才弹",
              updater.should_announce_upgrade({"update": {"last_run": "1.3.0"}}, "1.4.0"))
        check("同版本不弹",
              not updater.should_announce_upgrade({"update": {"last_run": "1.4.0"}}, "1.4.0"))
        check("降级（回退旧版）不弹",
              not updater.should_announce_upgrade({"update": {"last_run": "1.5.0"}}, "1.4.0"))
        check("老用户（本功能上线前就在用）当作升级",
              updater.should_announce_upgrade(
                  {"last_sync": "2026-10-01T08:00:00+08:00"}, "1.3.0"))
        check("有下载记录的老用户也算升级",
              updater.should_announce_upgrade({"files": {"x": {}}}, "1.3.0"))
        check("全新安装不弹",
              not updater.should_announce_upgrade({"last_sync": None, "files": {}}, "1.3.0"))

        # --- 资产挑选 ---
        check("没有 zip 时不给资产", updater.pick_asset(rel("1.4.0", assets=(wheel,))) is None)
        plain = updater.Asset("CanvasHelper-1.4.0.zip", "https://example.invalid/b.zip", 10)
        picked = updater.pick_asset(rel("1.4.0", assets=(wheel, plain, z64)))
        check("多个 zip 时优先 win64", picked is not None and picked.name == z64.name,
              repr(picked))

        # --- 一键替换的安全阀 ---
        app = tmp / "app" / "CanvasHelper"
        (app / "_internal").mkdir(parents=True)
        (app / "CanvasHelper.exe").write_bytes(b"MZ")
        check("标准单目录构建可自动替换", updater.safe_to_replace(app))
        check("目录里没有 exe 就不替换", not updater.safe_to_replace(tmp / "app"))
        check("盘符 / 家目录不替换", not updater.safe_to_replace(Path.home()))
        (app / "我的笔记.txt").write_text("x", encoding="utf-8")
        check("多出用户文件就不替换（防止 /MIR 删资料）",
              not updater.safe_to_replace(app))
        (app / "我的笔记.txt").unlink()

        # --- 解压（含 zip-slip）+ 替换脚本 ---
        zpath = tmp / "CanvasHelper-v1.4.0-win64.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("CanvasHelper/CanvasHelper.exe", b"MZ")
            zf.writestr("CanvasHelper/_internal/lib.dll", b"dll")
            zf.writestr("../evil.txt", b"x")
        new_dir = updater.stage(zpath, "1.4.0")
        check("解压出 exe 所在目录", (new_dir / "CanvasHelper.exe").is_file(), str(new_dir))
        check("拒绝目录穿越",
              not (tmp / "evil.txt").exists() and not (tmp / "updates" / "evil.txt").exists())
        check("解压结果可自动替换", updater.safe_to_replace(app, new_dir))

        script = updater.write_apply_script(4321, app, new_dir)
        text = script.read_text(encoding="utf-8-sig")
        check("替换脚本落在 updates 目录",
              script.is_file() and script.parent == updater.UPDATES_DIR, str(script))
        check("脚本等待旧进程退出",
              "$waitPid = 4321" in text and "Wait-Process -Id $waitPid" in text, text[:160])
        check("脚本镜像替换", "robocopy" in text.lower() and "/MIR" in text)
        check("脚本替换完重启", "Start-Process" in text and "CanvasHelper.exe" in text)
        check("脚本里的路径带引号（中文路径也安全）", f"'{app}'" in text, text[:160])

        # --- 一键清理能看到并删掉更新包 ---
        from canvas_helper import cleaner

        targets = {t.key: t for t in cleaner.scan()}
        check("一键清理能看见已下载的更新包",
              "updates" in targets and targets["updates"].bytes > 0,
              str(sorted(targets)))
        rep = cleaner.run({"updates"})
        check("一键清理能删掉更新包",
              "updates" in rep["cleaned"] and not updater.UPDATES_DIR.exists(), str(rep))

        # --- 启动时「已是最新」会自动回收上次的更新包 ---
        updater.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        (updater.UPDATES_DIR / "old.zip").write_bytes(b"12345")
        freed = updater.cleanup_downloads()
        check("已是最新时自动回收更新包",
              freed == 5 and not updater.UPDATES_DIR.exists(), str(freed))
    finally:
        updater.UPDATE_CACHE_PATH, updater.UPDATES_DIR = old_cache, old_updates
        shutil.rmtree(tmp, ignore_errors=True)

    # --- 静态：公告来源与打包自检清单 ---
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    check("CHANGELOG 有当前版本小节（发版公告就抓这里）",
          re.search(rf"^## \[{re.escape(ver)}\]", changelog, re.MULTILINE) is not None,
          ver)
    check("自检清单包含 updater（打包漏了 exe 会崩）", "updater" in _SUBMODULES)
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    check("配置模板有 update 段",
          {"check_on_start", "interval_hours", "include_prerelease"}
          <= set(example.get("update") or {}), str(example.get("update")))
    check("默认开启启动检查", DEFAULT_CONFIG["update"]["check_on_start"] is True,
          str(DEFAULT_CONFIG["update"]))
    gui_src = (ROOT / "canvas_helper" / "gui.py").read_text(encoding="utf-8")
    check("公告窗支持「本版新功能」模式（已是最新也能看）",
          "just_updated: bool = False" in gui_src and "current_notes" in gui_src)
    check("升级后首次启动会弹一次本版说明",
          "_announce_upgrade_once" in gui_src and "should_announce_upgrade" in gui_src)


def s17_install_ux() -> None:
    """[17] 免安装分发：快捷方式、替换原因、计划任务命令。"""
    from canvas_helper import paths as _paths
    from canvas_helper import scheduler, shortcut, updater

    print("\n[17] 免安装分发（快捷方式 / 一键更新前置条件 / 计划任务）")

    tmp = Path(tempfile.mkdtemp(prefix="ch_lnk_"))
    try:
        root = tmp / "app"
        (root / "docs" / "icons").mkdir(parents=True)
        (root / "docs" / "icons" / "canvashelper.ico").write_bytes(b"\x00")
        (root / "CanvasHelper.pyw").write_text("print(1)", encoding="utf-8")

        src_t = shortcut.target_for(project_dir=root, frozen=False)
        check("源码版快捷方式指向 python 解释器",
              Path(src_t.path).name.lower() in ("pythonw.exe", "python.exe"), src_t.path)
        check("源码版参数是 CanvasHelper.pyw",
              src_t.args.strip('"').endswith("CanvasHelper.pyw"), src_t.args)
        check("源码版带应用图标", src_t.icon.endswith("canvashelper.ico,0"), src_t.icon)
        check("源码版工作目录是项目根",
              Path(src_t.workdir) == root.resolve(), src_t.workdir)

        (root / "CanvasHelper.pyw").unlink()
        (root / "docs" / "icons" / "canvashelper.ico").unlink()
        fallback = shortcut.target_for(project_dir=root, frozen=False)
        check("没有 .pyw 时退回 -m canvas_helper gui",
              "-m canvas_helper gui" in fallback.args, fallback.args)
        check("没有图标文件时不设图标", fallback.icon == "", fallback.icon)

        exe = tmp / "bin" / "CanvasHelper.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        frozen_t = shortcut.target_for(exe=exe, frozen=True)
        check("打包版快捷方式直接指向 exe",
              Path(frozen_t.path) == exe.resolve(), frozen_t.path)
        check("打包版无参数、图标取 exe 自带资源",
              frozen_t.args == "" and frozen_t.icon.endswith("CanvasHelper.exe,0"),
              f"{frozen_t.args!r} / {frozen_t.icon!r}")

        script = shortcut.build_ps1(src_t)
        check("快捷方式脚本用 WScript.Shell", "WScript.Shell" in script)
        check("快捷方式脚本取真实桌面目录（兼容 OneDrive 重定向）",
              "GetFolderPath('Desktop')" in script)
        check("快捷方式脚本里的路径带引号", f"'{root.resolve()}'" in script, script[:160])
        check("快捷方式脚本会落盘 .lnk",
              "$lnk.Save()" in script and "CanvasHelper.lnk" in script)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp2 = Path(tempfile.mkdtemp(prefix="ch_why_"))
    try:
        app = tmp2 / "CanvasHelper"
        (app / "_internal").mkdir(parents=True)
        (app / "CanvasHelper.exe").write_bytes(b"MZ")
        check("标准安装目录可以自动替换", updater.why_not_replace(app) == "",
              updater.why_not_replace(app))
        check("目录里没有 exe 时说明原因",
              "没有 CanvasHelper.exe" in updater.why_not_replace(tmp2),
              updater.why_not_replace(tmp2))
        (app / "我的笔记.txt").write_text("x", encoding="utf-8")
        why = updater.why_not_replace(app)
        check("多出用户文件时点名说明（防止删除资料）", "我的笔记.txt" in why, why)
        (app / "我的笔记.txt").unlink()
        check("safe_to_replace 与原因判断一致",
              updater.safe_to_replace(app) is (updater.why_not_replace(app) == ""))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # 计划任务命令：打包版不该再引用不在包里的 headless_entry.py
    had_frozen = hasattr(sys, "frozen")
    old_frozen = getattr(sys, "frozen", None)
    try:
        sys.frozen = True  # type: ignore[attr-defined]
        packed_cmd = scheduler._cmd("sync --headless")
        check("打包版计划任务直接用 exe 自己",
              packed_cmd == f'"{sys.executable}" sync --headless', packed_cmd)
        check("打包版命令不再引用 headless_entry.py",
              "headless_entry.py" not in packed_cmd, packed_cmd)
    finally:
        if had_frozen:
            sys.frozen = old_frozen  # type: ignore[attr-defined]
        else:
            del sys.frozen  # type: ignore[attr-defined]
    source_cmd = scheduler._cmd("sync --headless")
    check("源码版计划任务仍是 pythonw + 入口脚本",
          "headless_entry.py" in source_cmd and "sync --headless" in source_cmd,
          source_cmd)

    orig_load = _paths.load_state
    try:
        _paths.load_state = lambda: {"schedule_cmd": scheduler._commands()}  # type: ignore[assignment]
        check("命令没变就不重建计划任务", not scheduler.moved())
        _paths.load_state = lambda: {  # type: ignore[assignment]
            "schedule_cmd": {"sync": "old-python.exe x", "report": "old"}}
        check("程序换了位置就要重建计划任务", scheduler.moved())
        _paths.load_state = dict  # type: ignore[assignment]
        check("没有记录过命令时也会重建（升级后首次）", scheduler.moved())
    finally:
        _paths.load_state = orig_load  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 脚本入口
# --------------------------------------------------------------------------- #
def main() -> int:
    cfg = load_config()
    root, fixture_root = build_course_root()
    folders = course_dirs(root)
    c, res = build_drafts_ctx(cfg)

    s1_folders(folders)
    s2_classify()
    s3_resolve(folders)
    s4_sanitize()
    s5_drafts(c, res, cfg)
    s5b_completed()
    s5c_cleaner()
    s5d_sort_and_color()
    s6_plan()
    s6b_local()
    s6c_rename()
    s7_extras(c, res.drafts)
    s8_auth()
    s9_packaging()
    s10_extract()
    s11_tidy()
    s12_discussions()
    s13_universal()
    s14_schools()
    s15_schoollist()
    s16_update()
    s17_install_ux()

    if fixture_root is not None:
        shutil.rmtree(fixture_root, ignore_errors=True)

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    if FAILURES:
        print("失败明细:")
        for f in FAILURES:
            print(f"  ✗ {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
