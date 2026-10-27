# -*- mode: python ; coding: utf-8 -*-
#
# 单目录 exe 的打包定义。入库而非靠命令行重建：下面几条「必须带上什么」就是
# 分发正确性本身，漏一个就是用户那边的启动崩溃或登录死路。
#
#   pyinstaller --noconfirm --clean Inferry.spec
#
# 产物在 dist/Inferry/，整目录压缩成 zip 分发。

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

import re
import sys
from pathlib import Path

# 仓库根目录（本 spec 所在目录）必须显式加进 pathex。Analysis 默认只把「入口脚本
# 所在目录」也就是 scripts\ 加进搜索路径；而 pip install -e 装出来的 PEP 660 版本
# 注册的是 MetaPathFinder、并不往 sys.path 里加目录，PyInstaller 的分析器看不到
# inferry——症状极隐蔽：构建成功、体积正常，exe 一启动就
# ModuleNotFoundError: No module named 'inferry'。
HERE = Path(SPECPATH).resolve()  # noqa: F821 - SPECPATH 由 PyInstaller 注入
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))  # 让下面的 collect_submodules 能导入它


# 版本资源：让 exe 的「属性 → 详细信息」里有产品名与版本号，而不是一片空白。
# 直接从 __init__.py 读，避免第二份要同步的版本号；文件生成在 build/（已 gitignore）。
def _version_info() -> str:
    src = (HERE / 'inferry' / '__init__.py').read_text(encoding='utf-8')
    match = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    version = match.group(1) if match else '0.0.0'
    numbers = [int(x) for x in re.findall(r'\d+', version.split('-')[0])[:4]]
    numbers += [0] * (4 - len(numbers))
    triple = (numbers[0], numbers[1], numbers[2])
    target = HERE / 'build' / 'version_info.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple(numbers)},
    prodvers={tuple(numbers)},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('CompanyName', 'Inferry'),
      StringStruct('FileDescription', 'Inferry - Canvas 课程同步助手'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'Inferry'),
      StringStruct('OriginalFilename', 'Inferry.exe'),
      StringStruct('ProductName', 'Inferry'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('LegalCopyright', 'MIT License'),
    ])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])]),
  ]
)
""", encoding='utf-8')
    print(f'[spec] version resource: {version} ({triple})')
    return str(target)


VERSION_INFO = _version_info()

pathex = [str(HERE)]

datas = []
binaries = []
hiddenimports = ['inferry']
# 逐个模块显式收集，而不是只写包名：函数内 import（gui -> runner -> cleaner 之类）
# 一旦被静态分析漏掉，就只有用户点到那个功能时才会发现。
hiddenimports += collect_submodules('inferry')

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
    pathex=pathex,
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
    name='Inferry',
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
    icon=['docs/icons/inferry.ico'],
    version=VERSION_INFO,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Inferry',
)
