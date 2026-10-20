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

from . import APP_NAME, __version__, deskbox, runner, scheduler
from .canvas_api import Course
from .classify import course_dirs, match_folder
from .paths import (
    LOG_PATH,
    PROFILE_DIR,
    canvas_home,
    get_logger,
    load_config,
    load_state,
    now_local,
    save_config,
)

log = get_logger()

FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")


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

        self._build_ui()
        self.after(100, self._pump)
        self.after(300, self._startup)

    # ---------- 基础设施 ----------

    def _startup(self) -> None:
        state = load_state()
        self._set_status(f"上次同步：{state.get('last_sync') or '（从未）'}")
        # 院校清单在线更新：后台静默拉取，不阻塞界面（缓存过期才拉）
        self._refresh_school_list_async()
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
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _finish(self) -> None:
        self._busy = False
        self.progress_bar.stop()
        self._set_status("就绪")

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
        ttk.Button(head, text="登录 / 刷新课程", command=self.do_load_courses).pack(side="right")
        ttk.Button(head, text="打开 Canvas", command=self.open_canvas).pack(side="right", padx=6)
        ttk.Button(head, text="选择院校", command=self.do_pick_school).pack(side="right", padx=6)

        # 标签页
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._build_sync_tab()
        self._build_todo_tab()
        self._build_notice_tab()
        self._build_grade_tab()
        self._build_settings_tab()

        # 日志 + 状态栏
        bottom = ttk.Frame(self, padding=(12, 0, 12, 8))
        bottom.pack(fill="both", expand=False)
        self.log_text = ScrolledText(bottom, height=6, font=("Consolas", 9), state="normal")
        self.log_text.pack(fill="x")
        bar = ttk.Frame(bottom)
        bar.pack(fill="x", pady=(6, 0))
        self.progress_bar = ttk.Progressbar(bar, mode="indeterminate", length=200)
        self.progress_bar.pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=10)

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
        tab = ttk.Frame(self.nb, padding=14)
        self.nb.add(tab, text=" 设置 ")

        self.var_root = tk.StringVar(value=self.cfg["classes_root"])
        self.var_home = tk.StringVar(value=self.cfg.get("canvas_home") or "")
        self.var_tz = tk.StringVar(value=self.cfg.get("timezone") or "")
        self.var_ccode = tk.StringVar(value=self.cfg.get("course_code_pattern") or "")
        self.var_todo = tk.StringVar(value=self.cfg["deskbox"]["todo_path"])
        self.var_exe = tk.StringVar(value=self.cfg["deskbox"]["exe_path"])
        self.var_fix = tk.BooleanVar(value=self.cfg["deskbox"]["fix_attachment_paths"])
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

        ttk.Label(tab, text="后台（计划任务）同步时 DeskBox 待办处理方式",
                  style="Header.TLabel").grid(row=14, column=0, sticky="w", pady=(14, 4))
        for i, (val, text) in enumerate((
            ("queue", "排队：不打扰 DeskBox，下次打开本程序时写入（推荐）"),
            ("restart", "立即重启 DeskBox 完成写入"),
            ("skip", "跳过：后台同步不写待办"),
        )):
            ttk.Radiobutton(tab, text=text, value=val, variable=self.var_policy).grid(
                row=15 + i, column=0, columnspan=3, sticky="w")

        ttk.Label(tab, text="计划任务", style="Header.TLabel").grid(
            row=18, column=0, sticky="w", pady=(14, 4))
        ttk.Checkbutton(tab, text="启用每日自动同步", variable=self.var_sched).grid(
            row=19, column=0, sticky="w")
        ttk.Entry(tab, textvariable=self.var_synctime, width=10).grid(row=19, column=1, sticky="w")
        ttk.Checkbutton(tab, text="每周生成学习摘要", variable=self.var_report).grid(
            row=20, column=0, sticky="w")
        ttk.Label(tab, text="（计划任务执行时使用已保存的浏览器登录态，无窗口）",
                  foreground="#666").grid(row=21, column=0, columnspan=3, sticky="w")

        btns = ttk.Frame(tab)
        btns.grid(row=22, column=0, columnspan=3, sticky="w", pady=(18, 0))
        ttk.Button(btns, text="保存设置", command=self.do_save_settings).pack(side="left")
        ttk.Button(btns, text="立即生成本周摘要", command=self.do_report).pack(side="left", padx=6)
        ttk.Button(btns, text="一键清理缓存", command=self.do_clean_cache).pack(side="left", padx=6)
        ttk.Button(btns, text="整理讲义目录", command=self.do_tidy).pack(side="left", padx=6)
        ttk.Button(btns, text="打开数据目录", command=self.open_project).pack(side="left")

        ttk.Label(
            tab,
            text=f"浏览器登录态保存在：{PROFILE_DIR}  ·  日志：{LOG_PATH}",
            foreground="#666",
        ).grid(row=23, column=0, columnspan=3, sticky="w", pady=(12, 0))

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
        drafts = getattr(self.result, "drafts", None) if self.result else None
        if not drafts:
            messagebox.showinfo(APP_NAME, "请先“读取截止时间”或执行同步。")
            return
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
                msg += "（DeskBox 已重启）" if info["mode"] == "swapped" else "（已写入文件）"
            self.log(msg)
            messagebox.showinfo(APP_NAME, msg)
            self.do_load_deadlines()

        self.bg(job, on_done=done)

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
        self.cfg["deskbox"]["background_policy"] = self.var_policy.get()
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
