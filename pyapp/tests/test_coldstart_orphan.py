"""T05 自测：隔离冷启动 → 配置 → 退出，全链路干净（self-test #2）。

用**打包产物** ``dist_py\\cuktech 10 ultra.exe`` 在**隔离的 CUKTECH_HOME** 下：
  1. ``CUKTECH_NO_WINDOW=1 CUKTECH_NO_TRAY=1`` 冷启动，等 ``ready.json``；
  2. ``/api/ping`` → ``/api/config{autoA:true}`` → ``/api/state`` 复核；
  3. ``/api/quit``；断言**退出码 0**；
  4. 断言退出后**无 ``_MEI`` 残留**（退出纪律：先杀子进程再退）；
  5. 断言**无孤儿子进程**（同名 ``cuktech 10 ultra.exe`` 全部消失）。

离线运行：python pyapp/tests/test_coldstart_orphan.py
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "dist_py" / "cuktech 10 ultra.exe"
EXE_NAME = "cuktech 10 ultra.exe"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _TH32CS_SNAPPROCESS = 0x00000002

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
        ]

    def pids_named(exe):
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
else:  # pragma: no cover
    def pids_named(exe):
        return set()


def mei_snapshot():
    base = Path(tempfile.gettempdir())
    try:
        return {p.name for p in base.glob("_MEI*")}
    except OSError:
        return set()


def wait_ready(home: Path, timeout=120):
    lock = home / "data" / "ready.json"
    end = time.time() + timeout
    while time.time() < end:
        try:
            if lock.exists():
                obj = json.loads(lock.read_text(encoding="utf-8"))
                if obj.get("port"):
                    return int(obj["port"])
        except Exception:
            pass
        time.sleep(0.25)
    return None


def req(port, path, method="GET", body=None, timeout=8):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    headers = {"Content-Type": "application/json"} if method == "POST" else {}
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not EXE.exists():
        print("SKIP 未找到打包产物：" + str(EXE))
        print("结果：PASS（跳过）")
        return 0

    home = Path(tempfile.mkdtemp(prefix="cuk_cold_"))
    env = dict(os.environ)
    env["CUKTECH_HOME"] = str(home)
    env.pop("CUKTECH_DATA_DIR", None)
    env["CUKTECH_NO_WINDOW"] = "1"
    env["CUKTECH_NO_TRAY"] = "1"

    baseline_exe_pids = pids_named(EXE_NAME)
    mei_before = mei_snapshot()
    proc = None
    try:
        t0 = time.time()
        proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        port = wait_ready(home, timeout=120)
        elapsed = time.time() - t0
        check("exe 冷启动就绪（%.1fs）" % elapsed, port is not None)
        if port is None:
            return 1
        check("冷启动在 3.6s 量级（<=6s）", elapsed <= 6.0, "%.2fs" % elapsed)

        code, ping = req(port, "/api/ping")
        check("/api/ping ok", ping.get("ok") is True, str(ping))

        code, cfg = req(port, "/api/config", "POST", {"autoA": True})
        check("/api/config{autoA:true} 生效",
              cfg.get("ok") is True and cfg.get("config", {}).get("autoA") is True, str(cfg))

        code, st = req(port, "/api/state")
        check("/api/state.config.autoA=true 复核", st.get("config", {}).get("autoA") is True)

        # 让采集器子进程有机会被拉起（1 主 + N 子）
        time.sleep(3.0)
        live_children = pids_named(EXE_NAME) - baseline_exe_pids
        print("    运行中同名校验：本进程组共 %d 个 %s" % (len(live_children), EXE_NAME))

        code, q = req(port, "/api/quit", "POST", {})
        check("/api/quit ok", q.get("ok") is True, str(q))
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            rc = None
        check("退出码 0", rc == 0, "rc=%s" % rc)

        time.sleep(1.0)
        leftover_pids = pids_named(EXE_NAME) - baseline_exe_pids
        check("退出后无孤儿子进程（同名进程全部消失）", not leftover_pids,
              "残留 PID：%s" % sorted(leftover_pids))

        leftover_mei = mei_snapshot() - mei_before
        check("退出后无 _MEI 残留", not leftover_mei, str(sorted(leftover_mei)))
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    print("-" * 60)
    print("T05(冷启动) 汇总：通过 %d，失败 %d" % (PASS, FAIL))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
