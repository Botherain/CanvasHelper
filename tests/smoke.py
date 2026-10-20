"""离线自检：分类规则、目录推断、压缩包解压、讲义整理、讨论区链接、待办合并、登录过期契约、打包元数据。

两种入口共用同一套断言，因此不会出现「脚本绿、pytest 红」的分叉：

* ``python tests/smoke.py`` —— 逐条打印 PASS/FAIL，任一失败退出码 1；
* ``python -m pytest tests`` —— 按小节拆成独立用例，失败时直接给出该小节明细。

不联网；真实 ``state.json`` / ``pending_todos.json`` 由 conftest 备份还原。
"""

from __future__ import annotations

import json
import os
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
    load_doc,
    merge,
    preview_status,
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
    check("声明 requires-python >=3.11", 'requires-python = ">=3.11"' in pt)
    check("依赖与原 requirements 同源",
          '"playwright>=1.63"' in pt and '"tzdata>=2024.1"' in pt)
    check("控制台入口指向 __main__", "canvas_helper.__main__:main" in pt)
    check("GUI 入口点存在", "canvas_helper.gui:main" in pt)
    check("只打包 canvas_helper", 'packages = ["canvas_helper"]' in pt)
    check("声明 MIT 且 LICENSE 存在",
          'license = "MIT"' in pt and (ROOT / "LICENSE").is_file())


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
    from datetime import UTC, datetime
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
          to_local(utc_due, ZoneInfo("America/New_York")).astimezone(UTC) == utc_due,
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
