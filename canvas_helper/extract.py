"""下载后的压缩包处理：判定「内容包」就解压，判定「数据包」原样保留。

判定顺序（越靠前越权威），核心是**保守**——解错的代价（把数据集铺满目录）比
漏解大，而压缩包永远保留，漏了随时可以手动解：

1. 包名命中 ``data_name_hints``（``data.zip`` / ``dataset_v2``） -> 数据包；
2. 忽略 ``README*`` 说明文件后，包内有讲义/代码类后缀
   （``.ipynb`` ``.py`` ``.pdf`` ``.html`` ``.md``…） -> 内容包，解压；
3. 没有上述任何文件 -> 数据包（``.sh`` 装机脚本不算内容信号、``.txt`` 也不算，
   数据集 zip 里几乎人手一份）。

这套规则是在本机 35 个真实课程压缩包上标定的：17 个讲义/作业包全部判为内容包，
10 个数据集包（含 ``README.md`` + csv 的 ``archive.zip``、``README`` + ``.sh`` +
tsv 的 ``MTNT.tar.gz``）全部判为数据包，8 个 MNIST 的 ``.gz`` 裸数据文件如实标注
「不支持」。**不按「数据文件占比」判**——真实的
``Lecture5.zip`` / ``Lecture6.zip`` 里图片占到 72% / 91%，占比规则会把它们误伤。

判定与执行结果写进 ``state["archives"]``（按路径 + 大小），重复同步不会解第二遍。
解压逐条手工写盘、**绝不 extractall**：拒绝绝对路径与 ``..``（zip-slip）、
跳过 ``__MACOSX`` / ``._*`` / ``.DS_Store`` 这类 macOS 垃圾、并设解压总量上限
防 zip 炸弹；同名同大小视为已解压跳过，同名不同大小走 ``unique_path`` 加 ``(1)``。
"""

from __future__ import annotations

import re
import shutil
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from .classify import sanitize, unique_path
from .paths import get_logger, load_config, now_hk

log = get_logger()

# 标准库能开的压缩格式。.rar/.7z 需要外部工具，零依赖做不到 -> 如实标注不解压
TAR_EXTS = (".tar", ".tar.gz", ".tgz")
ZIP_EXTS = (".zip",)
UNSUPPORTED_EXTS = (".rar", ".7z", ".gz", ".bz2", ".xz", ".cab", ".iso", ".arj")
_COMPOUND_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz")

# 讲义/代码类后缀：出现即判「内容包」。.sh 故意不算——数据集 zip 爱带装机脚本；
# .txt 也不算——数据集几乎人手一份 readme.txt。README* 成员见 _is_readme。
CODE_EXTS = {
    ".ipynb", ".py", ".r", ".m", ".jl", ".md", ".html", ".htm", ".pdf",
    ".ppt", ".pptx", ".doc", ".docx", ".tex", ".c", ".cpp", ".h", ".hpp",
    ".java", ".js", ".ts", ".rs", ".go",
}

DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 单包解压体积上限 2 GiB

# 预览文案：判定结果 -> 计划里那一行的备注
_NOTE = {
    "extracted": "压缩包已解压",
    "data": "数据包，不解压",
    "unsupported": "压缩格式不支持解压",
    "broken": "压缩包损坏，未解压",
    "error": "上次解压失败，下次同步重试",
}


def cfg_archives(cfg: dict | None = None) -> dict:
    """取 ``archives`` 配置段；允许直接把整份配置或该段传进来。"""
    if cfg is None:
        return load_config().get("archives") or {}
    if "classes_root" in cfg or "archives" in cfg:  # 整份配置
        return cfg.get("archives") or {}
    return cfg  # 本身就是 archives 段


def archive_suffix(name: str) -> str:
    """`Lecture3.zip` -> `.zip`；`x.tar.gz` -> `.tar.gz`；非压缩包 -> 空串。"""
    low = (name or "").lower()
    for suf in _COMPOUND_EXTS + TAR_EXTS + ZIP_EXTS + UNSUPPORTED_EXTS:
        if low.endswith(suf):
            return suf
    return ""


def is_archive(path: Path) -> bool:
    return bool(archive_suffix(path.name))


def _family(suf: str) -> str:
    if suf in ZIP_EXTS:
        return "zip"
    if suf in TAR_EXTS or suf in _COMPOUND_EXTS:
        return "tar"
    return "unsupported"


def _stem(name: str, suf: str) -> str:
    return name[: len(name) - len(suf)] if suf else Path(name).stem


def _name_is_data(stem: str, cfg: dict) -> bool:
    hints = [str(h).lower() for h in (cfg.get("data_name_hints") or []) if h]
    if not hints or not stem:
        return False
    norm = re.sub(r"[^0-9a-z]+", " ", stem.lower())  # Lecture5_data -> lecture5 data
    return any(re.search(rf"\b{re.escape(h)}\b", norm) for h in hints)


def _members(path: Path, family: str) -> list[tuple[str, int]]:
    """只读清单，不解压：[(成员名, 解压后大小)]。"""
    if family == "zip":
        with zipfile.ZipFile(path) as zf:
            return [(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()]
    with tarfile.open(path, "r:*") as tf:
        return [(m.name, m.size) for m in tf.getmembers() if m.isfile()]


def _is_readme(name: str) -> bool:
    """README* 两边都不计：讲义包和数据集包里都有一份说明，不能当内容信号。"""
    return Path(name or "").name.lower().startswith("readme")


def classify(path: Path, cfg: dict) -> tuple[str, str]:
    """判定压缩包类型 -> ``(kind, 说明)``。

    kind：``content`` 解压 / ``data`` 保留 / ``unsupported`` 格式做不了 /
    ``broken`` 读不出来 / ``""`` 非压缩包。
    """
    suf = archive_suffix(path.name)
    if not suf:
        return "", ""
    family = _family(suf)
    if family == "unsupported":
        return "unsupported", f"{suf} 需要外部工具，暂不解压"
    if _name_is_data(_stem(path.name, suf), cfg):
        return "data", "包名命中数据关键词"
    try:
        members = _members(path, family)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        return "broken", f"压缩包读取失败：{exc}"
    if not members:
        return "data", "压缩包为空"
    kept = [n for n, _ in members if not _is_readme(n)]
    code = sum(1 for n in kept if Path(n).suffix.lower() in CODE_EXTS)
    if code:
        return "content", f"含 {code} 个代码/讲义文件"
    if not kept:
        return "data", "压缩包里只有说明文件"
    return "data", "没有代码/讲义文件（疑似数据包）"


def dest_for(path: Path) -> Path:
    """解压目标：包名 == 所在目录名（``Lecture3\\Lecture3.zip``）就解到当前
    目录（与手工解压的位置一致），否则解到 ``<包名>\\``（``data.zip`` -> ``data\\``）。"""
    parent = path.parent
    stem = _stem(path.name, archive_suffix(path.name))
    if parent.name.lower() == stem.lower():
        return parent
    return parent / sanitize(stem)


def _is_junk(member: str, cfg: dict) -> bool:
    if not cfg.get("skip_junk", True):
        return False
    parts = [p for p in member.replace("\\", "/").split("/") if p]
    base = parts[-1] if parts else ""
    return (any(p in ("__MACOSX", ".ipynb_checkpoints") for p in parts)
            or base.startswith("._") or base == ".DS_Store")


def _safe_join(base: Path, member: str) -> Path | None:
    """zip-slip 防护：绝对路径 / ``..`` / 盘符一律拒绝，越界返回 None。"""
    name = (member or "").replace("\\", "/")
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    out = base.joinpath(*parts)
    try:  # 双保险：解析后必须仍落在目标目录内
        out.resolve().relative_to(base)
    except (ValueError, OSError):
        return None
    return out


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _extract_zip(path: Path, dest: Path, cfg: dict) -> tuple[int, int]:
    max_bytes = int(cfg.get("max_total_bytes") or DEFAULT_MAX_BYTES)
    written = skipped = 0
    total = 0
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or _is_junk(info.filename, cfg):
                skipped += 1
                continue
            out = _safe_join(dest, info.filename)
            if out is None:
                skipped += 1
                log.warning("拒绝越界路径 %s（zip-slip）", info.filename)
                continue
            total += max(info.file_size, 0)
            if total > max_bytes:
                raise ValueError(f"解压体积超过上限 {_fmt_size(max_bytes)}")
            if out.exists():
                if out.stat().st_size == info.file_size:
                    skipped += 1  # 同名同大小：上次解过了
                    continue
                out = unique_path(out)  # 同名不同大小 -> x (1).ext
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh, length=1 << 20)
            written += 1
    return written, skipped


def _extract_tar(path: Path, dest: Path, cfg: dict) -> tuple[int, int]:
    max_bytes = int(cfg.get("max_total_bytes") or DEFAULT_MAX_BYTES)
    written = skipped = 0
    total = 0
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:*") as tf:
        for m in tf.getmembers():
            # 链接/设备/目录一律不解：符号链接能把写入引到目录之外
            if not m.isfile() or _is_junk(m.name, cfg):
                skipped += 1
                continue
            out = _safe_join(dest, m.name)
            if out is None:
                skipped += 1
                log.warning("拒绝越界路径 %s（tar-slip）", m.name)
                continue
            total += max(m.size, 0)
            if total > max_bytes:
                raise ValueError(f"解压体积超过上限 {_fmt_size(max_bytes)}")
            if out.exists():
                if out.stat().st_size == m.size:
                    skipped += 1
                    continue
                out = unique_path(out)
            src = tf.extractfile(m)
            if src is None:
                skipped += 1
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh, length=1 << 20)
            written += 1
    return written, skipped


class ProcResult:
    """一次处理的结果；``changed`` 表示本轮产生了新的判定/解压记录（需要落盘）。"""

    __slots__ = ("changed", "count", "kind", "reason")

    def __init__(self, kind: str, changed: bool, count: int = 0, reason: str = "") -> None:
        self.kind = kind
        self.changed = changed
        self.count = count
        self.reason = reason

    def __repr__(self) -> str:
        return f"ProcResult({self.kind!r}, changed={self.changed}, count={self.count})"


def process(path: Path, state: dict, cfg: dict | None = None,
            progress: Callable[[str], None] | None = None) -> ProcResult | None:
    """处理一个压缩包；非压缩包 / 功能关闭 / 文件不存在时返回 None。

    ``cfg`` 传 ``archives`` 配置段（或整份配置），不传则现场读配置。
    """
    if not is_archive(path):
        return None
    cfg = cfg_archives(cfg)
    if not cfg.get("extract", True) or not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None

    store = state.setdefault("archives", {})
    key = str(path)
    rec = store.get(key)
    if rec and rec.get("size") == size:
        # 幂等：判定/执行过就不再动（数据包也不会每次同步重新开包）
        return ProcResult(str(rec.get("kind") or "data"), False,
                          int(rec.get("count") or 0), str(rec.get("reason") or ""))

    kind, reason = classify(path, cfg)
    at = now_hk().isoformat(timespec="seconds")
    if kind != "content":
        store[key] = {"kind": kind, "reason": reason, "size": size, "at": at}
        if progress and kind in ("data", "unsupported", "broken"):
            progress(f"{path.name}：{reason}")
        return ProcResult(kind, True, 0, reason)

    dest = dest_for(path)
    extract = _extract_zip if _family(archive_suffix(path.name)) == "zip" else _extract_tar
    try:
        written, skipped = extract(path, dest, cfg)
    except (RuntimeError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        # 加密 / 超限 / 损坏：判为终态，免得每次同步都重试同一个坏包
        store[key] = {"kind": "error", "reason": str(exc), "size": size, "at": at}
        log.error("解压失败 %s: %s", path.name, exc)
        if progress:
            progress(f"解压失败 {path.name}：{exc}")
        return ProcResult("error", True, 0, str(exc))
    except OSError as exc:
        # 文件被占用这类可恢复错误不留记录 -> 下次同步自动重试
        log.error("解压失败 %s: %s", path.name, exc)
        if progress:
            progress(f"解压失败 {path.name}：{exc}")
        return ProcResult("error", False, 0, str(exc))

    store[key] = {"kind": "extracted", "dest": str(dest), "count": written,
                  "skipped": skipped, "size": size, "at": at}
    log.info("解压 %s -> %s（%d 个文件，跳过 %d）", path, dest, written, skipped)
    if progress:
        progress(f"解压 {path.name} → {dest.name}（+{written} 个文件）")
    return ProcResult("extracted", True, written, reason)


def plan_note(path: Path, state: dict, cfg: dict | None = None) -> str:
    """预览阶段给计划里的一项补上压缩包说明；非压缩包返回空串。"""
    if not is_archive(path):
        return ""
    cfg = cfg_archives(cfg)
    if not cfg.get("extract", True):
        return ""
    rec = (state.get("archives") or {}).get(str(path))
    if rec:
        return _NOTE.get(str(rec.get("kind")), "")
    if path.is_file():
        kind, _reason = classify(path, cfg)
        return {"content": "内容包，本次解压",
                "data": "数据包，不解压",
                "unsupported": "压缩格式不支持解压",
                "broken": "压缩包损坏，未解压"}.get(kind, "")
    return "下载后自动判定解压"
