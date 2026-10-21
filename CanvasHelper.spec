# -*- mode: python ; coding: utf-8 -*-
#
# 单目录 exe 的打包定义。入库而非靠命令行重建：下面几条「必须带上什么」就是
# 分发正确性本身，漏一个就是用户那边的启动崩溃或登录死路。
#
#   pyinstaller --noconfirm --clean CanvasHelper.spec
#
# 产物在 dist/CanvasHelper/，整目录压缩成 zip 分发。

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = []
binaries = []
hiddenimports = ['canvas_helper']

# Playwright：不只 import，还要 driver（node.exe + cli.js）。缺了它「安装 Chromium」
# 无从执行，Edge 不在的机器上就彻底没退路。
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# tzdata：Windows 自身没有 IANA 时区库，zoneinfo 全靠这个包的数据文件。
# 漏打进去，paths.py 里任何 ZoneInfo(...) 都会 ZoneInfoNotFoundError——虽然代码
# 已经兜底成「跟随系统时区」，但用户的时区配置会静默失效，等于白装。
datas += collect_data_files('tzdata')
hiddenimports += collect_submodules('tzdata')


a = Analysis(
    ['scripts/pyinstaller_entry.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CanvasHelper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['docs/icons/canvashelper.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CanvasHelper',
)
