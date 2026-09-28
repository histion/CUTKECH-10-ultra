"""应用窗口启动器 + 旧隔离档案清理（等价重写 ``launcher.js``）。

用 Edge / Chrome 的 ``--app`` 模式打开界面：无地址栏、无标签页、独立任务栏图标。
**沿用用户系统默认档案**：不传 ``--user-data-dir``，也不传任何会改变用户 Edge 自身行为
的开关（登录 / 同步 / 侧边栏 / 扩展一律保持原样）。

查找顺序：注册表 App Paths（msedge / chrome）→ 常见安装路径（含预览通道、用户级）→
系统默认浏览器。``windowsHide`` 必须保持 **False/不隐藏**，否则 Chromium 主窗口会被隐藏
（进程在跑但窗口看不见）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import paths
from .collector_bridge import no_window_kwargs

# 注册表 App Paths 键（与 ``launcher.js`` 完全一致）。
REG_KEYS = [
    "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\msedge.exe",
    "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\App Paths\\msedge.exe",
    "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\msedge.exe",
    "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe",
    "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe",
    "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe",
]

# 分离式启动：不接管子进程生命周期（窗口独立于本进程存活）。
_DETACHED = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0


def _reg_default(key: str):
    """``reg query <key> /ve`` 读默认值（REG_SZ/REG_EXPAND_SZ）；失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["reg", "query", key, "/ve"], capture_output=True,
                             timeout=3, **no_window_kwargs())
        text = out.stdout.decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        m = line.strip()
        if "REG_SZ" in m or "REG_EXPAND_SZ" in m:
            val = m.split("REG_SZ")[-1] if "REG_SZ" in m else m.split("REG_EXPAND_SZ")[-1]
            val = val.strip().strip('"')
            return val or None
    return None


def _known_paths() -> list:
    """兜底的常见安装位置（系统级 + 用户级，含 Edge 预览通道）。"""
    pf = os.environ.get("ProgramFiles") or "C:\\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or "C:\\Program Files (x86)"
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    out = []

    def push(*parts):
        out.append(str(Path(*parts)))

    for base in (pf86, pf):
        push(base, "Microsoft", "Edge", "Application", "msedge.exe")
        push(base, "Microsoft", "Edge Beta", "Application", "msedge.exe")
        push(base, "Microsoft", "Edge Dev", "Application", "msedge.exe")
        push(base, "Google", "Chrome", "Application", "chrome.exe")
    push(local, "Microsoft", "Edge", "Application", "msedge.exe")
    push(local, "Microsoft", "Edge Beta", "Application", "msedge.exe")
    push(local, "Microsoft", "Edge Dev", "Application", "msedge.exe")
    push(local, "Google", "Chrome", "Application", "chrome.exe")
    push(local, "Google", "Chrome SxS", "Application", "chrome.exe")
    return out


def find_browser():
    """返回可用的 Edge/Chrome 路径；都找不到返回 None。"""
    seen = set()

    def check(p):
        if not p:
            return None
        low = p.lower()
        if low in seen:
            return None
        seen.add(low)
        try:
            return p if os.path.exists(p) else None
        except OSError:
            return None

    for key in REG_KEYS:                     # 1) 注册表 App Paths
        hit = check(_reg_default(key))
        if hit:
            return hit
    for p in _known_paths():                 # 2) 常见安装路径
        hit = check(p)
        if hit:
            return hit
    return None   # 3) 交给系统默认浏览器


def legacy_profile_dir() -> Path:
    """旧的隔离档案目录：``%LOCALAPPDATA%\\CuktechMonitor\\browser``。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CuktechMonitor" / "browser"


def cleanup_legacy_profile(on_log=None) -> bool:
    """清理旧隔离档案目录（只删这一个精确路径；父目录恰空才 rmdir）。

    实现落在 ``paths.cleanup_legacy_profile``（含"备份"白名单保护），此处仅做等价转发，
    保证 ``launcher.js`` 的对外语义（返回是否删除成功、失败只记日志）。
    """
    return paths.cleanup_legacy_profile(on_log)


def launch_window(url: str, on_log=None) -> dict:
    """用 ``--app=`` 打开应用窗口；找不到 Chromium 系则回退系统默认浏览器。

    ``CUKTECH_NO_WINDOW=1`` 时跳过（自动化测试）。返回 ``{browser, pid}``。
    """
    say = on_log if callable(on_log) else (lambda _m: None)
    if os.environ.get("CUKTECH_NO_WINDOW") == "1":
        say("已跳过窗口启动（测试模式）")
        return {"browser": None, "pid": 0}

    browser = find_browser()
    if not browser:
        say("未找到 Edge/Chrome，改用系统默认浏览器打开")
        # windowsHide 必须为假：设真会给子进程传 SW_HIDE，Chromium 主窗口会被隐藏。
        proc = subprocess.Popen(["cmd", "/c", "start", "", url],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=_DETACHED)
    else:
        args = [
            "--app=" + url,
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1360,940",
            "--window-position=100,40",
        ]
        say("正在打开应用窗口：" + os.path.basename(browser))
        proc = subprocess.Popen([browser, *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=_DETACHED)
    return {"browser": browser, "pid": proc.pid}


__all__ = ["find_browser", "launch_window", "cleanup_legacy_profile",
           "legacy_profile_dir", "REG_KEYS"]
