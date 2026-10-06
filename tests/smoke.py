"""冒烟测试：分类规则、子目录推断、待办合并（不联网、不改动正式数据）。"""

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
    CategoryDirs, anchor_key, category_of, course_dirs, match_folder, plan_item,
    resolve_dir, sanitize)
from canvas_helper.deskbox import (TodoDraft, build_drafts, dedupe_completed,  # noqa: E402
                                   load_doc, merge, preview_status)
from canvas_helper.paths import DEFAULT_CONFIG, load_config  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


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


def main() -> int:
    cfg = load_config()
    root = Path(cfg["classes_root"])
    fixture_root: Path | None = None
    if not root.is_dir():
        # CI / 新机器没有真实课程目录：按蓝本造一份，保证测试自洽且与机器无关
        fixture_root = Path(tempfile.mkdtemp(prefix="ch_courses_"))
        for course, subs in COURSE_FIXTURE.items():
            for sub in subs:
                (fixture_root / course / sub).mkdir(parents=True, exist_ok=True)
        root = fixture_root
    folders = course_dirs(root)
    print(f"\n[1] 识别本地课程文件夹 ({len(folders)})")
    for k, v in folders.items():
        print(f"      {k} -> {v.name}")
    check("识别到 6 门课", len(folders) >= 6, str(len(folders)))
    check("匹配 CS0001", match_folder("CS0001", folders) is not None)
    check("匹配 CS0006", match_folder("CS0006", folders) is not None)

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
    ]
    for folder, title, kind, want in cases:
        got = category_of(folder, title, kind)
        check(f"{folder or '(根)'}/{title} -> {want}", got == want, f"got={got}")

    print("\n[3] 目录解析（镜像 Canvas 文件夹 + 跟随本地现状）")
    d5187 = match_folder("CS0001", folders)
    cats = CategoryDirs.scan(d5187)
    p = resolve_dir(cats, "course files/Lecture/Week3", "", "week3.pdf", "File")
    check("Lecture 平铺", p == cats.get("lecture"), str(p))
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
    check("CS0006 Lecture notes -> 讲义平铺", p == cats4.get("lecture"), str(p))
    p = resolve_dir(cats4, "course files", "", "Introduction.pptx", "File")
    check("CS0006 根目录 pptx -> 讲义", p == cats4.get("lecture"), str(p))
    p = resolve_dir(cats4, "course files", "", "Syllabus.pdf", "File")
    check("CS0006 根目录 syllabus -> 6.Others", p == cats4.get("other"), str(p))

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

    print("\n[5] 待办草稿 + 合并（副本，不碰真实文件）")
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
    check("跳过已过期", res.skipped_past == 1, str(res.skipped_past))
    check("生成 2 条草稿", len(res.drafts) == 2, str(len(res.drafts)))
    d0 = res.drafts[0]
    check("时区为 +08:00", d0.due.strftime("%z") == "+0800", str(d0.due))
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
                 "dueDate": "2026-01-0%dT00:00:00+08:00" % i,
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
    new_items = [i for i in doc2["items"] if i.get("dueDate", "").startswith("2026-11-20")]
    check("新条目字段齐全",
          all(k in new_items[0] for k in
              ("id", "text", "isCompleted", "colorMarker", "dueDate", "notes",
               "attachments", "reminderOffsetMinutes", "sortOrder", "createdAt", "updatedAt")),
          json.dumps(new_items[0], ensure_ascii=False)[:200] if new_items else "missing")
    check("sortOrder 递增", new_items and new_items[0]["sortOrder"] >= before,
          str(new_items[0]["sortOrder"]) if new_items else "")
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n[5b] 已完成作业的处理（不写正式文件）")
    from datetime import datetime  # noqa: E402

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
    doc2, added2 = merge({"version": 3, "items": []}, [d2], 1440,
                         done_index=done_idx, stats=st2)
    check("完成并删除后不再冒出来", len(added2) == 0 and st2.get("completed") == 1, str(st2))
    check("状态也是已完成", d2.status == "completed", d2.status)

    # (3) 截止时间变了 -> 视为新截止，重新提醒
    d3 = _draft("Assignment A", url_a, "2026-11-08T23:59:00+08:00")
    st3: dict = {}
    doc3, added3 = merge({"version": 3, "items": []}, [d3], 1440,
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
          d4.key == f"CS0001 · Quiz 1|2026-11-03T23:59:00+08:00", d4.key)

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
    got = preview_status(prev, [d4, _draft("Nothing", "https://canvas.example.edu/courses/1/assignments/9999", due_a)])
    check("预判已完成", got.get(d4.key) == "completed", str(got))
    check("预判未知项", list(got.values()).count("") == 0 and len(got) == 1, str(got))
    shutil.rmtree(prev.parent, ignore_errors=True)

    print("\n[5c] 一键清理缓存（只扫描，不删除）")
    from canvas_helper import cleaner  # noqa: E402

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

    print("\n[6] 下载计划（dry-run，不写盘）")
    from canvas_helper.filesync import build_plan, describe  # noqa: E402
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
    check("课件平铺", "1.Lecture session/lecture2.pdf" in names, str(names))
    check("作业进已有子目录", "3.Assignments/Assignment_1/A1 handout.pdf" in names, str(names))
    check("未匹配进 6.Others", "6.Others/Rules.pdf" in names, str(names))
    check("项目进 4.Projects", "4.Projects/Dataset.zip" in names, str(names))
    check("页面加 html 后缀",
          any(n.endswith("Week 2 Overview.html") for n in names), str(names))
    # 已存在同名 -> 改名
    (tmpdir / "1.Lecture session").mkdir()
    (tmpdir / "1.Lecture session" / "lecture2.pdf").write_bytes(b"x" * 999)
    plan2 = build_plan(Course(id=9, name="Demo", code="CS9999"), tmpdir, items, {"files": {}})
    renamed = [p for p in plan2.files if p.action == "rename"]
    check("重名改名", len(renamed) == 1 and "(1)" in renamed[0].target.name,
          str([p.target.name for p in plan2.files]))
    txt = describe([plan])
    check("预览文本可渲染", "合计" in txt and "CS9999" in txt, txt[:80])

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
    (tmp3 / "1.Lecture session" / "Lecture3.pptx").write_bytes(b"a" * 100)
    item2 = Item(course_id=9, module_name="", title="Lecture3.pptx", kind="File",
                  file_id=122, file_name="Lecture3.pptx",
                  file_url="http://x/8", file_size=999,
                  folder_path="course files/Lecture")
    p7 = build_plan(Course(id=9, name="Demo", code="CS9999"), tmp3, [item2], {"files": {}})
    check("内容变了仍改名下载",
          p7.files[0].action == "rename" and p7.files[0].target.name == "Lecture3 (1).pptx",
          f"{p7.files[0].action} {p7.files[0].target.name}")
    shutil.rmtree(tmp3, ignore_errors=True)

    print("\n[7] 附加功能：GradeNeed / .ics / 每周摘要")
    from canvas_helper.extras import build_ics, update_grade_file, weekly_report  # noqa: E402
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

    ics = build_ics(res.drafts)
    check("ics 头尾", ics.startswith("BEGIN:VCALENDAR") and ics.endswith("END:VCALENDAR\r\n"))
    check("ics 两条事件", ics.count("BEGIN:VEVENT") == 2, str(ics.count("BEGIN:VEVENT")))
    check("ics 含提前一天提醒", "TRIGGER:-P1D" in ics)
    check("ics 换行 CRLF", "\r\n" in ics)

    rep = weekly_report(gdir, c, [gdir / "x.pdf"], [], res.drafts)
    body = rep.read_text(encoding="utf-8")
    check("周报含截止项", "CS0001" in body and "本周截止" in body, body[:80])
    check("周报文件名规范", rep.name.startswith("本周摘要-"), rep.name)

    print("\n[8] 登录过期不再静默失败")
    from canvas_helper import paths as _paths  # noqa: E402
    from canvas_helper.runner import _relogin_headless  # noqa: E402

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
        from canvas_helper.runner import run_sync  # noqa: E402

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

    _log: list[str] = []
    say = _log.append
    env_bak = os.environ.get("SESSIONNAME")
    try:
        os.environ["SESSIONNAME"] = "Console"
        off = {"auth": {"popup_on_expiry": False, "popup_timeout_s": 99}}
        s1 = _Stub(True)
        check("配置关掉时不自愈", _relogin_headless(off, s1, say) is False)
        check("关闭时根本不碰会话", not s1.timeouts, str(s1.timeouts))

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

        os.environ.pop("SESSIONNAME", None)
        s4 = _Stub(True)
        check("无交互会话时不白等",
              _relogin_headless(on, s4, say) is False and not s4.timeouts,
              str(s4.timeouts))
        check("每种情况都有进度提示", len(_log) == 5, str(_log))
    finally:
        if env_bak is None:
            os.environ.pop("SESSIONNAME", None)
        else:
            os.environ["SESSIONNAME"] = env_bak

    print("\n[9] 打包元数据 pyproject.toml")
    from canvas_helper import __version__ as _ver  # noqa: E402

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

    for d in (tmpdir, gdir, *( [fixture_root] if fixture_root else [] )):
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
