"""院校清单的在线更新与本地缓存。

**为什么要在线更新**：Canvas 站点会变（学校换域名、合并、停用），内置清单
必然会过时。Instructure 官方提供公开的院校目录接口：

    https://canvas.instructure.com/api/v1/accounts/search

它返回官方托管租户的 ``(名称, 域名)``，是权威且会持续更新的数据源。

**数据流**::

    内置清单（已核实，126 条）
        +  官方目录（在线拉取，91 条左右）
        ->  合并去重 ->  本地缓存（%LOCALAPPDATA%\\Inferry\\schools_cache.json）
        ->  界面上的院校列表

**降级顺序**（任何一步失败都不影响使用）：

1. 在线源可用 -> 拉取 + 写缓存
2. 在线源不可用但有缓存 -> 用缓存（并提示缓存的更新时间）
3. 都没有 -> 只用内置清单

**关于官方接口的两个实测细节**（写代码前先用程序探过）：

* ``term`` 参数是**首字母匹配**而不是全文搜索——``term=harvard`` 命中 0 条，
  ``term=h`` 命中 30 条 H 开头的租户。
* 不带 ``term`` 时返回的是同一份按名称排序的清单（约 91 条），一次拉完即可，
  无需按字母遍历（实测遍历 37 次请求也只多得到 0 条）。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from typing import Any

from .paths import DATA_DIR, get_logger
from .schools import SCHOOLS

log = get_logger()

#: 官方院校目录接口（Instructure 公开接口，「Find My Canvas URL」用的就是它）
OFFICIAL_URL = "https://canvas.instructure.com/api/v1/accounts/search"

#: 本地缓存文件
CACHE_PATH = DATA_DIR / "schools_cache.json"

#: 缓存有效期（天）。超过就后台再拉一次。
CACHE_TTL_DAYS = 30

#: 拉取超时（秒）。后台任务里跑，超时就放弃用缓存。
FETCH_TIMEOUT = 20.0

#: 官方目录里混着测试 / 开发 / 演示租户与纯编号租户，对用户没有意义，
#: 拉取时直接剔掉（域名含这些标记，或名字是纯数字）。
_NOISE_DOMAIN_PARTS = ("-dev", "dev-", ".dev", "dev2", "qa", "test", "demo",
                        "sandbox", "staging", "mobileqa", "beta")
_NOISE_NAME_WORDS = ("demo", "dev", "sandbox", "staging", "qa", "test")
#: 官方源里名字形如 "09633076 instructure" —— 纯编号租户
_NUMERIC_NAME_RE = re.compile(r"^\d+\s")

#: 官方接口返回的租户按名称排序，名字前常带空格
_NAME_TRIM = " \t\r\n　"


def _clean_name(raw: str) -> str:
    """官方接口的 name 字段前后常有多余空格（含全角空格）。"""
    return (raw or "").strip(_NAME_TRIM)


def _official_domain(raw: str) -> str:
    """把官方接口的 domain 字段整理成站点根地址。"""
    dom = (raw or "").strip().lower()
    if not dom:
        return ""
    if "://" in dom:
        dom = dom.split("://", 1)[1]
    dom = dom.split("/", 1)[0].strip()
    if not dom or "." not in dom:
        return ""
    return f"https://{dom}"


def fetch_official(timeout: float = FETCH_TIMEOUT) -> list[tuple[str, str]]:
    """从 Instructure 官方目录拉取院校列表，返回 ``[(名称, 站点地址), ...]``。

    网络失败、返回格式异常都返回空列表（调用方据此降级到缓存 / 内置清单），
    绝不抛异常打断用户操作。
    """
    if not OFFICIAL_URL.startswith("https://"):
        log.warning("官方院校目录地址不是 https，拒绝拉取: %s", OFFICIAL_URL)
        return []
    ctx = ssl.create_default_context()
    # S310: 上面已校验过是 https 常量，不可能是 file:// 之类；ruff 无法静态推断
    req = urllib.request.Request(  # noqa: S310
        OFFICIAL_URL + "?per_page=100",
        headers={"User-Agent": "Inferry/1.0 (+https://github.com/Botherain/Inferry)",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.warning("拉取官方院校目录失败（用本地缓存/内置清单）: %s", exc)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("官方院校目录返回的不是合法 JSON: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = _clean_name(str(item.get("name") or ""))
        url = _official_domain(str(item.get("domain") or ""))
        if not name or not url or url in seen:
            continue
        if _is_noise(name, url):
            continue
        seen.add(url)
        out.append((name, url))
    log.info("官方院校目录：%d 条原始，过滤后 %d 条可用", len(data), len(out))
    return out


def _is_noise(name: str, url: str) -> bool:
    """判断是不是测试 / 演示 / 无意义租户。"""
    dom = url.split("//")[-1].lower()
    if _NUMERIC_NAME_RE.match(name):
        return True
    low = name.lower()
    if any(w in low for w in _NOISE_NAME_WORDS):
        return True
    host = dom.split(".")[0]
    return any(part in host for part in _NOISE_DOMAIN_PARTS)


def _read_cache() -> tuple[list[tuple[str, str]], float]:
    """读本地缓存，返回 ``([(名称, 地址)], 缓存写入时间戳)``；没有/坏了都给空。"""
    try:
        raw = CACHE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return [], 0.0
    if not isinstance(data, dict):
        return [], 0.0
    entries = data.get("entries")
    if not isinstance(entries, list):
        return [], 0.0
    out: list[tuple[str, str]] = []
    for item in entries:
        if isinstance(item, dict):
            name = _clean_name(str(item.get("name") or ""))
            url = str(item.get("url") or "").strip()
            if name and url.startswith("https://"):
                out.append((name, url))
    try:
        fetched = float(data.get("fetched_at") or 0.0)
    except (TypeError, ValueError):
        fetched = 0.0
    return out, fetched


def _write_cache(entries: list[tuple[str, str]]) -> bool:
    """原子写缓存（先写临时文件再替换），避免中途崩溃写坏文件。"""
    payload = {
        "version": 1,
        "fetched_at": time.time(),
        "entries": [{"name": n, "url": u} for n, u in entries],
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), prefix="schools_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CACHE_PATH)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        log.warning("写院校清单缓存失败: %s", exc)
        return False
    return True


def merge_builtin(online: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """合并内置清单与在线清单。

    - **内置优先**：同一地址两边都有时，保留内置的名称（内置的机构名经站点核实，
      且带中英文，比官方接口的名字好用），在线源只补充它没有的。
    - 去重按**站点地址**（大小写无关）。
    - 地区：内置的按原样；在线的按域名后缀粗分，够界面分组用。
    """
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for name, url, region in SCHOOLS:
        key = url.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append((name, url, region))

    for name, url in online:
        key = url.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append((name, url, _guess_region(url)))

    return out


def _guess_region(url: str) -> str:
    """按域名后缀粗分地区（在线源的条目没有地区信息）。

    ``*.instructure.com`` 是 Instructure 官方托管的租户，绝大多数在美国
    （社区学院 / K-12 学区 / 企业培训机构），少数在加拿大与澳洲。
    """
    dom = url.split("//")[-1].lower()
    if dom.endswith((".edu.hk", ".hk")):
        return "中国香港/澳门"
    if dom.endswith((".edu.cn", ".cn")):
        return "中国内地/台湾"
    if dom.endswith((".edu.tw", ".tw")):
        return "中国内地/台湾"
    if dom.endswith((".edu.au", ".au")):
        return "澳大利亚/新西兰"
    if dom.endswith((".edu.nz", ".nz")):
        return "澳大利亚/新西兰"
    if dom.endswith(".ca"):
        return "加拿大"
    if dom.endswith((".ac.uk", ".uk")):
        return "英国"
    if dom.endswith((".ac.kr", ".kr")):
        return "韩国"
    if dom.endswith((".edu.sg", ".sg")):
        return "新加坡"
    if dom.endswith((".edu.jp", ".jp")):
        return "日本"
    if dom.endswith(".edu"):
        return "美国"
    if dom.endswith(".instructure.com"):
        # 官方托管租户绝大多数在美国
        return "美国"
    if dom.endswith((".edu.mx", ".edu.br", ".edu.ar")):
        return "拉美"
    return "其他"


def cache_age_days() -> float:
    """本地缓存的年龄（天）；没有缓存返回 -1。"""
    _entries, fetched = _read_cache()
    if not fetched:
        return -1.0
    return max(0.0, (time.time() - fetched) / 86400.0)


def needs_refresh(ttl_days: int = CACHE_TTL_DAYS) -> bool:
    """缓存是否过期（没有缓存也算需要刷新）。"""
    age = cache_age_days()
    return age < 0 or age > ttl_days


def update(force: bool = False, timeout: float = FETCH_TIMEOUT) -> dict[str, Any]:
    """拉取在线清单并写入本地缓存。

    返回统计字典::

        {"ok": bool, "fetched": int, "total": int, "cached": bool, "error": str}

    ``ok`` 为 False 表示拉取失败（此时本地缓存与内置清单照常可用）。
    """
    online = fetch_official(timeout=timeout)
    if not online:
        return {"ok": False, "fetched": 0, "total": len(SCHOOLS),
                "cached": bool(_read_cache()[0]), "error": "官方目录不可达"}
    if not _write_cache(online):
        return {"ok": False, "fetched": len(online), "total": len(SCHOOLS),
                "cached": False, "error": "缓存写入失败"}
    merged = merge_builtin(online)
    log.info("院校清单已更新：在线 %d 条 + 内置 %d 条 = 共 %d 条",
             len(online), len(SCHOOLS), len(merged))
    return {"ok": True, "fetched": len(online), "total": len(merged),
            "cached": True, "error": ""}


def current_schools() -> list[tuple[str, str, str]]:
    """当前生效的院校列表 = 内置 + 本地缓存（在线源的内容已经进缓存了）。"""
    cached, _fetched = _read_cache()
    return merge_builtin(cached)


def search_schools(keyword: str = "", region: str = "") -> list[tuple[str, str, str]]:
    """在**当前生效**的清单里搜索（名称或域名包含即命中）。"""
    rows = current_schools()
    kw = (keyword or "").strip().lower()
    if region and region != "全部":
        rows = [r for r in rows if r[2] == region]
    if not kw:
        return rows
    scored: list[tuple[int, str, str, str]] = []
    for name, url, reg in rows:
        low = name.lower()
        dom = url.split("//")[-1].split("/")[0].lower()
        if low.startswith(kw):
            rank = 0
        elif kw in low:
            rank = 1
        elif kw in dom:
            rank = 2
        else:
            continue
        scored.append((rank, name, url, reg))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [(n, u, r) for _rank, n, u, r in scored]


def all_regions() -> list[str]:
    """当前生效清单里出现过的地区（按出现顺序去重）。"""
    out: list[str] = []
    for _name, _url, region in current_schools():
        if region not in out:
            out.append(region)
    return out
