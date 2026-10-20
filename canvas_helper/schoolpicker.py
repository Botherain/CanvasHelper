"""「选择院校」窗口：让用户先选学校，再进该校的 Canvas 登录页。

为什么需要它：Canvas 在全球有数千所院校在用，站点地址五花八门
（``canvas.<校名>.edu``、``<缩写>.instructure.com``、``canvas.<校名>.ac.uk``……）。
让用户自己猜域名是最大的上手门槛，所以这里给一份**已核实**的清单供搜索，
同时也允许直接手填地址——清单只是起点，不在清单里的学校一样能用。

程序本身不含任何与具体院校绑定的逻辑：选完只得到一个站点地址，
后续登录、课程读取全部走 :mod:`canvas_helper.auth` 的通用流程。
"""

from __future__ import annotations

import tkinter as tk
from contextlib import suppress
from tkinter import messagebox, ttk

from . import APP_NAME
from .paths import get_logger
from .schools import SCHOOLS, regions, search

log = get_logger()

FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")


def normalize_home(text: str) -> str:
    """把用户输入的地址整理成规范形式。

    - 补全缺失的 ``https://``（用户多半直接敲域名）
    - 去掉结尾斜杠、去掉 ``/login`` 这类路径（用户常从浏览器地址栏复制）
    - 去掉首尾空白与可能的引号
    """
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    scheme, _, rest = raw.partition("://")
    scheme = scheme.lower() or "https"
    rest = rest.strip("/")
    # 只保留主机 + 路径，去掉查询串与片段
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    # 用户常把登录页地址整段贴进来 -> 只取到第一个斜杠为止
    for suffix in ("/login", "/login/", "/users/auth", "/users/sign_in"):
        if rest.lower().endswith(suffix):
            rest = rest[: -len(suffix)]
            break
    rest = rest.rstrip("/")
    if not rest:
        return ""
    return f"{scheme}://{rest}"


class SchoolPicker(tk.Toplevel):
    """院校选择窗口：搜索清单 / 手填地址 / 选地区筛选。

    用法::

        dlg = SchoolPicker(parent, current="https://canvas.example.edu")
        dlg.wait_window()
        url = dlg.selected_url      # 选中或手填的规范地址（没选则空串）
    """

    def __init__(self, parent: tk.Tk, current: str = "") -> None:
        super().__init__(parent)
        self.title(f"选择你的学校 · {APP_NAME}")
        self.transient(parent)
        self.geometry("760x560")
        self.minsize(640, 460)
        self.selected_url = ""
        self._current = current

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)

        # 标题沿用主界面的 Header.TLabel 样式（独立运行时兜底注册一次）
        style = ttk.Style(self)
        with suppress(tk.TclError):
            style.configure("Header.TLabel", font=FONT_BOLD)
        ttk.Label(body, text="先选学校，再进该校的 Canvas 登录页",
                  style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            body,
            text="清单里的地址均已核实确实是 Canvas；找不到你的学校就直接手填地址。",
            foreground="#666").grid(row=1, column=0, sticky="w", pady=(2, 8))

        # --- 搜索 + 地区 ---
        top = ttk.Frame(body)
        top.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="搜索").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.var_q = tk.StringVar()
        self.var_q.trace_add("write", lambda *_: self._refresh())
        entry = ttk.Entry(top, textvariable=self.var_q)
        entry.grid(row=0, column=1, sticky="ew")
        entry.focus_set()
        ttk.Label(top, text="地区").grid(row=0, column=2, sticky="w", padx=(12, 6))
        self.var_region = tk.StringVar(value="全部")
        reg_box = ttk.Combobox(top, textvariable=self.var_region, state="readonly",
                               width=16, values=["全部", *regions()])
        reg_box.grid(row=0, column=3, sticky="w")
        reg_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh())

        # --- 列表 ---
        wrap = ttk.Frame(body)
        wrap.grid(row=3, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(wrap, columns=("name", "url"), show="headings", height=16)
        self.tree.heading("name", text="院校")
        self.tree.heading("url", text="Canvas 地址")
        self.tree.column("name", width=380, anchor="w")
        self.tree.column("url", width=320, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda _e: self._on_choose())
        self.tree.bind("<Return>", lambda _e: self._on_choose())
        # 在清单里选了就把地址同步到手填框——否则「选了 A 但手填框里还留着旧的 B」
        # 会让人以为选了 A 其实存的是 B
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # --- 手动输入 ---
        manual = ttk.LabelFrame(body, text="不在清单里？直接填地址", padding=10)
        manual.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        manual.columnconfigure(1, weight=1)
        ttk.Label(manual, text="地址").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.var_manual = tk.StringVar(value=current)
        ttk.Entry(manual, textvariable=self.var_manual).grid(row=0, column=1, sticky="ew")
        ttk.Label(
            manual,
            text="例：canvas.example.edu（不用写 https://）",
            foreground="#666").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- 按钮 ---
        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="使用选中 / 填写的地址", command=self._on_choose).pack(side="right")
        ttk.Button(btns, text="取消", command=self._on_cancel).pack(side="right", padx=6)

        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    # ---------- 内部 ----------

    def _refresh(self) -> None:
        """按搜索词与地区重填列表。"""
        self.tree.delete(*self.tree.get_children())
        region = self.var_region.get()
        for name, url, reg in search(self.var_q.get()):
            if region != "全部" and reg != region:
                continue
            self.tree.insert("", "end", values=(name, url))

    def _on_select(self, _event=None) -> None:
        """清单里选中某一项 -> 把手填框同步成它的地址。"""
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        if vals and len(vals) > 1:
            self.var_manual.set(str(vals[1]))

    def _on_choose(self) -> None:
        picked = ""
        sel = self.tree.selection()
        if sel:
            vals = self.tree.item(sel[0], "values")
            if vals:
                picked = str(vals[1])
        manual = normalize_home(self.var_manual.get())
        # 清单里选了就用清单的；否则用手填的（手填优先，便于覆盖）
        url = manual or picked
        if not url:
            messagebox.showinfo(APP_NAME, "请先选一所学校，或在下方填写 Canvas 地址。",
                                parent=self)
            return
        self.selected_url = url
        self.destroy()

    def _on_cancel(self) -> None:
        self.selected_url = ""
        self.destroy()


def pick_school(parent: tk.Tk, current: str = "") -> str:
    """弹出院校选择窗口，返回选定的站点地址（取消则返回空串）。"""
    dlg = SchoolPicker(parent, current=current)
    parent.wait_window(dlg)
    return dlg.selected_url


def all_schools() -> list[tuple[str, str, str]]:
    """全部院校（供命令行列出）。"""
    return list(SCHOOLS)
