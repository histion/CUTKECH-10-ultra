"""任务栏功率图标管理（等价重写 ``server.js`` 的 ``startTray``/``stopTray``）。

复用现有 ``tray.ps1``（PowerShell + WinForms，零第三方依赖）。⚠️ 关键差异：onefile 的
``_MEIPASS`` 每次启动都变、退出即删，故 ``tray.ps1`` 必须从**稳定根目录**的副本读取
（``paths.ensure_runtime_assets`` 已把副本落好）——否则托盘脚本指向的临时目录会被删掉。
"""

from __future__ import annotations

import os
import subprocess
import threading

from . import paths
from .collector_bridge import no_window_kwargs

# 托盘脚本最多重启次数（与 ``server.js`` 一致）。
_MAX_TRIES = 4


class TrayManager:
    """启停 ``tray.ps1``；异常退出最多重试 4 次。"""

    def __init__(self, app) -> None:
        self.app = app
        self.proc = None
        self.tries = 0
        self.stopping = False
        self._lock = threading.RLock()

    def _enabled(self) -> bool:
        if os.environ.get("CUKTECH_NO_TRAY") == "1":
            return False
        try:
            return self.app.config.data.get("tray") is not False
        except Exception:
            return True

    def _mode(self) -> str:
        try:
            mode = self.app.config.data.get("trayMode")
        except Exception:
            mode = "total"
        return mode if mode in ("all", "panel") else "total"

    def start(self) -> None:
        """启动托盘（已运行 / 超重试上限 / 正在退出 / 无端口时直接返回）。"""
        with self._lock:
            if not self._enabled():
                return
            if self.proc is not None or self.tries >= _MAX_TRIES or self.stopping:
                return
            port = getattr(self.app, "port", None)
            if not port:
                return
            script = paths.root_dir() / "tray.ps1"     # 稳定根副本（见模块说明）
            try:
                proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                     "-WindowStyle", "Hidden", "-File", str(script),
                     "-Port", str(port), "-Mode", self._mode()],
                    cwd=str(paths.meipass_dir()), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    **no_window_kwargs(),
                )
            except Exception as exc:  # noqa: BLE001
                self.app.log("任务栏图标启动失败：" + str(exc))
                return
            self.proc = proc
            self.app.log("tray icon started on port %s" % port)
        threading.Thread(target=self._read_stderr, args=(proc,), name="tray-stderr", daemon=True).start()
        threading.Thread(target=self._watch, args=(proc,), name="tray-watch", daemon=True).start()

    def _read_stderr(self, proc) -> None:
        try:
            while True:
                raw = proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self.app.push_log("任务栏图标：" + line)
        except Exception:
            pass

    def _watch(self, proc) -> None:
        try:
            proc.wait()
        except Exception:
            pass
        with self._lock:
            if self.proc is not proc:
                return
            self.proc = None
            if self.stopping or not self._enabled():
                return
            self.tries += 1
            if self.tries > _MAX_TRIES:
                return
            self.app.push_log("任务栏图标退出了，3 秒后重启（第 %d 次）" % self.tries)
        threading.Timer(3.0, self.start).start()

    def stop(self) -> None:
        """停止托盘（置 ``stopping`` 阻止自愈重启）。"""
        with self._lock:
            self.stopping = True
            proc = self.proc
            self.proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


__all__ = ["TrayManager"]
