"""把带边框的源图裁成 Windows 图标 ``.ico``（外加一张 README 预览图）。

用法::

    python scripts/make_icon.py                 # 用默认源图 -> docs/icons/canvashelper.ico
    python scripts/make_icon.py <源图> [输出ico]  # 指定来源/去处
    python scripts/make_icon.py --verify         # 只体检已有的 ico，不重新生成

流程
----
1. **裁掉边框** —— 从四条边逐行/逐列向内推进：某一行（列）上均匀取 9 个采样点，
   全部落在「四角主色」的容差内就算边框，直到碰到内容像素为止。单边最多裁掉
   35%，这样即使整图底色与边框同色，也不会被裁成空白。
2. **取中心画面** —— 裁完边框的内接矩形再取中心正方形（宽高取小者、居中），
   因此无论原图是横的还是竖的，图标里留下的都是画面正中那块。
3. **去外圈、留圆角** —— 源图画的是一张带投影的圆角卡片，四角还剩一圈底色。
   先按「与底色的偏差」量出卡片外接矩形并裁紧（把外圈彻底截掉），再用卡片
   自身的圆角半径画一个 4 倍超采样的抗锯齿遮罩当 alpha：圆角保住，圆角之外
   全透明——四角既不是底色也不是灰边，深浅桌面都干净。
4. **多尺寸** —— 归一到 256px 后写出 16/24/32/48/64/128/256 七个尺寸，Windows
   按显示比例挑最接近的一档，避免小图标发虚、大图标被插值糊掉。编码上 256 用
   PNG、其余用 BMP（Pillow 只认「整份 save」的开关，所以分两次存再按帧拼），
   这样 shell 能拿到压缩帧、GDI / .NET 又不会把 PNG 帧画成噪点。

依赖只在开发机：Pillow 在 ``dev`` extra 里（``pip install -e .[dev]``，CI 装的
也是它，否则 mypy 会报 PIL import-not-found），项目**运行时**依旧是零依赖。
"""

from __future__ import annotations

import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = Path.home() / "Downloads" / "CanvasHelper.jpg"
ICO_PATH = ROOT / "docs" / "icons" / "canvashelper.ico"
PREVIEW_PATH = ROOT / "docs" / "icons" / "canvashelper.png"

SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
# 单边最多裁掉的比例；裁到这个上限就停，宁可留一圈边也不把画面裁没。
MAX_TRIM = 0.35
# 与四角主色的最大通道差，超过即视为「内容像素」。
TOL = 26
# 沿一条行/列取几个采样点判断是否整条都是边框。
SAMPLES = 9
# 从这一档起改用 PNG：小于它的走 BMP。Pillow 的 bitmap_format 是「整份 save」
# 的开关、不支持逐帧，所以先各存一份再按帧拼装（见 _mix_ico）。
#   BMP —— GDI / .NET 的 Icon.ToBitmap 只认这个，收到 PNG 帧会画成一片噪点；
#   PNG —— 256 档用 BMP 要 260 多 KB，而 shell 从 Vista 起本来就优先取 PNG。
PNG_FROM = 256
# 「卡片」判定：与底色（四角均值）的偏差超过这个值就算卡片（含投影）。
CARD_TOL = 10
# 圆角遮罩的超采样倍数：先画 4 倍大的再缩回来，边缘才是抗锯齿的。
MASK_SS = 4


def _sample(px: Image.Image, xy: tuple[int, int]) -> tuple[int, int, int]:
    """取一个像素的 RGB 三元组。

    Pillow 的 ``getpixel`` 类型是 ``float | tuple[int, ...] | None``（调色板图给
    索引、单通道图给 int），这里统一收窄成 RGB 元组，省得每个调用点各写一遍判断。
    """
    p = px.getpixel(xy)
    if not isinstance(p, tuple) or len(p) < 3:
        raise TypeError(f"getpixel{xy} 返回 {p!r}，期望 RGB 元组")
    return (p[0], p[1], p[2])


def _corner_color(px: Image.Image) -> tuple[float, float, float]:
    """四角像素的均值——边框通常就是这个颜色。"""
    w, h = px.size
    pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    r = g = b = 0.0
    for xy in pts:
        p = _sample(px, xy)
        r += p[0]
        g += p[1]
        b += p[2]
    n = len(pts)
    return (r / n, g / n, b / n)


def _dev(pixel: tuple[int, int, int], ref: tuple[float, float, float]) -> float:
    """单像素与参考色的最大通道差。"""
    return max(abs(pixel[i] - ref[i]) for i in range(3))


def trim_border(im: Image.Image) -> tuple[int, int, int, int]:
    """返回裁掉边框后的内接矩形 ``(left, top, right, bottom)``；裁不动就原样返回。"""
    px = im.convert("RGB")
    w, h = px.size
    ref = _corner_color(px)
    max_x = max(1, int(w * MAX_TRIM))
    max_y = max(1, int(h * MAX_TRIM))

    def row_is_border(y: int) -> bool:
        # 采样点不取 0 / w-1：角落按定义就是边框色，会把判定带偏。
        return all(
            _dev(_sample(px, (int(w * i / (SAMPLES + 1)), y)), ref) <= TOL
            for i in range(1, SAMPLES + 1)
        )

    def col_is_border(x: int) -> bool:
        return all(
            _dev(_sample(px, (x, int(h * i / (SAMPLES + 1)))), ref) <= TOL
            for i in range(1, SAMPLES + 1)
        )

    left, top = 0, 0
    right, bottom = w - 1, h - 1
    while left < max_x and col_is_border(left):
        left += 1
    while right > w - 1 - max_x and col_is_border(right):
        right -= 1
    while top < max_y and row_is_border(top):
        top += 1
    while bottom > h - 1 - max_y and row_is_border(bottom):
        bottom -= 1

    if right - left < 8 or bottom - top < 8:
        return (0, 0, w, h)
    return (left, top, right + 1, bottom + 1)


def center_square(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """内接矩形里的中心正方形。"""
    left, top, right, bottom = box
    side = min(right - left, bottom - top)
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    x0 = min(max(left, cx - side // 2), right - side)
    y0 = min(max(top, cy - side // 2), bottom - side)
    return (x0, y0, x0 + side, y0 + side)


def _bg_color(px: Image.Image) -> tuple[int, int, int]:
    """四角 5x5 patch 的均值 = 卡片之外那圈底色。"""
    w, h = px.size
    pts = [(0, 0), (w - 5, 0), (0, h - 5), (w - 5, h - 5)]
    acc = [0, 0, 0]
    n = 0
    for x, y in pts:
        for i in range(x, x + 5):
            for j in range(y, y + 5):
                p = _sample(px, (i, j))
                acc[0] += p[0]
                acc[1] += p[1]
                acc[2] += p[2]
                n += 1
    return (acc[0] // n, acc[1] // n, acc[2] // n)


def _content_mask(px: Image.Image) -> Image.Image:
    """卡片（含投影）的二值遮罩：与底色的偏差 > CARD_TOL 的像素为 255。"""
    diff = ImageChops.difference(px, Image.new("RGB", px.size, _bg_color(px)))
    return diff.convert("L").point(lambda v: 255 if v > CARD_TOL else 0)


def _corner_radius(mask: Image.Image, box: tuple[int, int, int, int]) -> int:
    """圆角半径：圆角矩形的顶行要到 ``x = left + r`` 才开始有卡片。

    同时夹在四分之一边长以内，防止误把整幅都判成底色时得到一个离谱的值。
    """
    left, top, right, _bottom = box
    limit = max(0, (right - left) // 4)
    for x in range(left, right):
        if mask.getpixel((x, top)):
            return min(max(0, x - left), limit)
    return 0


def _rounded_alpha(size: tuple[int, int], radius: int) -> Image.Image:
    """圆角矩形抗锯齿遮罩：圆角内 255、圆角外 0。"""
    w, h = size
    big = Image.new("L", (w * MASK_SS, h * MASK_SS), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, w * MASK_SS - 1, h * MASK_SS - 1],
        radius=radius * MASK_SS,
        fill=255,
    )
    return big.resize((w, h), Image.Resampling.LANCZOS)


def trim_to_card(px: Image.Image) -> tuple[Image.Image, int]:
    """裁掉卡片外那圈底色，再量圆角半径。

    返回 ``(带透明圆角的 RGBA 图, 圆角半径)``；半径单独返回只是为了能在报告里
    打出来核对。
    """
    box = _content_mask(px).getbbox()
    if box is None:
        # 整幅都跟底色一样：没有卡片可裁，原样留着（至少不会裁成空白）。
        out = px.convert("RGBA")
        out.putalpha(_rounded_alpha(px.size, 0))
        return (out, 0)
    cropped = px.crop(center_square(box))
    mask = _content_mask(cropped)
    mbox = mask.getbbox()
    radius = _corner_radius(mask, mbox) if mbox is not None else 0
    out = cropped.convert("RGBA")
    out.putalpha(_rounded_alpha(cropped.size, radius))
    return (out, radius)


def _read_ico(blob: bytes) -> list[tuple[tuple[int, int], bytes, bytes]]:
    """拆开一份 ico，返回 ``[(尺寸, 目录项前 8 字节, 图像数据), ...]``。

    ICO 目录项一共 16 字节：前 8 字节是宽 / 高 / 色数 / 保留 / 平面数 / 位深，
    重新拼装时原样搬走即可；末尾 8 字节的「字节数、偏移」会随拼装改变，
    因此不复制、写的时候重算。
    """
    reserved, kind, count = struct.unpack_from("<HHH", blob, 0)
    if reserved != 0 or kind != 1:
        raise ValueError(f"文件头不对: reserved={reserved} type={kind}")
    out = []
    for i in range(count):
        off = 6 + i * 16
        if off + 16 > len(blob):
            raise ValueError(f"目录项 {i} 越界")
        size, ptr = struct.unpack_from("<II", blob, off + 8)
        payload = blob[ptr : ptr + size]
        if len(payload) != size:
            raise ValueError(f"{blob[off] or 256}x{blob[off + 1] or 256} 的数据不完整")
        out.append(((blob[off] or 256, blob[off + 1] or 256), blob[off : off + 8], payload))
    return out


def _write_ico(parts: list[tuple[bytes, bytes]]) -> bytes:
    """把 ``[(目录项前 8 字节, 图像数据), ...]`` 拼成完整 ico 并校正偏移。"""
    out = bytearray(struct.pack("<HHH", 0, 1, len(parts)))
    offset = 6 + 16 * len(parts)
    for head, payload in parts:
        out += head + struct.pack("<II", len(payload), offset)
        offset += len(payload)
    for _head, payload in parts:
        out += payload
    return bytes(out)


def _mix_ico(base: Image.Image) -> bytes:
    """小档 BMP、大档 PNG 的混合编码 ico（取舍见 PNG_FROM 的注释）。"""
    sizes = [(n, n) for n in SIZES]
    buf = BytesIO()
    base.save(buf, format="ICO", sizes=sizes, bitmap_format="bmp")
    bmp = [f for f in _read_ico(buf.getvalue()) if f[0][0] < PNG_FROM]
    buf = BytesIO()
    base.save(buf, format="ICO", sizes=sizes)
    png = [f for f in _read_ico(buf.getvalue()) if f[0][0] >= PNG_FROM]
    frames = sorted(bmp + png, key=lambda f: f[0][0])
    return _write_ico([(head, payload) for _size, head, payload in frames])


def build(src: Path, dst: Path) -> str:
    """生成 .ico 与 README 预览 .png，返回一份给人看的报告。"""
    with Image.open(src) as im:
        im.load()
        raw_w, raw_h = im.size
        box = trim_border(im)
        sq = center_square(box)
        base, radius = trim_to_card(im.convert("RGB").crop(sq))
        card_wh = base.size

    # 先归一到 256：最大档 1:1 不丢细节，其余档由 ico 编码器统一缩小，
    # 顺带保证「源图比 256 小」时 256 那一档也照样写得进去。
    base = base.resize((256, 256), Image.Resampling.LANCZOS)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(_mix_ico(base))
    base.save(PREVIEW_PATH, format="PNG")

    trimmed = {
        "left": box[0],
        "top": box[1],
        "right": raw_w - box[2],
        "bottom": raw_h - box[3],
    }
    lines = [
        f"源图    : {src}  {raw_w}x{raw_h}",
        f"裁边框  : {trimmed}",
        f"中心方块: {sq[2] - sq[0]}x{sq[3] - sq[1]}",
        f"卡片    : {card_wh[0]}x{card_wh[1]}  圆角半径 {radius}  四角透明",
        f"输出    : {dst}  ({dst.stat().st_size} B)",
        f"预览    : {PREVIEW_PATH}  ({PREVIEW_PATH.stat().st_size} B)",
    ]
    lines += [f"  {line}" for line in describe(dst)]
    return "\n".join(lines)


def describe(path: Path) -> list[str]:
    """解析 .ico 目录，列出每一档尺寸与它的编码方式（PNG / BMP）。"""
    data = path.read_bytes()
    if len(data) < 6:
        raise ValueError(f"{path} 太短，不像是 ico")
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1:
        raise ValueError(f"{path} 文件头不对: reserved={reserved} type={kind}")

    out: list[str] = []
    for i in range(count):
        off = 6 + i * 16
        if off + 16 > len(data):
            raise ValueError(f"{path} 目录项 {i} 越界")
        # ICO 目录项按顺序是：宽、高、色数、保留、平面数、位深、字节数、偏移。
        w, h, _colors, _reserved, _planes, _bpp, size, ptr = struct.unpack_from(
            "<BBBBHHII", data, off
        )
        w = w or 256  # 目录里 0 代表 256
        h = h or 256
        payload = data[ptr : ptr + size]
        if len(payload) != size:
            raise ValueError(f"{path} 尺寸 {w}x{h} 的数据不完整")
        fmt = "PNG" if payload[:8] == b"\x89PNG\r\n\x1a\n" else "BMP"
        out.append(f"ico[{i}] {w}x{h}  {fmt}  {size} B")
    return out


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}

    if "--verify" in flags:
        path = Path(args[0]) if args else ICO_PATH
        if not path.exists():
            print(f"不存在: {path}")
            return 1
        print("\n".join(describe(path)))
        return 0

    src = Path(args[0]) if args else DEFAULT_SRC
    dst = Path(args[1]) if len(args) > 1 else ICO_PATH
    if not src.exists():
        print(f"源图不存在: {src}")
        return 1

    print(build(src, dst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
