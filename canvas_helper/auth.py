"""登录与会话管理：持久化浏览器 profile，一次登录长期复用。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from .paths import PROFILE_DIR, canvas_home, clear_auth_needed, get_logger

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright

LOGIN_PROBE = "/api/v1/users/self/profile"
log = get_logger()

StatusCb = Callable[[str], None]

# Chromium 的 headless 模式不会加载 profile 里已落盘的 cookie（已实测：磁盘上有、
# headless 上下文读不到），因此改为显式导出/导入 cookie，两条路径都可靠。
# 站点地址来自配置（canvas_home），SSO 域名各校不同——登录窗只有这一个用途，
# 整上下文 cookie 全存（含登录链路 IdP、Edge 新标签页等，均留本机 gitignored
# 文件），换任何学校都不会漏存 SSO 票据；不想留就 canvas-helper clean --login。
SESSION_PATH = PROFILE_DIR.parent / "session.json"


def _noop(_: str) -> None:
    pass


def _load_cookies() -> list[dict]:
    if not SESSION_PATH.exists():
        return []
    try:
        rows = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取 session.json 失败: %s", exc)
        return []
    out = []
    for raw in rows:
        row = dict(raw)
        if not row.get("name"):
            continue
        exp = row.get("expires")
        if exp is None or exp < 0:
            row.pop("expires", None)  # 会话 cookie
        if "domain" not in row and "url" not in row:
            continue
        out.append(row)
    return out


class AuthRequired(Exception):
    """当前会话未登录，需要用户交互式登录。"""


class CanvasSession:
    """封装 Playwright 持久化上下文与 API 请求。

    使用方式::

        with CanvasSession() as s:
            s.ensure_login()          # 需要时弹出浏览器窗口
            data = s.api_get("/api/v1/courses")
    """

    def __init__(self, channel: str = "msedge", timeout_ms: int = 60_000):
        self.channel = channel
        self.timeout_ms = timeout_ms
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self.profile: dict | None = None

    # ---------- 生命周期 ----------

    def _playwright(self):
        if self._pw is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
        return self._pw

    def _launch(self, headless: bool):
        p = self._playwright()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        kwargs = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": headless,
            "accept_downloads": True,
            "viewport": {"width": 1360, "height": 900},
        }
        try:
            return p.chromium.launch_persistent_context(channel=self.channel, **kwargs)
        except Exception as exc:  # Edge 缺失等
            log.warning("使用 %s 启动失败(%s)，回退到内置 chromium", self.channel, exc)
            return p.chromium.launch_persistent_context(**kwargs)

    def start(self, headless: bool = True) -> None:
        if self._ctx is not None:
            return
        ctx = self._launch(headless)
        self._ctx = ctx
        cookies = _load_cookies()
        if cookies:
            try:
                ctx.add_cookies(cookies)
                log.debug("已注入 %d 个保存的 cookie", len(cookies))
            except Exception as exc:
                log.warning("注入保存的 cookie 失败: %s", exc)

    def save_session(self) -> None:
        """把当前 cookie 显式写到 session.json（覆盖刷新后的值）。"""
        if self._ctx is None:
            return
        try:
            rows = self._ctx.cookies()
            SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            SESSION_PATH.write_text(json.dumps(rows, indent=1), encoding="utf-8")
            log.debug("已保存 %d 个 cookie -> %s", len(rows), SESSION_PATH)
        except Exception as exc:
            log.warning("保存 cookie 失败: %s", exc)

    def close(self) -> None:
        if self._ctx is not None:
            with suppress(Exception):
                self._ctx.close()
            self._ctx = None
        if self._pw is not None:
            with suppress(Exception):
                self._pw.stop()
            self._pw = None

    def __enter__(self) -> CanvasSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- API ----------

    def url(self, path: str) -> str:
        """/api/v1/... -> 绝对地址。"""
        if path.startswith("http"):
            return path
        home = canvas_home()
        if not home:
            raise RuntimeError(
                "未配置 Canvas 站点地址：请在设置页「Canvas 站点地址」填写（config.json 的 canvas_home）")
        return home + "/" + path.lstrip("/")

    def api_get(self, path: str, params: dict | None = None, timeout: int | None = None):
        """返回 (status_code, parsed_json_or_text)。带登录 cookie。"""
        if self._ctx is None:
            self.start(headless=True)
        ctx = self._ctx
        if ctx is None:  # start() 成功后必不为 None；此处仅为给类型检查器收窄
            raise RuntimeError("浏览器会话未能启动")
        resp = ctx.request.get(
            self.url(path),
            params=params,
            timeout=timeout or self.timeout_ms,
            fail_on_status_code=False,
        )
        status = resp.status
        try:
            body = resp.json()
        except Exception:
            body = resp.text()
        return status, body

    def api_ok(self, path: str, params: dict | None = None):
        status, body = self.api_get(path, params)
        if status == 200:
            return body
        raise AuthRequired(f"{path} -> HTTP {status}")

    def is_logged_in(self) -> bool:
        try:
            status, body = self.api_get(LOGIN_PROBE)
        except Exception as exc:
            log.warning("登录探测失败: %s", exc)
            return False
        ok = status == 200 and isinstance(body, dict) and "id" in body
        if ok:
            self.profile = body
            self.save_session()
            clear_auth_needed()  # 顺手撤掉后台同步留下的「待重新登录」标记
        return ok

    # ---------- 登录 ----------

    def ensure_login(self, on_status: StatusCb = _noop, timeout_s: int = 1200) -> bool:
        """确保已登录。必要时打开可见浏览器窗口等待用户完成 SSO/2FA。"""
        if not canvas_home():
            on_status("未配置 Canvas 站点地址")
            raise RuntimeError(
                "未配置 Canvas 站点地址：请在设置页「Canvas 站点地址」填写（config.json 的 canvas_home）")
        on_status("检查登录状态…")
        self.start(headless=True)
        if self.is_logged_in():
            on_status("已复用上次登录会话")
            return True

        on_status("会话已过期，打开浏览器登录窗口…")
        self.close()
        self.start(headless=False)
        if self._ctx is None:
            raise RuntimeError("浏览器会话未能启动")
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        try:
            page.goto(canvas_home() + "/", wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log.warning("打开 Canvas 失败: %s", exc)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if self.is_logged_in():
                    return self._finish_login(on_status)
                alive = bool(self._ctx and self._ctx.pages)
            except Exception as exc:
                log.debug("轮询异常: %s", exc)
                alive = False
            if not alive:
                # 用户手动关闭了登录窗口：cookie 已保存，换无窗口会话再探一次
                self.close()
                self.start(headless=True)
                if self.is_logged_in():
                    on_status("登录成功，已保存会话")
                    return True
                on_status("登录窗口已关闭，未获取到有效会话")
                return False
            time.sleep(1.5)

        on_status("登录超时")
        return False

    def _finish_login(self, on_status: StatusCb) -> bool:
        on_status("登录成功，已保存会话")
        log.info("登录成功: %s", (self.profile or {}).get("name"))
        self.save_session()
        # 换成无窗口会话，避免浏览器窗口一直挂在桌面上
        self.close()
        self.start(headless=True)
        if self.is_logged_in():
            return True
        # 兜底：无窗口会话不可用时退回有窗口会话，保证本次操作照常进行
        log.warning("无窗口会话校验失败，回退到有窗口会话")
        self.close()
        self.start(headless=False)
        if self.is_logged_in():
            return True
        on_status("登录校验失败，请重试")
        return False

    def require_login(self) -> None:
        if not self.is_logged_in():
            raise AuthRequired("未登录")


def probe_profile() -> dict | None:
    """无窗口探测当前 profile 是否有效，返回用户信息或 None。"""
    s = CanvasSession()
    try:
        s.start(headless=True)
        return s.profile if s.is_logged_in() else None
    except Exception as exc:
        log.warning("探测失败: %s", exc)
        return None
    finally:
        s.close()
