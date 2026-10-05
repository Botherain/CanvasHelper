"""命令行入口：无参数启动 GUI，`sync` / `report` 用于计划任务后台执行。"""

from __future__ import annotations

import sys

from .paths import ensure_dirs, get_logger


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ensure_dirs()
    log = get_logger()

    if not argv or argv[0] in ("gui", "-g", "--gui"):
        from . import gui

        gui.main()
        return 0

    cmd = argv[0]
    headless = "--headless" in argv or "-q" in argv

    if cmd == "sync":
        from .runner import run_sync

        res = run_sync(headless=True if headless else False,
                       progress=lambda m: log.info(m))
        log.info("同步统计: %s", res.stats)
        if res.errors:
            log.warning("问题: %s", res.errors)
        return 1 if res.errors and not res.stats else 0

    if cmd == "report":
        from .runner import run_report

        paths = run_report(headless=True if headless else False,
                           progress=lambda m: log.info(m))
        log.info("生成 %d 份摘要", len(paths))
        return 0

    if cmd == "preview":
        from .runner import run_sync

        res = run_sync(dry_run=True, progress=lambda m: log.info(m))
        print(res.text)
        if res.errors:
            print("\n警告:")
            for e in res.errors:
                print("  -", e)
        return 0

    if cmd == "login":
        from .auth import CanvasSession

        with CanvasSession() as s:
            ok = s.ensure_login(on_status=lambda m: log.info(m))
            log.info("登录结果: %s", ok)
            if ok and s.profile:
                log.info("用户: %s", s.profile.get("name"))
        return 0 if ok else 1

    if cmd == "courses":
        from .auth import CanvasSession
        from .canvas_api import CanvasAPI

        with CanvasSession() as s:
            if not s.ensure_login(on_status=lambda m: log.info(m)):
                return 1
            for c in CanvasAPI(s).courses():
                print(f"{c.id}\t{c.short}\t{c.name}")
        return 0

    if cmd == "flush":
        from .runner import flush_pending

        n = flush_pending(progress=lambda m: log.info(m))
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
        if "--all" in argv:
            keys |= {"state", "ics", "log", "browser"}
        else:
            keys |= {"state", "ics", "log", "browser"}
        if "--dry" in argv:
            for t in cleaner.scan():
                mark = "*" if t.default or t.key in keys else " "
                print(f" {mark} {t.key:<8} {t.label:<24} {t.size_text:>10}  {t.files} 个文件")
                print(f"     {t.detail}")
            print("\n* 为默认清理项。执行：python -m canvas_helper clean [--login] [--todos] [--pending]")
            return 0
        progress = lambda m: log.info(m)  # noqa: E731
        rep = cleaner.run(keys, progress=progress)
        for m in rep["messages"]:
            log.info(m)
        log.info("清理完成：释放 %s，%d 个文件", cleaner.fmt_size(rep["bytes"]), rep["files"])
        if rep.get("skipped"):
            log.info("%d 个文件被占用，已跳过", rep["skipped"])
        return 0

    print("用法: python -m canvas_helper [gui|sync|report|flush|clean|login|courses] [--headless]")
    print("  gui      启动图形界面（默认）")
    print("  sync     立即同步课程文件与待办")
    print("  report   生成本周学习摘要")
    print("  flush    把后台排队的待办写入 DeskBox")
    print("  clean    一键清理缓存（--dry 预览，--login 连登录态一起清）")
    print("  login    交互式登录并保存会话")
    print("  courses  列出已识别课程")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
