# -*- mode: python ; coding: utf-8 -*-
"""酷态科10号Ultra —— PyInstaller 规格（onefile / windowed）。

要点（design §9.1）：
  * ``onefile`` + ``console=False``（双击无控制台）；
  * 只读复用资源（vendor/web/collector.py/login.py/tray.ps1/app.ico）**平铺到包根**，
    保证 ``collector.py`` 里 ``Path(__file__).parent/'vendor'`` 命中；
  * 运行期第三方依赖整包收集（``bleak``/``winrt``/``aiohttp``/``cryptography``）；
    ``winrt`` 是命名空间包，PyInstaller 易漏 → ``collect_all`` + 显式 hiddenimports（R3）；
  * ``excludes`` 瘦身（tkinter/test/idlelib/lib2to3/pip/ensurepip）。
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
VERSION_FILE = os.path.join(ROOT, "build_py", "_version_info.txt")

datas = []
binaries = []
hiddenimports = []

# 运行期第三方依赖：整包收集（datas / binaries / hiddenimports 三合一）。
for pkg in ("bleak", "winrt", "aiohttp", "cryptography"):
    _d, _b, _h = collect_all(pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# winrt 命名空间包逐子模块收集 + 显式兜底（R3：漏了会 ModuleNotFoundError）。
hiddenimports += collect_submodules("winrt")
hiddenimports += ["cffi", "_cffi_backend", "winrt", "winrt._winrt"]

# 只读复用资源平铺到包根。
datas += [
    (os.path.join(ROOT, "vendor"), "vendor"),
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "collector.py"), "."),
    (os.path.join(ROOT, "login.py"), "."),
    (os.path.join(ROOT, "tray.ps1"), "."),
    (os.path.join(ROOT, "app.ico"), "."),
]

a = Analysis(
    [os.path.join(ROOT, "pyapp", "__main__.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "idlelib", "lib2to3", "pip", "ensurepip"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cuktech 10 ultra",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(ROOT, "app.ico"),
    # 版本资源：在追加内嵌归档**之前**写入 bootloader（rcedit 二次改 PE 会抹掉尾部归档）。
    version=VERSION_FILE if os.path.exists(VERSION_FILE) else None,
)
