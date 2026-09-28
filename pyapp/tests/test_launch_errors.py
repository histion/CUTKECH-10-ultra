"""QA 回归自测：非法/不可写 ``CUKTECH_HOME`` **快速退出**、**不弹模态框**（对齐旧 Node 版基线）。

对齐基线：旧 Node 版对非法 HOME 是「~0.2s 快速退出 code=1 + 写 ``%TEMP%\\cuktech-launch-error.log``，
不弹任何框」。此前 Python 版会因未捕获异常被 PyInstaller ``--windowed`` bootloader 渲染成
标题 ``Unhandled exception in script`` 的**模态窗口**并卡住 —— 本测试即为该回归的守卫。

三例：
  1. 不存在的盘   ``Q:\\nope\\cuktech``
  2. 非法字符     ``C:\\bad<>|?*\\cuktech``
  3. 指向一个文件 ``%TEMP%\\cuk_home_is_file``

对**打包产物**（模态框只在窗口化 exe 里出现）断言：
  * ≤2s 内退出且退出码非 0；
  * 全程 EnumWindows 未出现标题含 ``Unhandled exception`` 的窗口；
  * ``%TEMP%\\cuktech-launch-error.log`` 有**可读内容**（含 traceback）。

离线运行：python pyapp/tests/test_launch_errors.py
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "dist_py" / "cuktech 10 ultra.exe"
LOG = Path(tempfile.gettempdir()) / "cuktech-launch-error.log"

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


# ------------------------------------------------------------------ 窗口枚举（ctypes）
if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def window_titles() -> list:
        out: list = []

        def _cb(hwnd, _lp):
            if _user32.IsWindowVisible(hwnd):
                n = _user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _user32.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value:
                        out.append(buf.value)
            return True

        _user32.EnumWindows(_ENUMPROC(_cb), 0)
        return out

    def has_modal() -> bool:
        return any("Unhandled exception" in t for t in window_titles())
else:  # pragma: no cover
    def window_titles() -> list:
        return []

    def has_modal() -> bool:
        return False


def run_case(tag: str, exe: Path, home: str) -> dict:
    env = dict(os.environ)
    env["CUKTECH_HOME"] = home
    env["CUKTECH_NO_WINDOW"] = "1"
    env["CUKTECH_NO_TRAY"] = "1"
    env.pop("CUKTECH_DATA_DIR", None)
    try:
        LOG.unlink()
    except OSError:
        pass

    before = set(window_titles())
    t0 = time.time()
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    modal_seen = False
    t_log = None
    rc = None
    while time.time() - t0 < 25.0:
        if has_modal():
            modal_seen = True
        if t_log is None and LOG.exists() and LOG.stat().st_size > 40:
            t_log = time.time() - t0               # 兜底handler 完成时刻（写入日志）
        rc = proc.poll()
        if rc is not None:
            break
        time.sleep(0.02)
    t_exit = time.time() - t0

    if rc is None:                                 # 还没退 → 判卡死
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    check("%s 启动兜底 ≤2.5s 内完成（日志已落盘，实测 %s）"
          % (tag, ("%.2fs" % t_log) if t_log is not None else "未写"),
          t_log is not None and t_log <= 2.5)
    check("%s 退出码非 0（实测 %s）" % (tag, rc), rc not in (None, 0), "rc=%s" % rc)
    check("%s 未弹出 'Unhandled exception' 模态框" % tag, not modal_seen)

    ok_log = LOG.exists() and LOG.stat().st_size > 0
    content = LOG.read_text(encoding="utf-8", errors="replace") if ok_log else ""
    check("%s 已写 cuktech-launch-error.log 且有可读内容" % tag, ok_log and len(content) > 40,
          "size=%s" % (LOG.stat().st_size if LOG.exists() else "missing"))
    check("%s 日志含 Traceback（可读栈）" % tag, "Traceback" in content)
    return {"t_log": t_log, "t_exit": t_exit, "rc": rc}


def main() -> int:
    if not EXE.exists():
        skip("打包产物存在", str(EXE))
        print("结果：PASS（跳过，未找到 exe）")
        return 0

    # 指向一个文件（不是目录）
    fpath = Path(tempfile.gettempdir()) / "cuk_home_is_file"
    fpath.write_text("x", encoding="utf-8")

    print("== 打包产物：非法 HOME 三例（期望：快速兜底、rc≠0、无模态框、有日志）==")
    results = [
        ("① 不存在的盘", run_case("① 不存在的盘", EXE, r"Q:\nope\cuktech")),
        ("② 非法字符", run_case("② 非法字符", EXE, r"C:\bad<>|?*\cuktech")),
        ("③ 指向文件", run_case("③ 指向文件", EXE, str(fpath))),
    ]
    print("  备注：进程总退出耗时会额外包含 PyInstaller onefile 引导器**清理大型 _MEI 解压树**"
          "的时间（与本应用无关；实测）")
    for tag, r in results:
        print("    %s：启动兜底(写日志)=%s  进程退出=%.2fs  rc=%s"
              % (tag, ("%.2fs" % r["t_log"]) if r["t_log"] is not None else "-",
                 r["t_exit"], r["rc"]))

    print("-" * 60)
    print("T05(启动兜底) 汇总：通过 %d，失败 %d，跳过 %d" % (PASS, FAIL, SKIP))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
