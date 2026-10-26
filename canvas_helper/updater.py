"""版本校验、更新公告与一键替换（数据源：GitHub Releases）。

**为什么要这个模块**：exe 分发出去以后，用户不会主动去看 GitHub；功能更新了却
没人知道，等于白做。这里只做三件事，且每一件都必须是「失败也不影响正常使用」：

1. 查最新版本（``/releases`` 列表，一次请求拿到版本号 + 公告正文 + 资产清单）；
2. 与本地 ``__version__`` 比对，把「当前版本之后的所有版本说明」拼成一份公告；
3. exe 版可以下载 win64 zip、解压、生成一个替换脚本，退出后自动换上新版本。

**数据从哪来**：``.github/workflows/release.yml`` 在发版时会把 ``CHANGELOG.md``
里对应版本的小节写成 Release 正文，所以「更新公告」= CHANGELOG，不需要另维护一份。

**降级约定**（三条，缺一个都会变成「更新检查把程序卡死」）：

* 网络失败、返回格式异常一律返回空/``ok=False``，**绝不抛异常**给界面；
* 失败也写 ``attempted_at``——否则离线时每次启动都要白等一次超时；
* 真正的联网节奏由 :func:`needs_check` 控制（默认 24 小时一次），手动检查可绕过。

**一键替换只对打包版有效**：源码运行（``python -m canvas_helper``）时只打开下载页，
不去动用户的工作目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .paths import (
    UPDATE_CACHE_PATH,
    UPDATES_DIR,
    get_logger,
    now_local,
    to_local,
)

log = get_logger()

#: 本仓库（更新检查 / 下载页都从这里拼）
REPO = "Botherain/CanvasHelper"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

#: 拉取超时（秒）。后台线程 / 计划任务里跑，超时就当「这次没查到」。
FETCH_TIMEOUT = 12.0
#: 下载超时（秒）。更新包约 90 MB，给宽一点。
DOWNLOAD_TIMEOUT = 180.0
#: 默认检查间隔（小时）
DEFAULT_INTERVAL_HOURS = 24.0
#: 缓存里最多留多少个版本的公告
MAX_RELEASES_KEPT = 10
#: 公告里最多展示多少个版本（落后很多版本时不必滚一万行）
MAX_NOTES_SHOWN = 10
#: 允许被 /MIR 覆盖的顶层条目：单目录构建只有 exe 与 _internal。
#: 装到自己的文件夹里（旁边还放着别的文件）时**不自动替换**，免得删掉用户资料。
_ALLOWED_TOP = {"canvashelper.exe", "_internal"}

_USER_AGENT = f"CanvasHelper/{__version__} (+https://github.com/{REPO})"

#: 版本号：可选 v 前缀 + 数字段 + 可选后缀（rc1 / beta.2 / +local）
_VER_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(?:[-_.+]?(.+))?$")
#: 预发布标记：rc / alpha / beta / dev / pre，以及 PEP 440 的 a1、b2。
#: 单字母必须跟数字，否则「+build5」这种构建元数据会被误判成预发布。
_PRE_RE = re.compile(r"^(?:(?:rc|alpha|beta|dev|pre|preview)\d*|[ab]\d+)", re.IGNORECASE)
#: 公告正文里要去掉的 markdown 记号
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEAD_RE = re.compile(r"^#{2,6}\s*", re.MULTILINE)
_CODE_RE = re.compile(r"`([^`]*)`")


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #

def parse_version(text: str) -> tuple[int, ...] | None:
    """把 ``v1.4.0`` / ``1.4`` / ``1.4.0-rc1`` 变成可比较的元组；看不懂返回 None。

    统一补到三段数字，最后一位表示「正式版 1 / 预发布 0」，于是
    ``1.4.0-rc1 < 1.4.0`` 成立，而不必写半天的特判。
    """
    match = _VER_RE.match((text or "").strip())
    if not match:
        return None
    nums = [int(x) for x in match.group(1).split(".")]
    while len(nums) < 3:
        nums.append(0)
    suffix = (match.group(2) or "").strip()
    prerelease = bool(suffix) and bool(_PRE_RE.match(suffix))
    return (*nums, 0 if prerelease else 1)


def is_newer(remote: str, local: str) -> bool:
    """``remote`` 是否比 ``local`` 新。任何一边解析失败都算「不是」（不误报更新）。"""
    left, right = parse_version(remote), parse_version(local)
    if left is None or right is None:
        return False
    return left > right


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Asset:
    """Release 里的一个下载资产（只关心 zip）。"""

    name: str
    url: str
    size: int = 0
    sha256: str = ""


@dataclass
class Release:
    """一个 GitHub Release。"""

    tag: str
    version: str
    title: str = ""
    notes: str = ""
    url: str = ""
    published_at: str = ""
    prerelease: bool = False
    assets: list[Asset] = field(default_factory=list)


@dataclass
class UpdateStatus:
    """一次检查的结果（既用于联网检查，也用于读缓存）。"""

    ok: bool = False
    error: str = ""
    current: str = ""
    latest: str = ""
    checked_at: str = ""
    from_cache: bool = False
    newer: list[Release] = field(default_factory=list)
    asset: Asset | None = None

    @property
    def has_update(self) -> bool:
        return bool(self.newer)

    @property
    def behind(self) -> int:
        """落后几个版本。"""
        return len(self.newer)

    @property
    def notes_text(self) -> str:
        return format_notes(self.newer)


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #

def _http_json(url: str, timeout: float = FETCH_TIMEOUT) -> Any:
    """GET 一个 https JSON；任何失败都返回 None（不抛）。"""
    if not url.startswith("https://"):
        log.warning("更新检查地址不是 https，拒绝访问: %s", url)
        return None
    ctx = ssl.create_default_context()
    req = urllib.request.Request(  # noqa: S310 - 上面已校验 https
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.info("更新检查未成功（离线或接口不可用）: %s", exc)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.info("更新检查返回的不是合法 JSON: %s", exc)
        return None


def _asset_from(raw: dict) -> Asset | None:
    url = str(raw.get("browser_download_url") or "")
    name = str(raw.get("name") or "")
    if not name or not url.startswith("https://"):
        return None
    digest = str(raw.get("digest") or "")
    sha = digest.split("sha256:", 1)[1].strip() if digest.startswith("sha256:") else ""
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return Asset(name=name, url=url, size=size, sha256=sha)


def fetch_releases(
    timeout: float = FETCH_TIMEOUT,
    *,
    include_prerelease: bool = False,
) -> list[Release]:
    """拉取 Release 列表（新→旧）。失败返回空列表。"""
    data = _http_json(f"{RELEASES_API}?per_page=30", timeout=timeout)
    if not isinstance(data, list):
        return []
    out: list[Release] = []
    for raw in data:
        if not isinstance(raw, dict) or raw.get("draft"):
            continue
        if raw.get("prerelease") and not include_prerelease:
            continue
        tag = str(raw.get("tag_name") or "").strip()
        version = tag.lstrip("vV")
        if parse_version(version) is None:
            continue
        assets = [a for a in (_asset_from(x) for x in raw.get("assets") or []
                              if isinstance(x, dict)) if a is not None]
        out.append(Release(
            tag=tag,
            version=version,
            title=str(raw.get("name") or tag),
            notes=str(raw.get("body") or ""),
            url=str(raw.get("html_url") or RELEASES_PAGE),
            published_at=str(raw.get("published_at") or ""),
            prerelease=bool(raw.get("prerelease")),
            assets=assets,
        ))
    out.sort(key=lambda r: parse_version(r.version) or (0,), reverse=True)
    return out


def pick_asset(release: Release) -> Asset | None:
    """从资产里挑出要给 Windows 用的 zip：优先 win64，其次任意 zip，绝不选 wheel。"""
    ranked: list[tuple[int, Asset]] = []
    for asset in release.assets:
        name = asset.name.lower()
        if not name.endswith(".zip"):
            continue
        score = 2 if ("win64" in name or "windows" in name) else 1
        ranked.append((score, asset))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: (pair[0], pair[1].size), reverse=True)
    return ranked[0][1]


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #

def _release_to_dict(release: Release) -> dict:
    return {
        "tag": release.tag,
        "version": release.version,
        "title": release.title,
        "notes": release.notes,
        "url": release.url,
        "published_at": release.published_at,
        "prerelease": release.prerelease,
        "assets": [
            {"name": a.name, "url": a.url, "size": a.size, "sha256": a.sha256}
            for a in release.assets
        ],
    }


def _release_from_dict(raw: dict) -> Release | None:
    tag = str(raw.get("tag") or "")
    version = str(raw.get("version") or tag.lstrip("vV"))
    if parse_version(version) is None:
        return None
    assets: list[Asset] = []
    for a in raw.get("assets") or []:
        if not isinstance(a, dict):
            continue
        url = str(a.get("url") or "")
        name = str(a.get("name") or "")
        if not name or not url.startswith("https://"):
            continue
        try:
            size = int(a.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append(Asset(name=name, url=url, size=size, sha256=str(a.get("sha256") or "")))
    return Release(
        tag=tag,
        version=version,
        title=str(raw.get("title") or tag),
        notes=str(raw.get("notes") or ""),
        url=str(raw.get("url") or RELEASES_PAGE),
        published_at=str(raw.get("published_at") or ""),
        prerelease=bool(raw.get("prerelease")),
        assets=assets,
    )


def read_cache() -> dict:
    """读更新缓存；没有 / 坏了都返回空字典（调用方从空开始）。"""
    try:
        data = json.loads(UPDATE_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(payload: dict) -> bool:
    """原子写缓存（mkstemp + replace），失败只记一条警告。"""
    try:
        UPDATE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(UPDATE_CACHE_PATH.parent),
                                   prefix="update_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, UPDATE_CACHE_PATH)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        log.warning("写更新缓存失败: %s", exc)
        return False
    return True


def _now_text() -> str:
    return now_local().isoformat(timespec="seconds")


def _touch_attempt(stamp: str) -> None:
    """只更新「上次尝试时间」，保留旧的公告缓存（离线时仍能看上次查到的内容）。"""
    cache = read_cache()
    if not cache:
        cache = {"version": 1, "checked_at": "", "releases": []}
    cache["attempted_at"] = stamp
    _write_cache(cache)


def needs_check(interval_hours: float = DEFAULT_INTERVAL_HOURS) -> bool:
    """现在该不该真联网查一次。按「上次**尝试**」算，失败也算数。"""
    try:
        interval = float(interval_hours)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_HOURS
    if interval <= 0:
        return True
    cache = read_cache()
    stamp = str(cache.get("attempted_at") or cache.get("checked_at") or "")
    if not stamp:
        return True
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if at.tzinfo is None:
        at = to_local(at)
    try:
        age = (now_local() - at).total_seconds()
    except (TypeError, ValueError):
        return True
    return age >= interval * 3600.0


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #

def check(
    local: str | None = None,
    *,
    timeout: float = FETCH_TIMEOUT,
    include_prerelease: bool = False,
    fetch: Callable[[], list[Release]] | None = None,
) -> UpdateStatus:
    """联网检查一次，返回状态。``fetch`` 可注入（离线测试用）。

    失败时把 ``attempted_at`` 记下来（避免离线时每次启动都等超时），
    并把 ``ok=False`` 交回调用方——界面据此静默降级，不弹错误框。
    """
    current = local or __version__
    getter = fetch or (lambda: fetch_releases(timeout, include_prerelease=include_prerelease))
    try:
        releases = getter()
    except Exception as exc:  # 注入的 fetcher 也不允许把界面搞崩
        log.info("更新检查失败: %s", exc)
        releases = []
    stamp = _now_text()
    if not releases:
        _touch_attempt(stamp)
        return UpdateStatus(ok=False, error="无法获取最新版本（离线或接口不可用）", current=current)
    cache = {
        "version": 1,
        "checked_at": stamp,
        "attempted_at": stamp,
        "releases": [_release_to_dict(r) for r in releases[:MAX_RELEASES_KEPT]],
    }
    _write_cache(cache)
    newer = [r for r in releases if is_newer(r.version, current)]
    latest = releases[0].version
    log.info("更新检查完成：当前 v%s，最新 v%s%s",
             current, latest, f"，落后 {len(newer)} 个版本" if newer else "（已是最新）")
    return UpdateStatus(
        ok=True,
        current=current,
        latest=latest,
        checked_at=stamp,
        newer=newer,
        asset=pick_asset(newer[0]) if newer else None,
    )


def cached_status(local: str | None = None) -> UpdateStatus | None:
    """读缓存算状态（零网络）。没缓存返回 None。"""
    cache = read_cache()
    raw = cache.get("releases")
    if not isinstance(raw, list):
        return None
    releases = [r for r in (_release_from_dict(x) for x in raw if isinstance(x, dict))
                if r is not None]
    if not releases:
        return None
    releases.sort(key=lambda r: parse_version(r.version) or (0,), reverse=True)
    current = local or __version__
    newer = [r for r in releases if is_newer(r.version, current)]
    return UpdateStatus(
        ok=True,
        current=current,
        latest=releases[0].version,
        checked_at=str(cache.get("checked_at") or ""),
        from_cache=True,
        newer=newer,
        asset=pick_asset(newer[0]) if newer else None,
    )


def should_notify(
    status: UpdateStatus | None,
    state: dict | None,
) -> tuple[bool, bool]:
    """要不要提醒：返回 ``(顶栏横幅, 是否自动弹公告)``。

    用户点过「跳过这个版本」就彻底安静（有更新的版本才会再出现）；
    看过公告之后再启动只留横幅，不再每次都弹窗。
    """
    if status is None or not status.ok or not status.has_update:
        return (False, False)
    info = (state or {}).get("update") or {}
    latest = status.latest
    if str(info.get("skipped") or "") == latest:
        return (False, False)
    return (True, str(info.get("seen") or "") != latest)


# --------------------------------------------------------------------------- #
# 公告文本
# --------------------------------------------------------------------------- #

def _plain(text: str) -> str:
    """把 Release 正文（markdown）压成适合只读文本框的纯文本。

    不去追求渲染效果，只去掉 **加粗**、`代码`、链接语法与 ``###`` 这类记号，
    中文标点、列表符号、缩进全部保留。GitHub 的 body 用 CRLF，先统一成 LF，
    否则「连续空行压成一段」的正则会失效、公告里全是空行。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _COMMENT_RE.sub("", text)
    text = _IMAGE_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = _CODE_RE.sub(r"\1", text)
    text = _HEAD_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_notes(releases: list[Release], limit: int = MAX_NOTES_SHOWN) -> str:
    """把「落后期间的每个版本」拼成一份公告（新→旧）。"""
    if not releases:
        return ""
    blocks: list[str] = []
    for release in releases[:limit]:
        head = f"===== v{release.version}"
        if release.published_at:
            head += f" · {release.published_at[:10]}"
        head += " ====="
        body = _plain(release.notes) or "（这个版本没有写更新说明）"
        blocks.append(f"{head}\n{body}")
    if len(releases) > limit:
        blocks.append(f"…… 还有 {len(releases) - limit} 个更早的版本，详见下载页")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# 下载 / 解压 / 替换
# --------------------------------------------------------------------------- #

def download(
    asset: Asset,
    dest_dir: Path | None = None,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """把资产流式下载到 ``dest_dir``，返回文件路径。

    写 ``*.part`` 再原子改名；大小不符或 sha256 不一致都抛 ``ValueError``
    （宁可让用户重下一次，也不要静默装上一个坏包）。
    """
    if not asset.url.startswith("https://"):
        raise ValueError(f"下载地址不是 https：{asset.url}")
    target_dir = Path(dest_dir or UPDATES_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (asset.name or "CanvasHelper-update.zip")
    part = target.with_name(target.name + ".part")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(  # noqa: S310 - 上面已校验 https
        asset.url, headers={"User-Agent": _USER_AGENT}
    )
    digest = hashlib.sha256()
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp, \
                open(part, "wb") as fh:  # noqa: S310
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if progress is not None:
                    progress(got, asset.size)
        if asset.size and got != asset.size:
            raise ValueError(f"下载不完整：{got}/{asset.size} 字节")
        if asset.sha256 and digest.hexdigest().lower() != asset.sha256.lower():
            raise ValueError("下载包校验失败（sha256 不一致），请重试")
        os.replace(part, target)
    finally:
        with suppress(OSError):
            part.unlink(missing_ok=True)
    log.info("更新包已下载：%s（%.1f MB）", target, got / 1048576.0)
    return target


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """解压并拒绝目录穿越（zip-slip），跳过目录项以外的异常条目。"""
    root = dest.resolve()
    for member in archive.infolist():
        name = member.filename.replace("\\", "/").lstrip("/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            continue
        target = root.joinpath(*parts).resolve()
        if target != root and root not in target.parents:
            continue
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def find_exe(root: Path) -> Path | None:
    """在解压结果里找 ``CanvasHelper.exe``（取层级最浅的那个）。"""
    try:
        found = sorted(root.rglob("CanvasHelper.exe"), key=lambda p: len(p.parts))
    except OSError:
        return None
    return found[0] if found else None


def stage(zip_path: Path, version: str = "") -> Path:
    """解压更新包到 ``updates/stage-<version>``，返回新版本 exe 所在目录。"""
    base = UPDATES_DIR / (f"stage-{version}" if version else "stage")
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        _safe_extract(archive, base)
    exe = find_exe(base)
    if exe is None:
        raise ValueError("更新包里没有找到 CanvasHelper.exe")
    return exe.parent


def is_frozen() -> bool:
    """是不是 PyInstaller 打包出来的 exe（源码运行不自我替换）。"""
    return bool(getattr(sys, "frozen", False))


def current_app_dir() -> Path | None:
    """当前 exe 所在目录；源码运行返回 None。"""
    if not is_frozen():
        return None
    return Path(sys.executable).resolve().parent


def safe_to_replace(old_dir: Path, new_dir: Path | None = None) -> bool:
    """能不能对 ``old_dir`` 做 ``/MIR`` 镜像替换。

    替换会删掉旧目录里多出来的文件，所以只在「这就是一个 CanvasHelper 单目录
    构建」时才允许：目录里有 exe，顶层只有 ``CanvasHelper.exe`` 与 ``_internal``，
    且不是盘符根 / 用户主目录。
    """
    old = Path(old_dir)
    try:
        if not (old / "CanvasHelper.exe").is_file():
            return False
        if old.parent == old:  # 盘符根
            return False
        if old.resolve() == Path.home().resolve():
            return False
        top = {p.name.lower() for p in old.iterdir() if not p.name.startswith(".")}
    except OSError:
        return False
    if not top <= _ALLOWED_TOP:
        return False
    return new_dir is None or find_exe(Path(new_dir)) is not None


def _ps_quote(text: str | Path) -> str:
    """PowerShell 单引号字符串：内部的单引号改成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


_APPLY_SCRIPT = """\
# CanvasHelper 自动更新脚本（由程序生成，替换完成后可删除）
# 做的事：等旧进程退出 -> 用新版本镜像覆盖安装目录 -> 重新打开
$ErrorActionPreference = 'Continue'
$waitPid = {pid}
$old = {old}
$new = {new}
$exe = {exe}
$log = {log}

function Write-Log([string]$text) {{
    try {{ "$(Get-Date -Format s) $text" | Out-File -FilePath $log -Append -Encoding utf8 }} catch {{}}
}}

Write-Log "等待进程 $waitPid 退出"
try {{ Wait-Process -Id $waitPid -Timeout 120 -ErrorAction Stop }} catch {{}}
Start-Sleep -Milliseconds 800

if (-not (Test-Path (Join-Path $old 'CanvasHelper.exe'))) {{
    Write-Log "旧目录里没有 CanvasHelper.exe，放弃替换：$old"
    exit 2
}}
if (-not (Test-Path (Join-Path $new 'CanvasHelper.exe'))) {{
    Write-Log "新版本目录里没有 CanvasHelper.exe，放弃替换：$new"
    Start-Process -FilePath $exe
    exit 3
}}

Write-Log "开始替换：$new -> $old"
robocopy $new $old /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
$code = $LASTEXITCODE
Write-Log "robocopy 退出码 $code"

if ($code -ge 8) {{
    Write-Log "替换失败，保留新版本目录供手动处理：$new"
}} else {{
    Write-Log "替换完成，正在重启"
}}
Start-Process -FilePath $exe
exit $code
"""


def apply_script_path() -> Path:
    return UPDATES_DIR / "apply-update.ps1"


def apply_log_path() -> Path:
    return UPDATES_DIR / "apply-update.log"


def write_apply_script(
    pid: int,
    old_dir: Path,
    new_dir: Path,
    exe_path: Path | None = None,
    *,
    script_path: Path | None = None,
) -> Path:
    """生成替换脚本（**只生成不执行**），返回脚本路径。

    用 PowerShell 而不是 .cmd：批处理按 OEM 代码页读取，路径里带中文时容易
    变乱码；PowerShell 脚本可以带 BOM 存 UTF-8，中文路径也稳。
    """
    exe = Path(exe_path) if exe_path else Path(old_dir) / "CanvasHelper.exe"
    target = Path(script_path) if script_path else apply_script_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = _APPLY_SCRIPT.format(
        pid=int(pid),
        old=_ps_quote(Path(old_dir)),
        new=_ps_quote(Path(new_dir)),
        exe=_ps_quote(exe),
        log=_ps_quote(apply_log_path()),
    )
    target.write_text(text, encoding="utf-8-sig")
    log.info("更新脚本已生成：%s", target)
    return target


def launch_apply(script: Path) -> bool:
    """独立进程拉起替换脚本（父进程随后自行退出）。"""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        log.warning("找不到 powershell，无法自动替换")
        return False
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            creationflags=flags, close_fds=True,
        )
    except OSError as exc:
        log.warning("拉起更新脚本失败: %s", exc)
        return False
    log.info("已启动更新脚本，等待本程序退出后替换")
    return True


def cleanup_downloads() -> int:
    """删掉 ``updates/`` 里下载包与解压结果，返回释放的字节数。

    只在「已经是最新版本」时由 GUI 调用——那说明更新要么没开始、要么已经装好，
    留着只是占几十上百 MB。
    """
    freed = 0
    try:
        entries = list(UPDATES_DIR.rglob("*")) if UPDATES_DIR.is_dir() else []
        for path in entries:
            if path.is_file():
                with suppress(OSError):
                    freed += path.stat().st_size
    except OSError:
        pass
    if UPDATES_DIR.is_dir():
        shutil.rmtree(UPDATES_DIR, ignore_errors=True)
    if freed:
        log.info("已清理旧的更新包：%.1f MB", freed / 1048576.0)
    return freed


# --------------------------------------------------------------------------- #
# 给 GUI / 计划任务用的便捷入口
# --------------------------------------------------------------------------- #

def interval_of(cfg: dict | None) -> float:
    """配置里的检查间隔（小时）。``update`` 段缺失时用默认值。"""
    section = (cfg or {}).get("update") or {}
    try:
        return float(section.get("interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS


def maybe_check(cfg: dict | None = None) -> UpdateStatus | None:
    """启动 / 后台同步用的静默检查：遵守开关与节流。

    返回 None 表示「这次没有联网也没缓存」；返回 ``from_cache=True`` 的状态
    表示「节流命中，用的是上次结果」。
    """
    section = (cfg or {}).get("update") or {}
    if not section.get("check_on_start", True):
        return None
    if not needs_check(interval_of(cfg)):
        return cached_status()
    return check(include_prerelease=bool(section.get("include_prerelease", False)))
