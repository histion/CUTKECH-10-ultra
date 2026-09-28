"""T04 自测：托盘 / 快捷方式 / 浏览器启动的**离线**逻辑（不依赖真 PowerShell 与真窗口）。

用假 ``subprocess.Popen`` 断言：
  * ``TrayManager`` 从**稳定根**读 ``tray.ps1``、参数 ``-Port/-Mode`` 正确、``NO_TRAY=1`` 时不动手；
  * ``create_shortcut`` 的 ``-EncodedCommand`` 里 ``TargetPath``=exe 自身、``IconLocation`` 指向稳定根 ``app.ico``、
    ``RESULT_OK`` 判定；
  * ``find_browser`` 返回 str/None；``cleanup_legacy_profile`` 只删精确路径。

离线运行：python pyapp/tests/test_integration.py
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


class FakeProc:
    def __init__(self, out=b"", err=b""):
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.returncode = 0

    def communicate(self, timeout=None):
        return self.stdout.read(), self.stderr.read()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class FakeConfig:
    def __init__(self, **data):
        self.data = {"tray": True, "trayMode": "panel", "address": "", "interval": 1.5, "autoA": False}
        self.data.update(data)

    def save(self, cfg=None):
        pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cuk_t04_"))
    os.environ["CUKTECH_HOME"] = str(tmp)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    try:
        from pyapp import paths, tray, shortcut, browser_launcher

        paths.ensure_runtime_assets()   # 落稳定根 app.ico / tray.ps1

        # ---------- TrayManager：从稳定根读 tray.ps1 ----------
        captured = {}

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            return FakeProc()

        tray.subprocess.Popen = fake_popen
        os.environ.pop("CUKTECH_NO_TRAY", None)
        app = SimpleNamespace(config=FakeConfig(tray=True, trayMode="panel"), port=1234,
                              log=lambda m: None, push_log=lambda m: None)
        tm = tray.TrayManager(app)
        tm.start()
        cmd = captured.get("cmd") or []
        check("TrayManager 用稳定根 tray.ps1",
              str(paths.root_dir() / "tray.ps1") in cmd, str(cmd))
        check("TrayManager 传 -Port/-Mode 正确",
              "-Port" in cmd and "1234" in cmd and "-Mode" in cmd and "panel" in cmd, str(cmd))

        os.environ["CUKTECH_NO_TRAY"] = "1"
        captured.clear()
        tray.TrayManager(SimpleNamespace(config=FakeConfig(), port=1234,
                                         log=lambda m: None, push_log=lambda m: None)).start()
        check("NO_TRAY=1 时不启动托盘", "cmd" not in captured)
        os.environ.pop("CUKTECH_NO_TRAY", None)

        # ---------- create_shortcut ----------
        sc = {}

        def fake_popen2(cmd, **kw):
            sc["cmd"] = cmd
            return FakeProc(out=b"RESULT_OK\r\n")

        shortcut.subprocess.Popen = fake_popen2
        os.environ["CUKTECH_EXE"] = str(paths.exe_path())
        res = shortcut.create_shortcut(lambda m: None)
        check("create_shortcut 判定 RESULT_OK → ok=True", res.get("ok") is True, str(res))
        b64 = sc["cmd"][-1]
        ps = base64.b64decode(b64).decode("utf-16-le")
        check("快捷方式 TargetPath 指向 exe 自身", str(paths.exe_path()) in ps, ps[:120])
        check("快捷方式 IconLocation 指向稳定根 app.ico",
              str(paths.root_dir() / "app.ico") in ps, ps[:200])

        # ---------- find_browser 形状 ----------
        b = browser_launcher.find_browser()
        check("find_browser 返回 str 或 None", b is None or isinstance(b, str), repr(b))
        os.environ["CUKTECH_NO_WINDOW"] = "1"
        lw = browser_launcher.launch_window("http://x", lambda m: None)
        check("launch_window 在 NO_WINDOW=1 时跳过", lw == {"browser": None, "pid": 0}, str(lw))
        os.environ.pop("CUKTECH_NO_WINDOW", None)

        # ---------- cleanup_legacy_profile 只删精确路径 ----------
        lap = tmp / "lap"
        os.environ["LOCALAPPDATA"] = str(lap)
        prof = lap / "CuktechMonitor" / "browser"
        prof.mkdir(parents=True)
        other = lap / "CuktechMonitor" / "keep.txt"
        other.write_text("keep", encoding="utf-8")
        removed = browser_launcher.cleanup_legacy_profile(lambda m: None)
        check("cleanup_legacy_profile 删掉 browser 目录", removed is True and not prof.exists())
        check("父目录非空时保留（CuktechMonitor 仍在）", other.exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    print("T04(集成) 汇总：通过 %d，失败 %d" % (PASS, FAIL))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
