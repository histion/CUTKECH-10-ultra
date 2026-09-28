"""扫码登录代理：``qr`` / ``poll`` 子进程代理、设备地址自动发现、成功后重启采集器。

等价重写 ``server.js`` 的 ``loginJob``/``runPy``/``loginStart``/``loginPoll``。

* ``qr``：一次性子进程（``--role login-qr`` → ``login.py qr``），产出 ``data/qr.png``；
* ``poll``：**长驻**子进程（``--wait 280 --verbose``），受理即返回、结果经
  ``/api/state.login.result`` 异步获取；成功后自动发现设备地址、落 token、重启采集器。
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

from . import MAC_RE_TEXT, paths
from .collector_bridge import last_json_of_type, no_window_kwargs, run_role

_MAC_RE = re.compile(MAC_RE_TEXT)

# poll 最长等待秒数（与 login.py 的 --wait 默认一致）。
_POLL_WAIT_S = 280


class LoginProxy:
    """登录 job 状态机（单一在途任务）。"""

    def __init__(self, app) -> None:
        self.app = app
        self.running = False
        self.kind: str | None = None
        self.started_at = 0
        self.result: dict | None = None
        self.proc = None
        self._lock = threading.RLock()

    # -------------------------------------------------------------- 小工具

    def _push_log(self, line: str) -> None:
        try:
            self.app.push_log(line)
        except Exception:
            pass

    @staticmethod
    def last_json(text: str) -> dict | None:
        """取多行输出里最后一条 JSON 对象（等价 ``lastJson``）。"""
        return last_json_of_type(text, None)

    @staticmethod
    def _run_py(role: str, args: list[str], timeout_ms: int) -> dict:
        """一次性子进程（等价 ``runPy``）：收集 stdout/stderr，超时 kill。"""
        return run_role(role, args, timeout_ms)

    # -------------------------------------------------------------- 状态

    def current(self) -> dict:
        """``/api/state.login`` 的回显结构。"""
        with self._lock:
            return {"running": self.running, "kind": self.kind, "result": self.result}

    # -------------------------------------------------------------- 申请二维码

    def start(self) -> dict:
        """``POST /api/login/start``：申请二维码（同步等待，通常数秒）。"""
        with self._lock:
            if self.running:
                return {"ok": False, "error": "已有登录任务在进行"}
            self.running = True
            self.kind = "qr"
            self.started_at = int(time.time() * 1000)
            self.result = None

        self._push_log("开始申请登录二维码…")
        result = self._run_py("login-qr", ["qr"], 60000)
        obj = self.last_json(result.get("stdout") or "")

        with self._lock:
            self.running = False
            self.kind = None

        if not obj or not obj.get("ok") or not obj.get("png"):
            err = (obj or {}).get("error") or result.get("error") or "申请二维码失败"
            self._push_log("申请二维码失败：" + str(err))
            return {"ok": False, "error": err}

        self._push_log("二维码已生成，等待扫码（%ss 内有效）" % obj.get("timeout"))
        return {"ok": True, "timeout": obj.get("timeout"), "ts": int(time.time() * 1000)}

    # -------------------------------------------------------------- 轮询授权

    def poll(self) -> dict:
        """``POST /api/login/poll``：受理即返回，实际等待在后台子进程里进行。"""
        with self._lock:
            if self.running:
                return {"ok": False, "error": "已有登录任务在进行"}
            self.running = True
            self.kind = "poll"
            self.started_at = int(time.time() * 1000)
            self.result = None

        extra = ["poll", "--address", (self.app.effective_address() or "auto"),
                 "--wait", str(_POLL_WAIT_S), "--verbose"]
        try:
            proc = subprocess.Popen(
                paths.child_argv("login-poll", extra),
                cwd=str(paths.meipass_dir()), env=paths.child_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **no_window_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.running = False
            return {"ok": False, "error": str(exc)}

        with self._lock:
            self.proc = proc
        threading.Thread(target=self._read_stderr, args=(proc,), name="login-stderr", daemon=True).start()
        threading.Thread(target=self._watch, args=(proc,), name="login-watch", daemon=True).start()
        self._push_log("已开始等待扫码授权…")
        return {"ok": True}

    def _read_stderr(self, proc) -> None:
        try:
            while True:
                raw = proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._push_log("登录：" + line)
        except Exception:
            pass

    def _watch(self, proc) -> None:
        out = b""
        try:
            # 按行读（同 CollectorBridge：管道上用 read(n) 会阻塞到满 n 字节/EOF）。
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    break
                out += raw
        except Exception:
            out = out if isinstance(out, bytes) else b""
        try:
            proc.wait()
        except Exception:
            pass
        code = proc.returncode

        with self._lock:
            self.running = False
            self.proc = None

        obj = self.last_json(out.decode("utf-8", "replace"))
        if obj and obj.get("ok"):
            with self._lock:
                self.result = {"ok": True, "region": obj.get("region"),
                               "tokenLen": obj.get("tokenLen")}
            mac = obj.get("mac")
            current = self.app.effective_address()
            if isinstance(mac, str) and _MAC_RE.match(mac) and mac.upper() != current:
                # 自动发现：账号里找到哪台充电头就记哪台（便携包给别人用时对方没填过地址）。
                try:
                    self.app.set_address(mac.upper())
                except Exception:
                    pass
                self._push_log("已记住设备地址 " + mac.upper())
            self._push_log("登录成功，已保存 BLE 密钥")
            bridge = getattr(self.app, "bridge", None)
            if bridge is not None:
                bridge.restart_soon()
        else:
            err = (obj or {}).get("error") or ("退出码 %s" % code)
            with self._lock:
                self.result = {"ok": False, "error": err}
            self._push_log("登录失败：" + str(err))

    # -------------------------------------------------------------- 收尾

    def stop(self) -> None:
        """退出纪律：kill 在途 poll 子进程（``__main__.shutdown`` 调用）。"""
        with self._lock:
            proc = self.proc
            self.proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


__all__ = ["LoginProxy"]
