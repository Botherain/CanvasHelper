"""CanvasHelper 图形界面（tkinter + ttk）。"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from contextlib import suppress
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from . import APP_NAME, __version__, deskbox, runner, scheduler, shortcut, updater
from .canvas_api import Course
from .classify import course_dirs, match_folder
from .paths import (
    DATA_DIR,
    LOG_PATH,
    PROFILE_DIR,
    canvas_home,
    get_logger,
    load_config,
    load_state,
    now_local,
    save_config,
    save_state,
)

log = get_logger()

FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")

#: 设置页显示用的颜色名（值仍写 DeskBox 认识的英文名）
_COLOR_ZH = {
    "red": "红",
    "orange": "橙",
    "yellow": "黄",
    "green": "绿",
    "blue": "蓝",
    "purple": "紫",
    "teal": "青",
    "pink": "粉",
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} · Canvas 课程同步助手 v{__version__}")
        self.geometry("1200x820")
        self.minsize(980, 640)
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self._busy = False
        self.courses: list[Course] = []
        self.course_folders: dict[int, Path | None] = {}  # match_folder 可能无匹配
        # 身兼二职：同步跑完是 SyncResult，只跑截止收集时是 SimpleNamespace(drafts=...)，
        # 两者的取用方都只认 .drafts（多数还带 getattr 默认值），故此处不做强类型约束
        self.result: Any = None
        # 通知只活在内存里：state.json 不再存通知正文，这里放的是最近一次
        # 拉到的结果，由 _refresh_notice_tree 渲染。已读标记仍在 state 的
        # notices.seen 里（只存 key，用于判定未读）。
        self.notices: list[dict] = []
        self._on_done = None
        self._on_error = None
        # 版本检查：先读缓存（零网络），启动后再后台补一次联网检查
        self.update_status: updater.UpdateStatus | None = updater.cached_status()
        self._update_checking = False
        # 公告窗打开期间，下载进度文字写到这里；窗口关掉后置回 None
        self._update_status_var: tk.StringVar | None = None
        # 公告窗本身（避免重复点横幅/按钮时开出好几个）
        self._update_win: tk.Toplevel | None = None
        # 升级后第一次启动：等拿到「本版说明」再弹一次
        self._upgrade_notes_pending = False

        self._build_ui()
        self.after(100, self._pump)
        self.after(300, self._startup)

    # ---------- 基础设施 ----------

    def _startup(self) -> None:
        state = load_state()
        self._set_status(f"上次同步：{state.get('last_sync') or '（从未）'}")
        # 院校清单在线更新：后台静默拉取，不阻塞界面（缓存过期才拉）
        self._refresh_school_list_async()
        # 版本校验：错开一点启动，让首屏先画出来；节流由 updater 自己控制
        self.after(800, self._announce_upgrade_once)
        self.after(1500, self._check_update_async)
        # 首次运行：还没配过站点地址 -> 先让用户选学校，选完再进登录
        if not canvas_home():
            self.after(400, self._first_run_pick)
            return
        auth = state.get("auth_needed") or {}
        if auth:
            # 后台同步时登录过期了，必须让人一眼看到，而不是埋在 sync.log 里
            when = str(auth.get("at") or "")[:16].replace("T", " ")
            self.login_var.set(f"登录状态：⚠ 已过期（{when}），点击重新登录")
            self.log("上次后台同步时 Canvas 登录已过期，"
                     "点上方「登录状态」或「登录 / 刷新课程」重新登录")
        pending = deskbox.pending_drafts()
        if pending:
            self.log(f"检测到 {len(pending)} 条后台排队待办，准备写入 DeskBox…")

            def job():
                return runner.flush_pending(self.cfg, progress=self._progress)

            self.bg(job, on_done=lambda n: self.log(f"已补写 {n} 条排队待办"))
        if scheduler.status():
            self.log("计划任务：已注册每日同步/每周摘要")
            # 程序被挪过位置时旧任务会指向不存在的路径：稍后按当前位置重建
            self.after(2000, self._check_schedule_paths)
        # 通知不再落盘，所以打开时现拉一次；但登录态已知过期时跳过，
        # 免得刚开窗口就弹出浏览器登录框（那种情况下顶部横幅已经在提示了）
        if not auth:
            self.do_load_notices()

    def _refresh_school_list_async(self) -> None:
        """后台静默更新院校清单（缓存过期才拉；失败不影响使用）。"""
        from .schoollist import needs_refresh, update

        if not needs_refresh():
            return

        def job():
            return update()

        def done(rep):
            if rep.get("ok"):
                self.log(f"院校清单已在线更新：共 {rep['total']} 所")
            else:
                self.log(f"院校清单在线更新未成功（{rep.get('error') or '未知'}），"
                         "继续用本地清单")

        self.bg(job, on_done=done)

    def _progress(self, msg: str) -> None:
        self.q.put(("progress", msg))

    def bg(self, fn, on_done=None, on_error=None) -> bool:
        """在后台线程运行 fn，避免阻塞界面。"""
        if self._busy:
            messagebox.showinfo(APP_NAME, "有任务正在进行，请稍候。")
            return False
        self._busy = True
        self._on_done = on_done
        self._on_error = on_error
        # indeterminate 才会动；空闲态是 determinate 的 0%，切回来再 start
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)
        self._set_status("任务进行中…")

        def work():
            try:
                self.q.put(("done", fn()))
            except Exception as exc:
                log.exception("后台任务失败")
                self.q.put(("error", exc))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "progress":
                    self.log(payload)
                    self.status_var.set(str(payload))
                elif kind == "done":
                    self._finish()
                    self._safe_call(self._on_done, payload)
                elif kind == "error":
                    self._finish()
                    self.log(f"错误：{payload}")
                    if self._on_error:
                        self._safe_call(self._on_error, payload)
                    else:
                        messagebox.showerror(APP_NAME, str(payload))
                elif kind == "update":
                    self._on_update_status(*payload)
                elif kind == "update-progress":
                    if self._update_status_var is not None:
                        self._update_status_var.set(str(payload))
                elif kind == "update-ready":
                    self._on_update_ready(payload)
                elif kind == "update-error":
                    if self._update_status_var is not None:
                        self._update_status_var.set(f"更新失败：{payload}")
                    self.log(f"更新失败：{payload}")
                    messagebox.showerror(APP_NAME, f"下载 / 准备更新失败：\n{payload}")
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _finish(self) -> None:
        self._busy = False
        self.progress_bar.stop()
        # 收尾先走满再回 0：任务结束瞬间直接清零会看不出「刚跑完」
        self.progress_bar.configure(mode="determinate", value=100)
        self.after(500, self._reset_progress)
        self._set_status("就绪")

    def _reset_progress(self) -> None:
        if self._busy:  # 收尾延迟期间又开了新任务，别把新进度条清掉
            return
        self.progress_bar.configure(value=0)

    def _safe_call(self, fn, payload) -> None:
        if not fn:
            return
        try:
            fn(payload)
        except Exception as exc:
            log.exception("回调执行失败")
            self.log(f"回调错误：{exc}")
            messagebox.showerror(APP_NAME, str(exc))

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def log(self, msg: str) -> None:
        self.log_text.insert("end", f"{now_local():%H:%M:%S}  {msg}\n")
        # 控件行数无上限时，GUI 挂得越久文本控件越滚越慢：保留最近 500 行，
        # 超出的部分从顶部按批删（index("end-1c") 取的是最后一行的逻辑行号）。
        last = int(self.log_text.index("end-1c").split(".")[0])
        if last > 500:
            self.log_text.delete("1.0", f"{last - 500 + 1}.0")
        self.log_text.see("end")

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        self.option_add("*Font", FONT)
        style = ttk.Style(self)
        with suppress(tk.TclError):
            style.theme_use("vista")
        style.configure("Treeview", font=FONT, rowheight=24)
        style.configure("Treeview.Heading", font=FONT)
        style.configure("TButton", font=FONT, padding=(10, 5))
        style.configure("TLabel", font=FONT)
        style.configure("Header.TLabel", font=FONT_BOLD)

        # 顶栏
        head = ttk.Frame(self, padding=(12, 10, 12, 6))
        head.pack(fill="x")
        ttk.Label(head, text="Canvas 同步助手", style="Header.TLabel").pack(side="left")
        self.login_var = tk.StringVar(value="登录状态：未知")
        lbl_login = ttk.Label(head, textvariable=self.login_var, cursor="hand2")
        lbl_login.pack(side="left", padx=(16, 0))
        # 过期时标签会变成「⚠ 已过期…点击重新登录」，直接点它即可
        lbl_login.bind("<Button-1>", lambda _e: self.do_load_courses())
        # 「有新版本」提示：默认不占位，发现有更新才 pack 出来
        self.update_var = tk.StringVar(value="")
        self.update_label = ttk.Label(
            head, textvariable=self.update_var, cursor="hand2", foreground="#0a58ca")
        self.update_label.bind("<Button-1>", lambda _e: self.do_show_update())
        ttk.Button(head, text="登录 / 刷新课程", command=self.do_load_courses).pack(side="right")
        ttk.Button(head, text="打开 Canvas", command=self.open_canvas).pack(side="right", padx=6)
        ttk.Button(head, text="选择院校", command=self.do_pick_school).pack(side="right", padx=6)

        # 日志 + 状态栏（进度条 / 状态文字）先占位、贴底。
        # ttk.Notebook 的请求高度由最高的那个标签页决定：设置页有 32 行控件，
        # 实测把 notebook 的请求高度撑到 760px。若 notebook 先 pack，packer 会把
        # 剩余空间全给它、底部只剩 3px —— 进度条与状态文字被整体裁掉（默认窗口
        # 1200x820 即如此，要把窗口拉到约 960px 高才显现）。
        # 改成先 pack 且 side="bottom"，notebook 只取剩余空间，状态栏恒可见。
        bottom = ttk.Frame(self, padding=(12, 0, 12, 8))
        bottom.pack(fill="x", side="bottom")
        self.log_text = ScrolledText(bottom, height=4, font=("Consolas", 9), state="normal")
        self.log_text.pack(fill="x")
        bar = ttk.Frame(bottom)
        bar.pack(fill="x", pady=(6, 0))
        # 空闲时停在 0% 的确定态（可见的空槽），任务进行中切 indeterminate 跑动画
        self.progress_bar = ttk.Progressbar(bar, mode="determinate", length=200,
                                            maximum=100, value=0)
        self.progress_bar.pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=10)

        # 标签页（在状态栏之后 pack，吃剩下的空间）
        self.nb = ttk.Notebook(self)
        self._build_sync_tab()
        self._build_todo_tab()
        self._build_notice_tab()
        self._build_grade_tab()
        self._build_settings_tab()
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))

    # ----- 课程同步页 -----

    def _build_sync_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text=" 课程同步 ")

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="全选", command=lambda: self._select_all(True)).pack(side="left")
        ttk.Button(bar, text="全不选", command=lambda: self._select_all(False)).pack(side="left", padx=6)
        ttk.Button(bar, text="预览归档方案", command=self.do_preview).pack(side="left", padx=6)
        ttk.Button(bar, text="开始同步", command=self.do_sync).pack(side="left")
        ttk.Label(bar, text="（双击课程行可切换勾选）").pack(side="right")

        cols = ("sel", "code", "name", "folder", "items")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse", height=9)
        for cid, text, width, anchor in (
            ("sel", "选", 40, "center"),
            ("code", "代码", 90, "w"),
            ("name", "课程名称", 320, "w"),
            ("folder", "本地文件夹", 330, "w"),
            ("items", "内容数", 70, "center"),
        ):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor)  # type: ignore[call-overload]
        self.tree.pack(fill="both", expand=False, pady=(8, 0))
        self.tree.bind("<Double-1>", self._toggle_row)

        ttk.Label(tab, text="归档预览 / 结果", style="Header.TLabel").pack(anchor="w", pady=(10, 2))
        self.preview = ScrolledText(tab, height=14, font=("Consolas", 9))
        self.preview.pack(fill="both", expand=True)

    def _build_todo_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text=" 待办（DeskBox） ")

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="读取截止时间", command=self.do_load_deadlines).pack(side="left")
        ttk.Button(bar, text="写入 DeskBox", command=self.do_write_todos).pack(side="left", padx=6)
        ttk.Button(bar, text="导出 .ics 日历", command=self.do_export_ics).pack(side="left")
        self.deskbox_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.deskbox_var).pack(side="right")

        cols = ("due", "course", "text", "kind", "state")
        self.todo_tree = ttk.Treeview(tab, columns=cols, show="headings", height=18)
        for cid, text, width, anchor in (
            ("due", "截止时间", 150, "w"),
            ("course", "课程", 90, "w"),
            ("text", "任务", 520, "w"),
            ("kind", "类型", 80, "center"),
            ("state", "状态", 90, "center"),
        ):
            self.todo_tree.heading(cid, text=text)
            self.todo_tree.column(cid, width=width, anchor=anchor)  # type: ignore[call-overload]
        self.todo_tree.pack(fill="both", expand=True, pady=(8, 0))

        note = ("写入方式：先把合并结果写到 todo.json.new 副本 → 关闭 DeskBox → 原子替换 → "
                "立即重启，停机时间约 1 秒。")
        ttk.Label(tab, text=note, foreground="#666").pack(anchor="w", pady=(8, 0))
        self.after(200, self._refresh_deskbox_state)

    def _build_notice_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text=" 通知 / 公告 ")

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新通知", command=self.do_load_notices).pack(side="left")
        ttk.Button(bar, text="打开选中链接", command=self._open_notice).pack(side="left", padx=6)
        self.unread_var = tk.StringVar(value="尚未拉取（点「刷新通知」）")
        ttk.Label(bar, textvariable=self.unread_var).pack(side="right")

        cols = ("time", "course", "title", "kind")
        self.notice_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for cid, text, width, anchor in (
            ("time", "时间", 150, "w"),
            ("course", "课程", 100, "w"),
            ("title", "标题", 700, "w"),
            ("kind", "类型", 90, "center"),
        ):
            self.notice_tree.heading(cid, text=text)
            self.notice_tree.column(cid, width=width, anchor=anchor)  # type: ignore[call-overload]
        self.notice_tree.pack(fill="both", expand=True, pady=(8, 0))
        self.notice_tree.bind("<Double-1>", lambda _e: self._open_notice())
        self._refresh_notice_tree()

    def _build_grade_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text=" 成绩 ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="成绩来自 Canvas，同步时自动写入各课程 GradeNeed.txt").pack(side="left")

        cols = ("course", "name", "score", "total")
        self.grade_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for cid, text, width, anchor in (
            ("course", "课程", 110, "w"),
            ("name", "评分项", 560, "w"),
            ("score", "得分", 100, "center"),
            ("total", "满分", 100, "center"),
        ):
            self.grade_tree.heading(cid, text=text)
            self.grade_tree.column(cid, width=width, anchor=anchor)  # type: ignore[call-overload]
        self.grade_tree.pack(fill="both", expand=True, pady=(8, 0))

    def _build_settings_tab(self) -> None:
        outer = ttk.Frame(self.nb, padding=14)
        self.nb.add(outer, text=" 设置 ")
        # 这一页有 32 行控件，高度远超标签页可视区，而 grid 本身没有滚动机制：
        # 窗口一矮，底部几行（计划任务、按钮组、路径说明）就掉到可视区外且够不着。
        # 套一层 Canvas + 纵向滚动条，内容帧 tab 放进 Canvas 里。
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tab = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=tab, anchor="nw")

        self.var_root = tk.StringVar(value=self.cfg["classes_root"])
        self.var_home = tk.StringVar(value=self.cfg.get("canvas_home") or "")
        self.var_tz = tk.StringVar(value=self.cfg.get("timezone") or "")
        self.var_ccode = tk.StringVar(value=self.cfg.get("course_code_pattern") or "")
        self.var_todo = tk.StringVar(value=self.cfg["deskbox"]["todo_path"])
        self.var_exe = tk.StringVar(value=self.cfg["deskbox"]["exe_path"])
        self.var_fix = tk.BooleanVar(value=self.cfg["deskbox"]["fix_attachment_paths"])
        self.var_sort = tk.BooleanVar(value=self.cfg["deskbox"].get("sort_by_due", True))
        self.var_manual_color = tk.StringVar(
            value=self._color_display(self.cfg["deskbox"].get("manual_color")))
        self.course_color_var = tk.StringVar(value="")
        self.var_update = tk.BooleanVar(
            value=bool((self.cfg.get("update") or {}).get("check_on_start", True)))
        self.var_ics = tk.BooleanVar(value=self.cfg["extras"]["ics"])
        self.var_grades = tk.BooleanVar(value=self.cfg["extras"]["grades"])
        self.var_notices = tk.BooleanVar(value=self.cfg["extras"]["notices"])
        self.var_extract = tk.BooleanVar(value=(self.cfg.get("archives") or {}).get("extract", True))
        self.var_discuss = tk.BooleanVar(
            value=(self.cfg.get("discussions") or {}).get("links", True))
        self.var_policy = tk.StringVar(value=self.cfg["deskbox"]["background_policy"])
        self.var_sched = tk.BooleanVar(value=self.cfg["schedule"]["enabled"])
        self.var_synctime = tk.StringVar(value=self.cfg["schedule"]["sync_time"])
        self.var_report = tk.BooleanVar(value=self.cfg["schedule"]["report_enabled"])

        def row(label: str, var, y: int, browse: str = "") -> None:
            ttk.Label(tab, text=label).grid(row=y, column=0, sticky="w", pady=4)
            ttk.Entry(tab, textvariable=var, width=78).grid(row=y, column=1, sticky="we", pady=4)
            if browse:
                ttk.Button(tab, text="浏览…", command=lambda: self._browse(var, browse)).grid(
                    row=y, column=2, padx=6)

        row("课程根目录", self.var_root, 0, "dir")
        row("Canvas 站点地址", self.var_home, 1)
        ttk.Button(tab, text="选择院校…", command=self.do_pick_school).grid(
            row=1, column=2, padx=6)
        row("时区（留空=跟随本机）", self.var_tz, 2)
        row("课程代码正则（留空=自动）", self.var_ccode, 3)
        row("DeskBox todo.json", self.var_todo, 4, "file")
        row("DeskBox 程序路径", self.var_exe, 5, "exe")
        tab.columnconfigure(1, weight=1)

        ttk.Label(
            tab, text="时区写 IANA 名（如 Asia/Hong_Kong / America/New_York）；"
                      "课程代码正则用于学校代码形态古怪时（第一个捕获组优先）",
            foreground="#666").grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(tab, text="同步行为", style="Header.TLabel").grid(row=7, column=0, sticky="w", pady=(14, 4))
        ttk.Checkbutton(tab, text="自动修复已有待办中缺少 Classes 层级的附件路径",
                        variable=self.var_fix).grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(tab, text="导出截止日历 .ics", variable=self.var_ics).grid(
            row=9, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(tab, text="更新成绩到 GradeNeed.txt", variable=self.var_grades).grid(
            row=10, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(tab, text="读取并聚合通知/公告", variable=self.var_notices).grid(
            row=11, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(
            tab, text="下载后自动解压压缩包（内容包解压，数据包原样保留）",
            variable=self.var_extract).grid(row=12, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(
            tab, text="保存讨论区内容（帖子存快照，Colab/GitHub 链接文件直接下载）",
            variable=self.var_discuss).grid(row=13, column=0, columnspan=3, sticky="w")

        ttk.Label(tab, text="待办外观", style="Header.TLabel").grid(
            row=14, column=0, sticky="w", pady=(14, 4))
        ttk.Checkbutton(
            tab, text="写入 DeskBox 时按截止时间排序（会覆盖手动拖动顺序）",
            variable=self.var_sort).grid(row=15, column=0, columnspan=3, sticky="w")
        color_row = ttk.Frame(tab)
        color_row.grid(row=16, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(color_row, text="自建待办颜色").pack(side="left")
        ttk.Combobox(
            color_row, textvariable=self.var_manual_color,
            values=["不设置"] + [self._color_display(c) for c in deskbox.TODO_COLORS],
            state="readonly", width=12,
        ).pack(side="left", padx=6)
        course_row = ttk.Frame(tab)
        course_row.grid(row=17, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(course_row, text="课程颜色").pack(side="left")
        ttk.Label(course_row, textvariable=self.course_color_var,
                  foreground="#666").pack(side="left", padx=6)
        ttk.Button(course_row, text="编辑课程颜色…",
                   command=self.do_edit_course_colors).pack(side="left")

        ttk.Label(tab, text="后台（计划任务）同步时 DeskBox 待办处理方式",
                  style="Header.TLabel").grid(row=18, column=0, sticky="w", pady=(14, 4))
        for i, (val, text) in enumerate((
            ("queue", "排队：不打扰 DeskBox，下次打开本程序时写入（推荐）"),
            ("restart", "立即重启 DeskBox 完成写入"),
            ("skip", "跳过：后台同步不写待办"),
        )):
            ttk.Radiobutton(tab, text=text, value=val, variable=self.var_policy).grid(
                row=19 + i, column=0, columnspan=3, sticky="w")

        ttk.Label(tab, text="计划任务", style="Header.TLabel").grid(
            row=22, column=0, sticky="w", pady=(14, 4))
        ttk.Checkbutton(tab, text="启用每日自动同步", variable=self.var_sched).grid(
            row=23, column=0, sticky="w")
        ttk.Entry(tab, textvariable=self.var_synctime, width=10).grid(row=23, column=1, sticky="w")
        ttk.Checkbutton(tab, text="每周生成学习摘要", variable=self.var_report).grid(
            row=24, column=0, sticky="w")
        ttk.Label(tab, text="（计划任务执行时使用已保存的浏览器登录态，无窗口）",
                  foreground="#666").grid(row=25, column=0, columnspan=3, sticky="w")

        ttk.Label(tab, text="版本与更新", style="Header.TLabel").grid(
            row=26, column=0, sticky="w", pady=(14, 4))
        update_row = ttk.Frame(tab)
        update_row.grid(row=27, column=0, columnspan=3, sticky="w", pady=2)
        self.update_summary_var = tk.StringVar(value="")
        ttk.Label(update_row, textvariable=self.update_summary_var,
                  foreground="#666").pack(side="left")
        ttk.Button(update_row, text="检查更新",
                   command=self.do_check_update).pack(side="left", padx=(10, 4))
        ttk.Button(update_row, text="查看更新公告",
                   command=self.do_show_update).pack(side="left")
        interval = updater.interval_of(self.cfg)
        span = "每次启动都查" if interval <= 0 else f"每 {interval:g} 小时查一次"
        ttk.Checkbutton(
            tab, text=f"启动时自动检查更新（{span}，离线自动跳过）",
            variable=self.var_update).grid(row=28, column=0, columnspan=3, sticky="w")

        btns = ttk.Frame(tab)
        btns.grid(row=29, column=0, columnspan=3, sticky="w", pady=(18, 0))
        ttk.Button(btns, text="保存设置", command=self.do_save_settings).pack(side="left")
        ttk.Button(btns, text="立即生成本周摘要", command=self.do_report).pack(side="left", padx=6)
        ttk.Button(btns, text="一键清理缓存", command=self.do_clean_cache).pack(side="left", padx=6)
        ttk.Button(btns, text="整理讲义目录", command=self.do_tidy).pack(side="left", padx=6)
        ttk.Button(btns, text="打开数据目录", command=self.open_project).pack(side="left")
        ttk.Button(btns, text="创建桌面快捷方式",
                   command=self.do_create_shortcut).pack(side="left", padx=6)
        ttk.Button(btns, text="安装 Chromium", command=self.do_install_browser).pack(
            side="left", padx=(6, 0))

        ttk.Label(
            tab,
            text=f"本程序免安装：配置、登录态与日志都在 {DATA_DIR}（删程序目录不会丢）",
            foreground="#666",
        ).grid(row=30, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(
            tab,
            text=f"浏览器登录态保存在：{PROFILE_DIR}  ·  日志：{LOG_PATH}",
            foreground="#666",
        ).grid(row=31, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self._refresh_course_color_summary()
        self._refresh_update_summary()
        self._apply_update_banner()

        # 内容帧宽度跟随 Canvas；滚动区按内容实际高度计算，
        # 放得下时把滚动条置灰（避免一条永远不动却占宽的滚动条）。
        def _sync(_event: Any = None) -> None:
            canvas.itemconfigure(win, width=canvas.winfo_width())
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)
                tall = bbox[3] - bbox[1] > canvas.winfo_height()
                # ttk.Scrollbar 没有 -state 选项，走 ttk 的 state 子命令
                sb.state(["!disabled"] if tall else ["disabled"])

        def _wheel(event: Any) -> None:
            canvas.yview_scroll(-int(event.delta / 120), "units")

        def _bind_wheel(w: Any) -> None:
            # 滚轮只绑 Canvas 的话，鼠标停在 Entry / Checkbutton 上就滚不动，故递归绑
            w.bind("<MouseWheel>", _wheel)
            for child in w.winfo_children():
                _bind_wheel(child)

        tab.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)
        canvas.bind("<MouseWheel>", _wheel)
        _bind_wheel(tab)

    # ---------- 待办颜色设置 ----------

    @staticmethod
    def _color_display(value: Any) -> str:
        color = deskbox.normalize_color(value)
        if not color:
            return "不设置"
        return f"{_COLOR_ZH.get(color, color)} {color}"

    @staticmethod
    def _color_value(text: str) -> str:
        text = (text or "").strip()
        if not text or text == "不设置":
            return ""
        name = text.split()[-1].lower()
        return name if name in deskbox.TODO_COLORS else ""

    def _refresh_course_color_summary(self) -> None:
        if not hasattr(self, "course_color_var"):
            return
        colors = (self.cfg.get("deskbox") or {}).get("course_colors") or {}
        count = sum(1 for value in colors.values() if deskbox.normalize_color(value))
        self.course_color_var.set(f"已配置 {count} 门" if count else "尚未识别课程")

    def do_edit_course_colors(self) -> None:
        """逐门课修改 DeskBox 颜色；保存后写回 deskbox.course_colors。"""
        db = self.cfg.setdefault("deskbox", {})
        colors = {
            str(cid): color
            for cid, value in (db.get("course_colors") or {}).items()
            if (color := deskbox.normalize_color(value))
        }
        labels = {str(k): str(v) for k, v in (db.get("course_labels") or {}).items() if v}
        for cid, folder in (self.cfg.get("course_map") or {}).items():
            name = Path(str(folder)).name
            if name:
                labels.setdefault(str(cid), name)

        # 还没同步过、但已经读过截止时间时，drafts 里也有课程信息。
        if not colors and self.result is not None:
            courses = []
            seen: set[int] = set()
            for draft in getattr(self.result, "drafts", []) or []:
                course = getattr(draft, "course", None)
                cid = getattr(course, "id", 0)
                if course is not None and cid and cid not in seen:
                    courses.append(course)
                    seen.add(cid)
            if courses:
                colors = deskbox.ensure_course_colors(self.cfg, courses)
                labels = {str(k): str(v) for k, v in (db.get("course_labels") or {}).items() if v}

        selected = [str(i) for i in (self.cfg.get("selected_courses") or [])]
        ids: list[str] = [cid for cid in selected if cid in colors or cid in labels]
        for cid in list(colors) + list(labels):
            if cid not in ids:
                ids.append(cid)
        if not ids:
            messagebox.showinfo(
                APP_NAME,
                "还没有识别到课程。\n请先「读取截止时间」或执行一次同步，再来编辑课程颜色。",
            )
            return

        win = tk.Toplevel(self)
        win.title("编辑课程颜色")
        win.transient(self)
        win.geometry("660x480")
        win.grab_set()

        outer = ttk.Frame(win, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="每门课对应一种 DeskBox 颜色；保存后立即写入配置，下次写 DeskBox 时生效。"
                 "选「不设置」的课程会在下次同步时自动分配。",
            foreground="#666",
        ).pack(anchor="w", pady=(0, 8))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        grid = ttk.Frame(canvas)
        grid_id = canvas.create_window((0, 0), window=grid, anchor="nw")

        choices = ["不设置"] + [self._color_display(c) for c in deskbox.TODO_COLORS]
        vars_: dict[str, tk.StringVar] = {}
        for row, cid in enumerate(ids):
            label = labels.get(cid) or f"课程 {cid}"
            if selected and cid not in selected:
                label += "（历史课程）"
            ttk.Label(grid, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 16), pady=5)
            var = tk.StringVar(value=self._color_display(colors.get(cid)))
            ttk.Combobox(
                grid, textvariable=var, values=choices, state="readonly", width=14,
            ).grid(row=row, column=1, sticky="w", pady=5)
            vars_[cid] = var
        grid.columnconfigure(0, weight=1)

        def _sync(_event: Any = None) -> None:
            canvas.itemconfigure(grid_id, width=canvas.winfo_width())
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)

        def _wheel(event: Any) -> None:
            canvas.yview_scroll(-int(event.delta / 120), "units")

        grid.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)
        canvas.bind("<MouseWheel>", _wheel)

        def auto_assign() -> None:
            manual = self._color_value(self.var_manual_color.get())
            palette = [c for c in deskbox.TODO_COLORS if c != manual] or list(deskbox.TODO_COLORS)
            order = [cid for cid in selected if cid in vars_] or list(vars_)
            for i, cid in enumerate(order):
                vars_[cid].set(self._color_display(palette[i % len(palette)]))

        def save() -> None:
            new_colors: dict[str, str] = {}
            for cid, var in vars_.items():
                color = self._color_value(var.get())
                if color:
                    new_colors[cid] = color
            db["course_colors"] = new_colors
            db["course_labels"] = {k: v for k, v in labels.items() if v}
            save_config(self.cfg)
            self._refresh_course_color_summary()
            if new_colors:
                self.log(f"课程颜色已保存（{len(new_colors)} 门），下次写 DeskBox 时生效")
            else:
                self.log("课程颜色已清空，下次同步会按选课顺序重新分配")
            win.destroy()

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="按选课顺序重新分配", command=auto_assign).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="保存", command=save).pack(side="right", padx=6)

        win.wait_window()

    # ---------- 版本与更新 ----------

    def _refresh_update_summary(self) -> None:
        """设置页那行「当前 vX · 已是最新 / 落后 N 版」。"""
        if not hasattr(self, "update_summary_var"):
            return
        st = self.update_status
        if st is None:
            self.update_summary_var.set(f"当前 v{__version__} · 尚未检查")
            return
        if not st.ok:
            self.update_summary_var.set(f"当前 v{__version__} · 上次检查失败")
            return
        when = (st.checked_at or "")[:16].replace("T", " ")
        if st.has_update:
            self.update_summary_var.set(
                f"当前 v{__version__} → 最新 v{st.latest}（落后 {st.behind} 个版本）")
        else:
            self.update_summary_var.set(
                f"当前 v{__version__} · 已是最新" + (f"（检查于 {when}）" if when else ""))

    def _apply_update_banner(self) -> None:
        """按缓存状态决定顶栏要不要挂「新版本可用」。"""
        banner, _popup = updater.should_notify(self.update_status, load_state())
        if banner and self.update_status is not None:
            self.update_var.set(f"⬆ 新版本 v{self.update_status.latest} 可用，点此查看")
            self.update_label.pack(side="left", padx=(12, 0))
        else:
            self.update_label.pack_forget()

    @staticmethod
    def _mark_update(key: str, version: str) -> None:
        """在 state.json 里记住「已看过 / 已跳过」哪个版本（只存版本号）。"""
        state = load_state()
        info = dict(state.get("update") or {})
        info[key] = version
        state["update"] = info
        save_state(state)

    def _check_update_async(self) -> None:
        """启动时的静默检查。

        不走 ``bg()``：那个是单槽队列，启动阶段已被「院校清单 / 通知」占用，
        再塞一个只会弹「有任务正在进行」。这里自己起线程，结果用单独的
        队列消息回主线程。
        """

        def work() -> None:
            try:
                status = updater.maybe_check(self.cfg)
            except Exception as exc:  # 更新检查永远不许把界面搞崩
                log.exception("更新检查失败")
                status = updater.UpdateStatus(ok=False, error=str(exc), current=__version__)
            if status is not None:
                self.q.put(("update", (status, False)))

        threading.Thread(target=work, daemon=True).start()

    def _announce_upgrade_once(self) -> None:
        """升级后的第一次启动弹一次「已更新到 vX」。

        说明优先用缓存：刚点过一键更新的机器，缓存里就有新版本的 Release，
        所以重启后立刻能看，不用再联网。缓存没有就等 1.5 秒后的那次检查。
        """
        state = load_state()
        upgraded = updater.should_announce_upgrade(state, __version__)
        cached = self.update_status
        has_notes = cached is not None and cached.ok and bool(cached.current_notes)
        # 记下「上次运行的版本」：无论弹没弹，只判断这一次
        self._mark_update("last_run", __version__)
        if not upgraded:
            return
        if has_notes:
            self.log(f"已更新到 v{__version__}，以下是本版的新功能")
            self.do_show_update(auto=True, just_updated=True)
            return
        self._upgrade_notes_pending = True
        self.log(f"已更新到 v{__version__}，正在读取本版更新说明…")

    def _on_update_status(self, status: updater.UpdateStatus, manual: bool) -> None:
        self._update_checking = False
        self.update_status = status
        self._apply_update_banner()
        self._refresh_update_summary()
        if status.ok and not status.has_update:
            # 已是最新：上次下载的更新包留着没用，顺手回收（几十上百 MB）
            freed = updater.cleanup_downloads()
            if freed:
                self.log(f"已清理旧的更新包（{freed / 1048576.0:.1f} MB）")
        if manual:
            if not status.ok:
                messagebox.showwarning(APP_NAME, f"检查更新失败：\n{status.error or '未知原因'}")
            elif not status.has_update:
                self.log(f"已是最新版本（v{__version__}）")
                if status.current_notes:
                    # 顺手把「本版新功能」打开，比一句「已是最新」有用
                    self.do_show_update(auto=True)
                else:
                    messagebox.showinfo(APP_NAME, f"当前已是最新版本（v{__version__}）。")
            else:
                self.log(f"发现新版本 v{status.latest}（当前 v{__version__}）")
                # 手动查就是为了看内容，直接开公告窗，不再多弹一个「请点公告」框
                self.do_show_update(auto=True)
            return
        if status.ok and status.has_update:
            _banner, popup = updater.should_notify(status, load_state())
            self.log(f"发现新版本 v{status.latest}（当前 v{__version__}）")
            if popup or self._upgrade_notes_pending:
                self._upgrade_notes_pending = False
                self.do_show_update(auto=True, just_updated=True)
            return
        if self._upgrade_notes_pending:
            self._upgrade_notes_pending = False
            if status.current_notes:
                self.do_show_update(auto=True, just_updated=True)
            else:
                self.log(f"已更新到 v{__version__}，但这次没取到本版说明"
                         "（可在设置页「查看更新公告」稍后再看）")

    def do_check_update(self) -> None:
        """手动检查：绕过节流，失败会明说。"""
        if self._update_checking:
            return
        self._update_checking = True
        if hasattr(self, "update_summary_var"):
            self.update_summary_var.set(f"当前 v{__version__} · 正在检查…")
        section = self.cfg.get("update") or {}

        def work() -> None:
            try:
                status = updater.check(
                    include_prerelease=bool(section.get("include_prerelease", False)))
            except Exception as exc:
                status = updater.UpdateStatus(ok=False, error=str(exc), current=__version__)
            self.q.put(("update", (status, True)))

        threading.Thread(target=work, daemon=True).start()

    def do_show_update(self, auto: bool = False, just_updated: bool = False) -> None:
        """展示更新说明：有新版就是公告 + 更新入口；已是最新就是「本版新功能」。

        刻意**不用** ``wait_window()``：这个窗是从 ``_pump`` 里弹出来的，
        嵌套事件循环会让队列里的下载进度消息一直排到窗口关掉才处理。
        窗口是 ``grab_set`` 的，交互仍然只能落在它上面。
        """
        st = self.update_status
        if st is None or not st.ok:
            if not auto:
                messagebox.showinfo(
                    APP_NAME, "还没有检查过更新。\n先点「检查更新」联网查一次。")
            return
        has_update = st.has_update
        if not has_update and not st.current_notes:
            if not auto:
                messagebox.showinfo(
                    APP_NAME,
                    f"当前已是最新版本（v{__version__}）。\n这一版还没有发布说明"
                    "（源码运行或该版本未发布时会这样）。",
                )
            return
        if self._update_win is not None and self._update_win.winfo_exists():
            self._update_win.lift()
            self._update_win.focus_force()
            return

        win = tk.Toplevel(self)
        self._update_win = win
        if has_update:
            win.title(f"发现新版本 v{st.latest}")
        else:
            win.title(f"v{__version__} 新功能")
        win.transient(self)
        win.geometry("780x640")
        win.minsize(620, 460)
        win.grab_set()

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        if has_update:
            header = f"当前 v{__version__} → 最新 v{st.latest}（落后 {st.behind} 个版本）"
            intro = "以下是这段时间新增 / 修复的内容："
            body = st.notes_text
        elif just_updated:
            header = f"已更新到 v{__version__}"
            intro = "以下是这一版新增 / 修复的内容（也可以在设置页「查看更新公告」再打开）："
            body = st.current_notes
        else:
            header = f"当前版本 v{__version__}（已是最新）"
            intro = "以下是这一版新增 / 修复的内容："
            body = st.current_notes
        ttk.Label(outer, text=header, style="Header.TLabel").pack(anchor="w")
        when = (st.checked_at or "")[:16].replace("T", " ")
        origin = "来自上次检查的缓存" if st.from_cache else f"检查于 {when}"
        ttk.Label(
            outer,
            text=f"{origin}。{intro}",
            foreground="#666",
        ).pack(anchor="w", pady=(4, 8))

        box = ScrolledText(outer, wrap="word", font=("Microsoft YaHei UI", 9))
        box.pack(fill="both", expand=True)
        box.insert("1.0", body or "（这个版本没有写更新说明）")
        box.configure(state="disabled")

        status_var = tk.StringVar(value="")
        self._update_status_var = status_var
        ttk.Label(outer, textvariable=status_var, foreground="#666").pack(
            anchor="w", pady=(8, 0))

        def close() -> None:
            # 看过了就不再自动弹；顶栏提示保留到装上新版本或点「跳过」。
            # 「本版新功能」窗没什么可标记的，不写 state。
            if has_update:
                self._mark_update("seen", st.latest)
            self._update_status_var = None
            self._update_win = None
            win.destroy()

        def skip() -> None:
            self._mark_update("skipped", st.latest)
            self._apply_update_banner()
            self.log(f"已跳过 v{st.latest} 的更新提示（有更新的版本时会再提醒）")
            self._update_status_var = None
            self._update_win = None
            win.destroy()

        def open_page() -> None:
            target = st.newer[0].url if has_update else (
                st.current_release.url if st.current_release is not None else "")
            with suppress(OSError):
                os.startfile(target or updater.RELEASES_PAGE)

        def start_update() -> None:
            asset = st.asset
            app_dir = updater.current_app_dir()
            reason = (updater.why_not_replace(app_dir) if app_dir is not None
                      else "源码运行，程序不会自我替换")
            if asset is None or app_dir is None or reason:
                # 源码运行 / 没有 Windows 包 / 安装目录不适合替换：
                # 只给下载页，绝不擅自覆盖用户目录
                msg = reason or "这个版本没有可用的 Windows 安装包"
                self.log(f"不自动替换：{msg}")
                status_var.set(f"无法自动更新：{msg}（已打开下载页）")
                open_page()
                return
            dl_btn.configure(state="disabled", text="正在下载…")
            status_var.set("正在下载更新包…")

            def work() -> None:
                def report(got: int, total: int) -> None:
                    text = (f"正在下载 {got / 1048576.0:.1f} / {total / 1048576.0:.1f} MB"
                            if total else f"正在下载 {got / 1048576.0:.1f} MB")
                    self.q.put(("update-progress", text))

                try:
                    path = updater.download(asset, progress=report)
                    self.q.put(("update-progress", "正在解压…"))
                    new_dir = updater.stage(path, st.latest)
                    if not updater.safe_to_replace(app_dir, new_dir):
                        self.q.put(("update-ready", ("manual", new_dir)))
                        return
                    script = updater.write_apply_script(os.getpid(), app_dir, new_dir)
                    self.q.put(("update-ready", script))
                except Exception as exc:
                    self.q.put(("update-error", str(exc)))

            threading.Thread(target=work, daemon=True).start()

        win.protocol("WM_DELETE_WINDOW", close)

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(12, 0))
        if has_update:
            ttk.Button(btns, text="跳过这个版本", command=skip).pack(side="left")
        ttk.Button(btns, text="打开 Release 页面", command=open_page).pack(
            side="left", padx=6)
        ttk.Button(btns, text="以后再说" if has_update else "关闭",
                   command=close).pack(side="right")
        if has_update:
            app_dir_now = updater.current_app_dir()
            block = (updater.why_not_replace(app_dir_now) if app_dir_now is not None
                     else "源码运行，程序不会自我替换")
            can_update = st.asset is not None and app_dir_now is not None and not block
            if not can_update:
                # 提前把「为什么不能一键更新」说清楚，别等用户点了才发现
                status_var.set(f"当前环境不支持自动替换：{block or '没有可用的安装包'}")
            dl_btn = ttk.Button(
                btns,
                text="立即下载并更新" if can_update else "打开下载页",
                command=start_update,
            )
            dl_btn.pack(side="right", padx=6)

    def _on_update_ready(self, payload) -> None:
        """下载 + 解压完成：确认后拉起替换脚本并退出。"""
        if isinstance(payload, tuple):
            _kind, new_dir = payload
            self.log(f"更新包已解压到 {new_dir}，但安装目录不适合自动替换")
            if messagebox.askyesno(
                APP_NAME,
                "已下载并解压到：\n"
                f"{new_dir}\n\n"
                "当前安装目录里除了程序文件还有别的东西，自动替换会一并删掉，"
                "所以改由你手动覆盖。\n\n现在打开该文件夹吗？",
            ):
                with suppress(OSError):
                    os.startfile(str(new_dir))
            return
        script = payload
        if not messagebox.askyesno(
            APP_NAME,
            "更新已就绪。\n\n"
            "将关闭本程序 → 用新版本覆盖安装目录 → 自动重新打开。\n"
            "配置、登录态与课程文件都在 %LOCALAPPDATA%，不受影响。\n\n现在更新吗？",
        ):
            self.log(f"更新包已就绪，未执行替换。脚本：{script}")
            return
        if not updater.launch_apply(script):
            messagebox.showerror(
                APP_NAME,
                "无法启动更新脚本。\n请从 Release 页面下载最新 zip 手动解压覆盖。",
            )
            return
        self.destroy()

    # ---------- 交互 ----------

    def _browse(self, var: tk.StringVar, kind: str) -> None:
        if kind == "dir":
            p = filedialog.askdirectory(initialdir=var.get() or "D:\\")
        else:
            p = filedialog.askopenfilename(initialdir=str(Path(var.get()).parent))
        if p:
            var.set(p)

    def _toggle_row(self, _event) -> None:
        item = self.tree.focus()
        if not item:
            return
        vals = list(self.tree.item(item, "values"))
        vals[0] = "✔" if vals[0] != "✔" else ""
        self.tree.item(item, values=vals)
        self._save_selection()

    def _select_all(self, on: bool) -> None:
        for item in self.tree.get_children():
            vals = list(self.tree.item(item, "values"))
            vals[0] = "✔" if on else ""
            self.tree.item(item, values=vals)
        self._save_selection()

    def _save_selection(self) -> None:
        selected = []
        for item in self.tree.get_children():
            vals = self.tree.item(item, "values")
            if vals[0] == "✔":
                selected.append(int(vals[1]))
        self.cfg["selected_courses"] = selected
        save_config(self.cfg)
        self._set_status(f"已选 {len(selected)} 门课程")

    def _checked_courses(self) -> list[Course]:
        ids = {int(self.tree.item(i, "values")[1]) for i in self.tree.get_children()
               if self.tree.item(i, "values")[0] == "✔"}
        return [c for c in self.courses if c.id in ids]

    def open_canvas(self) -> None:
        # ShellExecute 直接开链接。原来是 `cmd /c start`，而 GUI 没有控制台，
        # cmd 就会临时新建一个可见控制台——点一下闪一个黑框。
        home = canvas_home()
        if home:
            os.startfile(home + "/")
        else:
            messagebox.showinfo(APP_NAME, "请先在设置页填写「Canvas 站点地址」。")

    def do_create_shortcut(self) -> None:
        """在桌面放一个快捷方式：exe 版指 exe，源码版指 pythonw + CanvasHelper.pyw。"""
        def job():
            return shortcut.create(shortcut.target_for())

        def done(result):
            ok, info = result
            if ok:
                self.log(f"桌面快捷方式已创建：{info}")
                messagebox.showinfo(APP_NAME, f"已在桌面创建快捷方式：\n{info}")
            else:
                self.log(info)
                messagebox.showwarning(
                    APP_NAME,
                    f"{info}\n\n也可以手动创建：右键程序 → 发送到 → 桌面快捷方式。",
                )

        self.bg(job, on_done=done)

    def _check_schedule_paths(self) -> None:
        """程序被挪过位置（或源码版换成了 exe）后，旧计划任务会静默失效。

        注册时把命令记在 ``state.schedule_cmd`` 里，这里只做字符串比对；
        不一致才重建，平时不产生任何子进程。
        """
        schedule = self.cfg.get("schedule") or {}
        if not schedule.get("enabled"):
            return
        try:
            stale = scheduler.moved()
        except Exception as exc:  # 读 state 失败不该影响启动
            log.warning("检查计划任务路径失败: %s", exc)
            return
        if not stale:
            return
        self.log("程序路径变了，正在按当前位置重建计划任务…")
        try:
            for line in scheduler.refresh(schedule):
                self.log(line)
        except Exception as exc:
            log.warning("重建计划任务失败: %s", exc)
            self.log(f"计划任务重建失败：{exc}（可在设置页重新保存一次）")

    # ---------- 院校选择 ----------

    def _first_run_pick(self) -> None:
        """首次运行：没配过站点地址 -> 先选学校。"""
        url = self.do_pick_school(ask_login=True)
        if not url:
            self.log("尚未选择院校。可点右上角「选择院校」随时设置。")
            return

    def do_pick_school(self, ask_login: bool = False) -> str:
        """弹出院校选择窗口；选中后写入配置并（可选）直接进入登录。"""
        from .schoolpicker import pick_school

        url = pick_school(self, current=canvas_home())
        if not url:
            return ""
        self.cfg["canvas_home"] = url
        save_config(self.cfg)
        self.var_home.set(url)
        self.log(f"已选择 Canvas 站点：{url}")
        self._set_status(f"已选择：{url}")
        if ask_login:
            # 选完学校直接进入该校的登录流程
            self.do_load_courses()
        return url

    def open_project(self) -> None:
        from .paths import DATA_DIR

        os.startfile(DATA_DIR)

    def _refresh_deskbox_state(self) -> None:
        running = deskbox.is_running()
        self.deskbox_var.set("DeskBox：运行中" if running else "DeskBox：未运行")
        self.after(4000, self._refresh_deskbox_state)

    # ---------- 动作 ----------

    def do_load_courses(self) -> None:
        from .auth import CanvasSession
        from .canvas_api import CanvasAPI

        def job():
            s = CanvasSession()
            try:
                if not s.ensure_login(on_status=self._progress):
                    raise RuntimeError("登录失败或超时")
                api = CanvasAPI(s)
                cs = api.courses()
                return cs, s.profile
            finally:
                s.close()

        def done(payload):
            courses, profile = payload
            self.courses = courses
            name = (profile or {}).get("name") or (profile or {}).get("login_id") or ""
            self.login_var.set(f"已登录：{name}" if name else "已登录")
            folders = course_dirs(Path(self.cfg["classes_root"]))
            self.course_folders = {}
            self.tree.delete(*self.tree.get_children())
            sel = set(self.cfg.get("selected_courses", []))
            for c in courses:
                folder = match_folder(c.short, folders)
                self.course_folders[c.id] = folder
                checked = "✔" if (c.id in sel or (not sel and folder)) else ""
                self.tree.insert("", "end", values=(
                    checked, c.id, f"{c.short} · {c.name}",
                    folder.name if folder else "（未匹配，将自动新建）", "",
                ))
            self.log(f"识别到 {len(courses)} 门课程")
            if not sel:
                self._save_selection()

        self.bg(job, on_done=done)

    def _fill_counts(self, res) -> None:
        counts = {p.course.id: len(p.files) for p in getattr(res, "plans", [])}
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if int(vals[1]) in counts:
                vals[4] = str(counts[int(vals[1])])
                self.tree.item(iid, values=vals)

    def do_preview(self) -> None:
        if not self._checked_courses():
            messagebox.showinfo(APP_NAME, "请先勾选至少一门课程。")
            return
        ids = [c.id for c in self._checked_courses()]

        def job():
            return runner.run_sync(self.cfg, course_ids=ids, dry_run=True,
                                   progress=self._progress)

        def done(res):
            self.result = res
            self.preview.delete("1.0", "end")
            self.preview.insert("end", res.text or "（无内容）")
            if res.errors:
                self.preview.insert("end", "\n\n警告：\n" + "\n".join(res.errors))
            self._fill_counts(res)
            self._fill_todo_tree(res.drafts, status="待写入")
            self._refresh_course_color_summary()

        self.bg(job, on_done=done)

    def do_sync(self) -> None:
        if not self._checked_courses():
            messagebox.showinfo(APP_NAME, "请先勾选至少一门课程。")
            return
        ids = [c.id for c in self._checked_courses()]
        if deskbox.is_running() and not messagebox.askyesno(
            APP_NAME,
            "同步过程中如需写入新待办，DeskBox 会被短暂关闭并立即重启（约 1 秒）。\n\n是否继续？",
        ):
            return

        def job():
            return runner.run_sync(self.cfg, course_ids=ids, dry_run=False,
                                   progress=self._progress)

        def done(res):
            self.result = res
            self.preview.delete("1.0", "end")
            self.preview.insert("end", res.text or "（无内容）")
            s = res.stats
            summary = (f"\n\n下载 {s.get('downloaded', 0)} 个文件 + {s.get('pages', 0)} 个页面，"
                       f"已存在 {s.get('exists', 0)} 项，错误 {s.get('errors', 0)} 个")
            if s.get("archives"):
                summary += (f"\n解压 {s['archives']} 个压缩包"
                            f"（+{s.get('archive_files', 0)} 个文件）")
            mode = {"swapped": "DeskBox 已重启写入", "applied": "已直接写入",
                    "queued": "待办已排队", "none": "无新增待办", "disabled": "待办功能已关闭"}
            summary += f"\n待办：{mode.get(res.todo_mode, res.todo_mode)}（新增 {len(res.todo_added)} 条）"
            ts = res.todo_stats or {}
            if ts.get("recolored"):
                summary += f"，更新 {ts['recolored']} 条颜色"
            if ts.get("resorted"):
                summary += f"，重排 {ts['resorted']} 条"
            summary += f"\n未读通知 {len(res.unread)} 条；成绩文件 {len(res.grade_files)} 个"
            if res.ics_path:
                summary += f"\n日历：{res.ics_path}"
            if res.errors:
                summary += "\n警告：\n" + "\n".join(res.errors)
            self.preview.insert("end", summary)
            self._fill_counts(res)
            self._fill_todo_tree(res.drafts, res)
            self._fill_grades(res)
            # 通知正文只在内存里：同步拉到了就刷新显示，没拉（本轮回调失败
            # 或关掉了通知聚合）则保留窗口上已有的内容，不清空
            got = getattr(res, "notices", None)
            if got is not None:
                self.notices = self._serialize_notices(got)
                self._refresh_notice_tree()
            self._refresh_course_color_summary()

        self.bg(job, on_done=done)

    def do_load_deadlines(self) -> None:
        def job():
            return runner.collect_deadlines(self.cfg, progress=self._progress)

        def done(drafts):
            self._fill_todo_tree(drafts, status="待写入")
            if self.result is None:
                from types import SimpleNamespace
                self.result = SimpleNamespace(drafts=drafts)
            else:
                self.result.drafts = drafts
            self._refresh_course_color_summary()

        self.bg(job, on_done=done)

    def _fill_todo_tree(self, drafts, res=None, status: str = "待写入") -> None:
        self.todo_tree.delete(*self.todo_tree.get_children())
        added_ids = set()
        if res is not None:
            added_ids = {id(d) for d in getattr(res, "todo_added", [])}
        done_n = exist_n = 0
        for d in drafts:
            if d.status == "completed":
                state = "已完成"
                done_n += 1
            elif d.status == "reopened":
                state = "截止已更新"
            elif id(d) in added_ids:
                state = "新增"
            elif d.status == "existing" or getattr(d, "existing", False):
                state = "已存在"
                exist_n += 1
            else:
                state = status
            self.todo_tree.insert("", "end", values=(
                f"{d.due:%Y-%m-%d %H:%M}" if d.due else "（无截止）",
                d.course.short, d.text, d.kind, state,
            ))
        msg = f"待办列表：{len(drafts)} 项"
        if done_n:
            msg += f"（其中 {done_n} 项已完成，不会重复添加）"
        if exist_n:
            msg += f"，{exist_n} 项已存在"
        self.log(msg)

    def do_write_todos(self) -> None:
        if self.result is None:
            messagebox.showinfo(APP_NAME, "请先“读取截止时间”或执行同步。")
            return
        drafts = getattr(self.result, "drafts", None) or []
        running = deskbox.is_running()
        if running and not messagebox.askyesno(
            APP_NAME,
            "将执行：写入副本 → 关闭 DeskBox → 替换 → 重启（停机约 1 秒）。\n是否继续？",
        ):
            return

        def job():
            todo_path = Path(self.cfg["deskbox"]["todo_path"])
            tmp, added, st = deskbox.prepare(todo_path, drafts, self.cfg["deskbox"])
            if not st.get("changed"):
                return {"added": 0, "mode": "none", "st": st}
            if deskbox.is_running():
                deskbox.quick_swap(
                    todo_path, tmp, Path(self.cfg["deskbox"]["exe_path"]),
                    prepared_count=st["total_items"], drafts=added,
                    offset_minutes=int(self.cfg["deskbox"].get("reminder_offset_minutes", 1440)),
                    progress=self._progress,
                    cfg=self.cfg["deskbox"],
                )
                mode = "swapped"
            else:
                deskbox.apply_copy(todo_path, tmp)
                mode = "applied"
            return {"added": len(added), "mode": mode, "st": st}

        def done(info):
            st = info.get("st") or {}
            if info["mode"] == "none":
                msg = "无新增待办"
                if st.get("completed"):
                    msg += f"（{st['completed']} 项已完成，未重复添加）"
                if st.get("existing"):
                    msg += f"，{st['existing']} 项已存在"
            else:
                msg = f"新增 {info['added']} 条待办"
                if st.get("completed"):
                    msg += f"，{st['completed']} 项已完成未添加"
                if st.get("deduped"):
                    msg += f"，清理重复 {st['deduped']} 条"
                if st.get("recolored"):
                    msg += f"，更新 {st['recolored']} 条颜色"
                if st.get("resorted"):
                    msg += f"，重排 {st['resorted']} 条"
                msg += "（DeskBox 已重启）" if info["mode"] == "swapped" else "（已写入文件）"
            self.log(msg)
            messagebox.showinfo(APP_NAME, msg)
            self.do_load_deadlines()

        self.bg(job, on_done=done)

    def do_install_browser(self) -> None:
        """下载 Playwright 自带的 Chromium：只有系统没有 Edge 时才需要。"""
        from .auth import install_chromium

        if not messagebox.askyesno(
                APP_NAME,
                "只有系统里没有 Microsoft Edge 时才需要这一步。\n\n"
                "将下载约 150 MB 的 Chromium 到本机，不影响已有文件与登录态。继续？"):
            return

        def done(ok):
            self.log("Chromium 已就绪，可以重新登录了" if ok
                     else "Chromium 下载失败，详见上方日志；也可以直接装 Microsoft Edge")
            messagebox.showinfo(APP_NAME, "Chromium 已就绪，重新登录即可" if ok
                                else "下载失败，详见日志")

        self.bg(lambda: install_chromium(progress=self._progress), on_done=done)

    def do_clean_cache(self) -> None:
        """一键清理缓存：勾选 -> 确认 -> 后台执行。"""
        from . import cleaner

        targets = cleaner.scan()
        if not targets:
            messagebox.showinfo(APP_NAME, "没有可清理的缓存。")
            return

        win = tk.Toplevel(self)
        win.title("一键清理缓存")
        win.geometry("600x520")
        win.transient(self)
        win.grab_set()

        ttk.Label(win, text="选择要清理的项目（默认只清可再生的缓存）",
                  style="Header.TLabel").pack(anchor="w", padx=16, pady=(14, 6))

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=16)
        vars_map: dict = {}
        total_var = tk.StringVar()

        # 先定义再使用：Checkbutton 的 command 在创建时立即求值，
        # 若留成 `lambda: _update_total()` 就得靠「调用时才查名」才能成立，太隐晦
        def _selected() -> list:
            return [t for k, (v, t) in vars_map.items() if v.get()]

        def _update_total() -> None:
            total_var.set(f"合计约 {cleaner.fmt_size(sum(t.bytes for t in _selected()))}"
                          f" · {sum(t.files for t in _selected())} 个文件")

        for t in targets:
            var = tk.BooleanVar(value=t.default)
            vars_map[t.key] = (var, t)
            box = ttk.Frame(body)
            box.pack(fill="x", pady=3)
            text = f"{'⚠ ' if t.destructive else ''}{t.label}   （{t.size_text}"
            text += f"，{t.files} 个文件）" if t.files else "）"
            ttk.Checkbutton(box, text=text, variable=var,
                            command=_update_total).pack(anchor="w")
            ttk.Label(box, text=t.detail, foreground="#666",
                      wraplength=540).pack(anchor="w", padx=22)

        _update_total()
        ttk.Label(win, textvariable=total_var, foreground="#333").pack(
            anchor="w", padx=16, pady=(8, 0))
        ttk.Label(
            win,
            text="默认不清理登录态；勾了「登录态」才需要重新登录。",
            foreground="#666", wraplength=540,
        ).pack(anchor="w", padx=16, pady=(2, 0))

        def start() -> None:
            chosen = _selected()
            if not chosen:
                messagebox.showinfo(APP_NAME, "请至少勾选一项。", parent=win)
                return
            lines = [f"将清理 {len(chosen)} 项，约 {cleaner.fmt_size(sum(t.bytes for t in chosen))}："]
            lines += [f"  · {t.label}" for t in chosen]
            warn = [t.label for t in chosen if t.destructive]
            if warn:
                lines += ["", "以下项目不可恢复，请确认：", "  · " + "\n  · ".join(warn)]
            if not messagebox.askyesno(APP_NAME, "\n".join(lines), parent=win):
                return
            keys = {t.key for t in chosen}
            win.destroy()

            def job():
                return cleaner.run(keys, progress=self._progress)

            def ok(rep: dict) -> None:
                for m in rep.get("messages", []):
                    self.log(m)
                self.log(f"清理完成：释放 {cleaner.fmt_size(rep.get('bytes', 0))}"
                         f"，{rep.get('files', 0)} 个文件")
                if rep.get("skipped"):
                    self.log(f"另有 {rep['skipped']} 个文件被占用，已跳过")

            self.bg(job, on_done=ok)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=16, pady=14)
        ttk.Button(bar, text="开始清理", command=start).pack(side="right")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=6)

    def do_tidy(self) -> None:
        """整理讲义目录：先列出移动方案，确认后才动文件。"""
        from . import tidy

        root = Path(self.cfg.get("classes_root") or "")
        if not root.is_dir():
            messagebox.showinfo(APP_NAME, "课程根目录不存在，请先在上面选择。")
            return
        plans = [tidy.plan_course(d) for d in course_dirs(root).values()]
        text = tidy.render(plans)
        pending = sum(len(p.todo) for p in plans)
        if not pending:
            messagebox.showinfo(APP_NAME, text)
            return

        win = tk.Toplevel(self)
        win.title("整理讲义目录")
        win.geometry("760x480")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text="以下是移动方案（只移动、不删除；确认后才执行）",
                  style="Header.TLabel").pack(anchor="w", padx=16, pady=(14, 6))
        box = ScrolledText(win, font=("Consolas", 9))
        box.pack(fill="both", expand=True, padx=16)
        box.insert("end", text)
        box.configure(state="disabled")

        def start() -> None:
            if not messagebox.askyesno(
                    APP_NAME,
                    f"将移动 {pending} 个文件到各自章节目录。\n\n"
                    "只移动、不删除；重名自动加 (1)。继续？", parent=win):
                return
            win.destroy()

            def job():
                return tidy.run(plans, progress=self._progress)

            def ok(rep: dict) -> None:
                msg = (f"讲义整理完成：移动 {rep['moved']} 个，跳过 {rep['skipped']} 个"
                       + (f"，失败 {rep['errors']} 个" if rep.get("errors") else "")
                       + "（未删除任何文件）")
                self.log(msg)
                messagebox.showinfo(APP_NAME, msg)

            self.bg(job, on_done=ok)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=16, pady=14)
        ttk.Button(bar, text=f"执行移动（{pending} 项）", command=start).pack(side="right")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=6)

    def do_export_ics(self) -> None:
        drafts = getattr(self.result, "drafts", None) if self.result else None
        if not drafts:
            messagebox.showinfo(APP_NAME, "没有可导出的截止时间。")
            return
        from .extras import export_ics

        path = export_ics(drafts)
        self.log(f"已导出 {path}")
        if messagebox.askyesno(APP_NAME, f"已导出到：\n{path}\n\n是否现在打开？"):
            os.startfile(path)

    def do_load_notices(self) -> None:
        def job():
            return runner.collect_notice_list(self.cfg, progress=self._progress)

        def done(payload):
            notices, unread = payload
            self.notices = self._serialize_notices(notices)
            self.unread_var.set(f"共 {len(notices)} 条 · 未读 {unread} 条")
            self._refresh_notice_tree()

        self.bg(job, on_done=done)

    @staticmethod
    def _serialize_notices(notices: list) -> list[dict]:
        """把 Notice 转成渲染用的 dict —— 通知只留一份在内存，不写 state.json。"""
        return [
            {"posted_at": n.posted_at, "course": n.course, "title": n.title,
             "url": n.url, "kind": n.kind}
            for n in notices
        ]

    def _refresh_notice_tree(self) -> None:
        self.notice_tree.delete(*self.notice_tree.get_children())
        label = {"announcement": "公告", "conversation": "消息", "activity": "动态"}
        for n in self.notices:
            # 归一化成 str：dict.get 在 mypy 眼里是 Any|None，而表头文案的
            # .get 要的是 str；顺带避免 kind 缺失/为 None 时把 None 塞进表格
            kind = str(n.get("kind") or "")
            self.notice_tree.insert("", "end", values=(
                n.get("posted_at", ""), n.get("course", ""), n.get("title", ""),
                label.get(kind, kind),
            ))

    def _open_notice(self) -> None:
        item = self.notice_tree.focus()
        if not item:
            return
        idx = self.notice_tree.index(item)
        if idx < len(self.notices) and self.notices[idx].get("url"):
            os.startfile(self.notices[idx]["url"])

    def _fill_grades(self, res) -> None:
        self.grade_tree.delete(*self.grade_tree.get_children())
        courses = {c.id: c for c in getattr(res, "courses", [])}
        for cid, data in (getattr(res, "grades", {}) or {}).items():
            c = courses.get(cid)
            code = c.short if c else str(cid)
            cur = data.get("current")
            if cur is not None:
                pct = cur * 100 if cur <= 1 else cur
                self.grade_tree.insert("", "end", values=(code, "★ 当前总评分", f"{pct:.1f}%", ""))
            for r in data.get("rows", []):
                self.grade_tree.insert("", "end", values=(
                    code, r.name,
                    "" if r.score is None else f"{r.score:g}",
                    "" if r.possible is None else f"{r.possible:g}",
                ))

    def do_report(self) -> None:
        def job():
            return runner.run_report(self.cfg, progress=self._progress)

        def done(paths):
            if paths:
                self.log("已生成摘要：")
                for p in paths:
                    self.log("  " + p)
                messagebox.showinfo(APP_NAME, "已生成：\n" + "\n".join(paths))
            else:
                messagebox.showinfo(APP_NAME, "没有生成任何摘要（请先登录并勾选课程）。")

        self.bg(job, on_done=done)

    def do_save_settings(self) -> None:
        self.cfg["classes_root"] = self.var_root.get().strip()
        self.cfg["canvas_home"] = self.var_home.get().strip().rstrip("/")
        # 时区留空 = 跟随本机系统时区；课程代码正则留空 = 自动识别
        self.cfg["timezone"] = self.var_tz.get().strip()
        self.cfg["course_code_pattern"] = self.var_ccode.get().strip()
        self.cfg["deskbox"]["todo_path"] = self.var_todo.get().strip()
        self.cfg["deskbox"]["exe_path"] = self.var_exe.get().strip()
        self.cfg["deskbox"]["fix_attachment_paths"] = self.var_fix.get()
        self.cfg["deskbox"]["sort_by_due"] = self.var_sort.get()
        self.cfg["deskbox"]["manual_color"] = self._color_value(self.var_manual_color.get())
        self.cfg["deskbox"]["background_policy"] = self.var_policy.get()
        upd = self.cfg.setdefault("update", {})
        upd["check_on_start"] = self.var_update.get()
        self.cfg["extras"]["ics"] = self.var_ics.get()
        self.cfg["extras"]["grades"] = self.var_grades.get()
        self.cfg["extras"]["notices"] = self.var_notices.get()
        self.cfg.setdefault("archives", {})["extract"] = self.var_extract.get()
        # 「保存讨论区内容」是总开关：快照与链接下载一起启停
        enabled = self.var_discuss.get()
        dsc = self.cfg.setdefault("discussions", {})
        dsc["links"] = enabled
        dsc["snapshot"] = enabled
        self.cfg["schedule"]["enabled"] = self.var_sched.get()
        self.cfg["schedule"]["sync_time"] = self.var_synctime.get().strip() or "08:30"
        self.cfg["schedule"]["report_enabled"] = self.var_report.get()
        save_config(self.cfg)
        self.log("设置已保存")

        if self.var_sched.get():
            ok, msg = scheduler.enable(self.cfg["schedule"])
            self.log(msg)
            if not ok:
                messagebox.showwarning(APP_NAME, "计划任务注册失败：\n" + msg)
                self.var_sched.set(False)
                self.cfg["schedule"]["enabled"] = False
                save_config(self.cfg)
            else:
                messagebox.showinfo(APP_NAME, "计划任务已注册：\n" + msg)
        else:
            ok, msg = scheduler.disable()
            if ok:
                self.log("计划任务已删除")


def main() -> None:
    from .paths import ensure_dirs

    ensure_dirs()
    app = App()
    app.mainloop()
