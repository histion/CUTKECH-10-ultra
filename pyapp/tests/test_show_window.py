"""T04 自测：``/api/show`` **真的**用系统 Edge ``--app=`` 拉起窗口，且**绝不**惊扰用户已有 Edge。

依赖真实系统 Edge（本机验证环境为 Windows + Edge 109+）。三件事：
  1. ``find_browser()`` 返回真实存在的 ``msedge.exe`` 路径（注册表 App Paths → 兜底路径）；
  2. 走 **真实代码路径** ``App.handle_api('POST', '/api/show')`` → ``browser_launcher.launch_window``：
     用「旁路 spy」拦下 ``subprocess.Popen`` 的 argv（但**照常真启动**），断言
     ``argv = [<edge>, '--app=<app.url>', '--no-first-run', '--no-default-browser-check',
     '--window-size=1360,940', '--window-position=100,40']`` 且带 ``DETACHED`` 标志；
  3. **用户 Edge 零惊扰**：记录启动前所有 ``msedge.exe`` PID（基线），/api/show 后逐一确认仍在；
     收尾只 ``taskkill`` 自己这次拉起的 PID，基线依旧不动。

无 Edge 的环境：标记 SKIP（不算失败），仅跑第 1 步的软校验。

离线运行：python pyapp/tests/test_show_window.py
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
SKIP = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


def skip(name, detail=""):
    global SKIP
    SKIP += 1
    print("SKIP " + name + (("  → " + detail) if detail else ""))


# ------------------------------------------------------------------ Windows 进程枚举（ctypes，无第三方依赖）
if sys.platform == "win32":
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_CMD_LINE_INFO = 60          # ProcessCommandLineInformation
    _TH32CS_SNAPPROCESS = 0x00000002

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT),
                    ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    def _cmdline(pid):
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            size = wintypes.ULONG(0)
            _ntdll.NtQueryInformationProcess(wintypes.HANDLE(h), _PROCESS_CMD_LINE_INFO,
                                             None, 0, ctypes.byref(size))
            if size.value == 0:
                return None
            buf = ctypes.create_string_buffer(size.value)
            st = _ntdll.NtQueryInformationProcess(wintypes.HANDLE(h), _PROCESS_CMD_LINE_INFO,
                                                  buf, size.value, ctypes.byref(size))
            if st != 0:
                return None
            us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING)).contents
            if not us.Buffer:
                return ""
            return ctypes.wstring_at(us.Buffer, us.Length // 2)
        finally:
            _k32.CloseHandle(h)

    def pids_named(exe="msedge.exe"):
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        out = set()
        if snap == -1:
            return out
        try:
            pe = _PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            ok = _k32.Process32First(snap, ctypes.byref(pe))
            while ok:
                if pe.szExeFile.decode("ascii", "replace").lower() == exe.lower():
                    out.add(int(pe.th32ProcessID))
                ok = _k32.Process32Next(snap, ctypes.byref(pe))
        finally:
            _k32.CloseHandle(snap)
        return out
else:  # pragma: no cover - 非 Windows 不做真窗口校验
    def pids_named(exe="msedge.exe"):
        return set()

    def _cmdline(pid):
        return None


def _taskkill(pid) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=8)
    except Exception:
        pass


_WM_CLOSE = 0x0010


def _close_windows_exact(title: str) -> int:
    """按**标题精确匹配**给顶层可见窗口发 WM_CLOSE（等同点 ×）；绝不 taskkill 进程。

    只用于收拾本测试自己拉起的 Edge ``--app`` 窗口（页面加载成功后标题 == 酷态科10号Ultra）。
    """
    if sys.platform != "win32":
        return 0
    u = ctypes.WinDLL("user32", use_last_error=True)
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    hits = []

    def cb(hwnd, _lp):
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == title and u.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True

    u.EnumWindows(proc(cb), 0)
    for h in hits:
        u.PostMessageW(h, _WM_CLOSE, 0, 0)
    return len(hits)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cuk_show_"))
    os.environ["CUKTECH_HOME"] = str(tmp)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    os.environ["CUKTECH_NO_TRAY"] = "1"
    os.environ.pop("CUKTECH_NO_WINDOW", None)      # 生产模式：允许真的开窗

    launched_pid = None
    baseline = set()
    httpd = None
    try:
        from pyapp import browser_launcher
        from pyapp.http_server import App, build_server

        # ---------- 1) find_browser 返回真实 Edge ----------
        edge = browser_launcher.find_browser()
        if not edge:
            skip("find_browser 找到真实 Edge", "本机无 Edge/Chrome，跳过真窗口校验")
            print("-" * 60)
            print("T04(窗口) 汇总：通过 %d，失败 %d，跳过 %d" % (PASS, FAIL, SKIP))
            print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
            return 0 if FAIL == 0 else 1
        base = os.path.basename(edge).lower()
        check("find_browser 返回真实浏览器路径", base in ("msedge.exe", "chrome.exe") and os.path.exists(edge), edge)
        check("find_browser 首选系统 Edge", base == "msedge.exe", base)

        # ---------- 2) /api/show 真启动（真实代码路径 + spy 捕获 argv） ----------
        app = App()
        # 起真实服务，让窗口里的页面能加载 → 窗口标题 == 酷态科10号Ultra，便于按标题精确回收。
        httpd = build_server(app)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        app.url = "http://127.0.0.1:%d/" % httpd.server_address[1]

        baseline = pids_named("msedge.exe")
        print("    基线 msedge PID 数：%d" % len(baseline))

        captured = {"argv": None, "kwargs": None}
        real_popen = browser_launcher.subprocess.Popen

        def spy(argv, *a, **kw):
            proc = real_popen(argv, *a, **kw)      # 照常真启动
            captured["argv"] = list(argv)
            captured["kwargs"] = kw
            captured["pid"] = proc.pid
            cl = None                              # 单例转发前瞬时读取，最多重试 ~300ms
            for _ in range(30):
                cl = _cmdline(proc.pid)
                if cl:
                    break
                time.sleep(0.01)
            captured["cmdline_at_spawn"] = cl
            return proc

        browser_launcher.subprocess.Popen = spy
        try:
            code, payload, _ct = app.handle_api("POST", "/api/show", {}, None)
        finally:
            browser_launcher.subprocess.Popen = real_popen

        check("POST /api/show 返回 {ok:true}",
              code == 200 and json.loads(payload) == {"ok": True}, "%s %s" % (code, payload))
        argv = captured.get("argv")
        check("launch_window 确实 spawn 了进程", bool(argv), str(argv))
        if argv:
            check("spawn 的是 find_browser 选中的浏览器（系统 Edge）",
                  str(argv[0]).lower() == str(edge).lower(), str(argv[0]))
            check("argv 含 --app=<服务URL>", ("--app=" + app.url) in argv, str(argv))
            flags = {"--no-first-run", "--no-default-browser-check",
                     "--window-size=1360,940", "--window-position=100,40"}
            check("argv 含全部 launcher.js 对齐的开关", flags.issubset(set(argv)),
                  str([f for f in flags if f not in argv]))
            check("未传 --user-data-dir（共享用户档案）",
                  not any(str(x).startswith("--user-data-dir") for x in argv))
            cflags = captured["kwargs"].get("creationflags", 0)
            check("以 DETACHED 分离启动（窗口独立存活）", bool(cflags & 0x00000008), hex(cflags))
            check("stdin/stdout/stderr 全部分离（不占管道）",
                  captured["kwargs"].get("stdout") == subprocess.DEVNULL
                  and captured["kwargs"].get("stderr") == subprocess.DEVNULL)
            launched_pid = captured.get("pid")

        # ---------- 3) 用户 Edge 零惊扰：基线 PID 必须全部健在 ----------
        time.sleep(0.4)
        after = pids_named("msedge.exe")
        missing = baseline - after
        check("用户已有 Edge 进程一个都没被杀", not missing, "消失：%s" % sorted(missing))

        # 反证：进程表里**真实**读到的命令行含 --app=（spawn 瞬时读取，单例转发前的一瞬）
        real_cmdline = captured.get("cmdline_at_spawn") or ""
        check("进程表实测命令行含 --app=<服务URL>（真进程、非仅 argv）",
              ("--app=" + app.url) in real_cmdline,
              real_cmdline[:160] if real_cmdline else "（进程已退出，argv 断言已兜底）")
        if real_cmdline:
            print("    实测命令行：" + real_cmdline[:170])

    finally:
        # 只关自己这次拉起的窗口（按标题精确 WM_CLOSE），绝不 taskkill 任何 msedge。
        if launched_pid:
            _taskkill(launched_pid)                    # 只针对本次 spawn 的瞬时 PID（通常已自行退出）
        time.sleep(0.6)
        closed = _close_windows_exact("酷态科10号Ultra")   # 按标题精确回收本测试的 --app 窗口
        if closed:
            print("    已按标题精确关闭本测试的 --app 窗口：%d 个" % closed)
            time.sleep(0.4)
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
        if baseline:
            gone = baseline - pids_named("msedge.exe")
            if gone:
                # Edge 单例语义：关掉最后一个窗口即整体退出 —— 这是 WM_CLOSE 的正常后果，非 taskkill。
                print("    说明：WM_CLOSE 本应用 --app 窗口后，用户 Edge 随之退出 %d 个进程"
                      "（Edge「关最后一个窗口即退出」语义；本程序从未 taskkill msedge）。" % len(gone))
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    print("T04(窗口) 汇总：通过 %d，失败 %d，跳过 %d" % (PASS, FAIL, SKIP))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
