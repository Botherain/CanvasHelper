"""命令行入口：无参数启动 GUI，`sync` / `report` 用于计划任务后台执行。"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from .paths import ensure_dirs, get_logger

#: 自检要导入的子模块。打包时漏掉任何一个，exe 都会在用户点开对应功能时才炸
#: （PyInstaller 是静态分析，函数内 import 漏了不会报错，只是运行期找不到）。
_SUBMODULES = (
    "auth", "canvas_api", "classify", "cleaner", "coursecode", "deskbox",
    "extract", "extras", "filesync", "gui", "paths", "runner", "scheduler",
    "schoollist", "schoolpicker", "schools", "shortcut", "tidy", "uninstall",
    "updater",
)


def _selftest() -> int:
    """导入全部子模块，成功返回 0、失败返回 1。

    给打包产物用的：windowed exe 里抛异常会弹崩溃对话框把 CI 挂住，所以这里
    把异常吞掉、只用退出码表达结果。``--selftest`` 在源码环境下同样可用。
    """
    import importlib

    try:
        for name in _SUBMODULES:
            importlib.import_module(f".{name}", __package__)
    except Exception:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Windows 控制台默认 GBK，中文输出会 UnicodeEncodeError；pythonw / 打包成
    # windowed exe 时 stdout 是 None，连 print 都会 RuntimeError("lost sys.stdout")。
    # 前者切编码，后者塞一个丢弃用的缓冲区——后面的代码就不用逐处判空了。
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    elif hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    ensure_dirs()
    log = get_logger()

    if argv and argv[0] == "--selftest":
        return _selftest()

    if argv and argv[0] in ("-V", "--version"):
        from . import APP_NAME, __version__

        print(f"{APP_NAME} {__version__}")
        return 0

    if not argv or argv[0] in ("gui", "-g", "--gui"):
        from . import gui

        gui.main()
        return 0

    cmd = argv[0]
    headless = "--headless" in argv or "-q" in argv

    if cmd == "sync":
        from .runner import run_sync

        res = run_sync(headless=headless,
                       progress=log.info)
        log.info("同步统计: %s", res.stats)
        # 后台（计划任务）同步顺手查一次更新：只写缓存 + 日志，不弹窗、不影响
        # 退出码。GUI 下次打开就能直接在顶栏看到「新版本可用」。
        from . import __version__ as _ver
        from .paths import load_config
        from .updater import maybe_check

        try:
            st = maybe_check(load_config())
            if st is not None and st.ok and st.has_update:
                log.info("发现新版本 v%s（当前 v%s），打开 GUI 可看更新公告",
                         st.latest, _ver)
        except Exception as exc:  # 更新检查永远不许影响同步的退出码
            log.info("更新检查跳过：%s", exc)
        if getattr(res, "auth_required", False):
            # 退出码 2 专门表示「登录过期」，与一般的 1（其他错误）区分开
            log.error("登录已过期：本次后台同步未执行。"
                      "打开 GUI 后点「登录 / 刷新课程」重新登录即可恢复。")
            return 2
        if res.errors:
            log.warning("问题: %s", res.errors)
        return 1 if res.errors and not res.stats else 0

    if cmd == "report":
        from .runner import run_report

        paths = run_report(headless=headless,
                           progress=log.info)
        log.info("生成 %d 份摘要", len(paths))
        return 0

    if cmd == "preview":
        from .runner import run_sync

        res = run_sync(dry_run=True, progress=log.info)
        print(res.text)
        if res.errors:
            print("\n警告:")
            for e in res.errors:
                print("  -", e)
        return 0

    if cmd == "login":
        from .auth import CanvasSession

        with CanvasSession() as s:
            ok = s.ensure_login(on_status=log.info)
            log.info("登录结果: %s", ok)
            if ok and s.profile:
                log.info("用户: %s", s.profile.get("name"))
        return 0 if ok else 1

    if cmd == "schools":
        from .schoollist import all_regions, cache_age_days, search_schools, update

        if "--update" in argv:
            # --update：先在线拉一次官方目录，再列出
            rep = update(force=True)
            if rep.get("ok"):
                log.info("院校清单已更新：在线 %d 条 + 内置 %d 条 = 共 %d 条",
                         rep["fetched"], rep["total"] - rep["fetched"], rep["total"])
            else:
                log.warning("院校清单在线更新失败：%s（继续用本地清单）",
                            rep.get("error") or "未知原因")
        kw = ""
        for a in argv[1:]:
            if not a.startswith("-"):
                kw = a
        rows = search_schools(kw)
        for name, url, region in rows:
            print(f"{name}\t{url}\t{region}")
        age = cache_age_days()
        tail = (f"清单上次在线更新是 {int(age)} 天前"
                if age >= 0 else "尚未在线更新过（当前为内置清单）")
        print(f"\n共 {len(rows)} 所 · {tail}"
              f"（用 schools <关键词> 过滤；--update 拉取最新；"
              f"不在清单里请在设置页手填地址）", file=sys.stderr)
        if "--regions" in argv:
            print("地区：" + "、".join(all_regions()), file=sys.stderr)
        return 0

    if cmd == "courses":
        from .auth import CanvasSession
        from .canvas_api import CanvasAPI

        with CanvasSession() as s:
            if not s.ensure_login(on_status=log.info):
                return 1
            for c in CanvasAPI(s).courses():
                print(f"{c.id}\t{c.short}\t{c.name}")
        return 0

    if cmd == "flush":
        from .runner import flush_pending

        n = flush_pending(progress=log.info)
        log.info("写入 %d 条排队待办", n)
        return 0

    if cmd == "clean":
        from . import cleaner

        keys = set()
        if "--login" in argv:
            keys.add("login")
        if "--pending" in argv:
            keys.add("pending")
        if "--todos" in argv:
            keys.add("todos")
        # 默认清理「可再生」的那几项；`--all` 历史上与本默认集合相同，不用另写分支。
        # 登录态 / 排队待办 / 重复待办都要显式加开关。
        keys |= {"state", "ics", "log", "browser", "updates"}
        if "--dry" in argv:
            for t in cleaner.scan():
                mark = "*" if t.default or t.key in keys else " "
                print(f" {mark} {t.key:<8} {t.label:<24} {t.size_text:>10}  {t.files} 个文件")
                print(f"     {t.detail}")
            print("\n* 为默认清理项。执行：python -m inferry clean [--login] [--todos] [--pending]")
            return 0
        progress = log.info
        rep = cleaner.run(keys, progress=progress)
        for m in rep["messages"]:
            log.info(m)
        log.info("清理完成：释放 %s，%d 个文件", cleaner.fmt_size(rep["bytes"]), rep["files"])
        if rep.get("skipped"):
            log.info("%d 个文件被占用，已跳过", rep["skipped"])
        return 0

    if cmd == "tidy":
        from . import tidy
        from .paths import load_config

        plans = tidy.plan_all(Path(load_config()["classes_root"]))
        print(tidy.render(plans))
        if not any(p.todo for p in plans):
            return 0
        if "--apply" in argv:
            rep = tidy.run(plans, progress=log.info)
            print(f"已移动 {rep['moved']} 个文件，跳过 {rep['skipped']} 个，"
                  f"失败 {rep['errors']} 个")
            return 1 if rep["errors"] else 0
        print("\n以上为预览，未改动任何文件。确认后执行：")
        print("  python -m inferry tidy --apply")
        return 0

    if cmd == "install-browser":
        from .auth import install_chromium

        if install_chromium(progress=log.info):
            log.info("Chromium 已就绪：之后没有 Edge 也能正常登录")
            return 0
        log.error("Chromium 下载失败。也可以直接装 Microsoft Edge，程序会优先用它")
        return 1

    if cmd == "update":
        from . import __version__ as _ver
        from . import updater

        brief = "--brief" in argv
        prerelease = "--prerelease" in argv
        st = updater.check(include_prerelease=prerelease)
        if not st.ok:
            log.error("检查更新失败：%s", st.error or "未知原因")
            return 1
        if not st.has_update:
            if brief:
                print(f"v{_ver} 已是最新")
            else:
                when = (st.checked_at or "")[:16].replace("T", " ")
                print(f"当前版本 v{_ver}，已是最新（v{st.latest}，检查于 {when}）")
            return 0
        if brief:
            print(f"发现新版本 v{st.latest}（当前 v{_ver}，落后 {st.behind} 个版本）")
            return 3
        print(f"当前版本 v{_ver} → 最新 v{st.latest}（落后 {st.behind} 个版本）")
        print()
        print(st.notes_text)
        print()
        asset = st.asset
        if asset is not None:
            print(f"下载包：{asset.name}（{asset.size / 1048576.0:.1f} MB）")
        if "--download" in argv:
            if asset is None:
                log.error("这个版本没有 Windows 压缩包，请到下载页手动选择")
                print(f"下载页：{updater.RELEASES_PAGE}")
                return 1
            marks = [-1]

            def report(got: int, total: int) -> None:
                pct = int(got * 100 / total) if total else 0
                if pct // 5 != marks[0]:
                    marks[0] = pct // 5
                    print(f"\r下载中 {pct:3d}%  {got / 1048576.0:.1f} MB",
                          end="", flush=True)

            try:
                path = updater.download(asset, progress=report)
            except Exception as exc:
                print()
                log.error("下载失败：%s", exc)
                return 1
            print()
            print(f"已下载到 {path}")
            print("exe 版可直接在设置页点「立即下载并更新」完成替换；"
                  "源码版解压后用 pip install . 覆盖安装。")
        else:
            print(f"下载页：{updater.RELEASES_PAGE}")
            print("（加 --download 可直接下载 win64 zip；--brief 只打一行）")
        return 3

    if cmd == "uninstall":
        from . import uninstall

        return uninstall.main(argv[1:])

    print("用法: python -m inferry "
          "[gui|sync|report|flush|clean|tidy|login|courses|schools|update|uninstall|install-browser] "
          "[--headless] [--version]")
    print("  gui      启动图形界面（默认）")
    print("  sync     立即同步课程文件与待办")
    print("  report   生成本周学习摘要")
    print("  flush    把后台排队的待办写入 DeskBox")
    print("  clean    一键清理缓存（--dry 预览，--login 连登录态一起清）")
    print("  tidy     整理讲义：先预览移动方案，--apply 才真正执行")
    print("  login    交互式登录并保存会话")
    print("  courses  列出已识别课程")
    print("  schools  列出 Canvas 院校清单（--update 在线更新；可带关键词过滤）")
    print("  update   检查新版本并显示更新公告（--download 直接下载 win64 zip；"
          "--brief 只打一行、有新版本退出码 3）")
    print("  uninstall  卸载：删程序目录 + 使用数据 + 计划任务（--yes 不询问；"
          "--keep-data 保留配置与登录态）")
    print("  install-browser  下载 Playwright 自带的 Chromium（没有 Edge 时才需要）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
