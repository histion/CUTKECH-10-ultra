"""BLE 采集器子进程管理 + 命令收发（ack）（等价重写 ``server.js`` 的
``findPython``/``startCollector``/``stopCollector``/``restartCollectorSoon``/
``handleCollectorLine``/``sendCommand``/``runSweep``）。

进程模型与现状 1:1：主进程 = HTTP 服务；子进程 = 采集器（exe 自我重入 ``--role collector``，
复用 ``collector.py`` 原文件、零改动）。层间通过 stdout 的 JSONL 协议通信。

线程模型：
  * ``_read_stdout`` 线程：逐行解析 JSONL → ``on_line``；
  * ``_read_stderr`` 线程：诊断输出进 UI 日志；
  * ``_watch`` 线程：等子进程退出 → 处理 close 逻辑（断线重连）；
  * ``send_command`` 由调用方线程阻塞等待 ack（ack 由 stdout 线程投递）—— 因此调用方
    **绝不能是 stdout 线程**（否则自锁）。

退出纪律（design R2）：onefile 引导器在**主进程退出时**删 ``_MEIPASS``，若子进程仍持有
该目录会删不掉并残留 ``_MEI``；故 ``__main__.shutdown`` 必须**先 kill 全部子进程**再退出。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from . import paths

# ``subprocess`` 隐藏控制台窗口（Windows）。非 Windows 返回空 dict。
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def no_window_kwargs() -> dict:
    """``subprocess`` / ``Popen`` 的"隐藏控制台窗口"关键字参数（非 Windows 为空）。

    供采集器 / 扫描 / 登录等所有子进程复用，避免各处重复魔数。
    """
    return {"creationflags": _CREATE_NO_WINDOW} if _CREATE_NO_WINDOW else {}


def run_role(role: str, extra: list[str], timeout_ms: int, on_spawn=None) -> dict:
    """以指定角色跑一个一次性子进程并收集输出（扫描 / 登录 qr 用）。

    ``on_spawn(proc)`` 可选回调：进程创建后立即登记，供退出时 kill（退出纪律 R2）。
    返回 ``{ok, code, stdout, stderr}`` 或 ``{ok:false, error, stdout, stderr}``。
    """
    cmd = paths.child_argv(role, extra)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(paths.meipass_dir()), env=paths.child_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **no_window_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if callable(on_spawn):
        try:
            on_spawn(proc)
        except Exception:
            pass
    try:
        out, err = proc.communicate(timeout=timeout_ms / 1000.0)
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace")}
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
        return {"ok": False, "error": "超时",
                "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace")}


def last_json_of_type(text: str, kind: str | None = None) -> dict | None:
    """从多行 stdout 里取**最后一条**可解析且 ``t == kind`` 的 JSON 对象。

    ``kind`` 为 ``None`` 时取最后一条 JSON（``server.js`` 的 ``lastJson`` 语义）。
    """
    lines = [s.strip() for s in str(text or "").replace("\r\n", "\n").split("\n")]
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if kind is None or (isinstance(obj, dict) and obj.get("t") == kind):
            return obj
    return None


class CollectorBridge:
    """采集器子进程生命周期与命令通道（持有 ``app`` 门面）。"""

    def __init__(self, app) -> None:
        self.app = app
        self.state = app.state
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict = {}
        self._cmd_seq = 0
        self._go_fired = False
        self._sweep_proc = None

    # -------------------------------------------------------------- 小工具

    def _push_log(self, line: str) -> None:
        try:
            self.app.push_log(line)
        except Exception:
            pass

    def _log(self, line: str) -> None:
        fn = getattr(self.app, "log", None)
        if callable(fn):
            try:
                fn(line)
                return
            except Exception:
                pass
        paths._log(line)  # type: ignore[attr-defined]

    def effective_address(self) -> str:
        return self.app.effective_address()

    def token_path(self):
        return paths.data_file("cuktech.token")

    def _interval(self) -> float:
        cfg = getattr(self.app, "config", None)
        try:
            return float(cfg.data.get("interval", 1.5))
        except Exception:
            return 1.5

    def _spawn_child(self, extra: list[str]):
        cmd = paths.child_argv("collector", extra)
        self._log("spawn collector " + " ".join(cmd))
        return subprocess.Popen(
            cmd, cwd=str(paths.meipass_dir()), env=paths.child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **no_window_kwargs(),
        )

    # -------------------------------------------------------------- 启停

    def start(self) -> dict:
        """启动采集器。已在跑 → ``{ok:true,already:true}``；缺 token/地址 → 置 link 并拒绝。"""
        with self._lock:
            if self.state.collector is not None:
                return {"ok": True, "already": True}

        if not self.token_path().exists():
            self.state.link = {"state": "need-login", "attempt": 0,
                               "msg": "尚未登录小米云，请先扫码授权"}
            return {"ok": False, "error": self.state.link["msg"]}
        address = self.effective_address()
        if not address:
            self.state.link = {"state": "need-login", "attempt": 0,
                               "msg": "还没有设备地址，请登录小米云自动识别充电头"}
            return {"ok": False, "error": self.state.link["msg"]}

        extra = ["--address", address, "--token-file", str(self.token_path()),
                 "--interval", str(self._interval())]
        try:
            child = self._spawn_child(extra)
        except Exception as exc:  # noqa: BLE001
            self.state.link = {"state": "error", "attempt": 0,
                               "msg": "启动采集器失败：" + str(exc)}
            return {"ok": False, "error": self.state.link["msg"]}

        with self._lock:
            self.state.collector = child
            self.state.collecting = True
            self.state.last_spawn_at = int(time.time() * 1000)

        threading.Thread(target=self._read_stdout, args=(child,), name="col-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(child,), name="col-stderr", daemon=True).start()
        threading.Thread(target=self._watch, args=(child,), name="col-watch", daemon=True).start()
        return {"ok": True}

    def stop(self) -> None:
        """停止采集器：摘除引用 → 拒绝在途 ack → kill。"""
        with self._lock:
            child = self.state.collector
            self.state.collector = None
            self.state.collecting = False
        self.reject_pending("采集器已停止，命令未送达")
        if child is None:
            return
        try:
            child.kill()
        except Exception:
            pass

    def _set_sweep_proc(self, proc) -> None:
        self._sweep_proc = proc

    def kill_sweep(self) -> None:
        """杀掉在途的扫描子进程（退出纪律 R2：先杀子进程再退出）。"""
        with self._lock:
            proc = self._sweep_proc
            self._sweep_proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def restart_soon(self) -> None:
        """重启采集器：**等旧进程真的退出**再拉新的（close 后 400ms；4s 兜底）。

        直接"停 + 500ms 后起"有两个坑：① 旧进程（BLE 链路）未必已释放，新旧同抢一台
        充电头会触发 "MIOT RX counter repeated or moved backwards"；② 扫描+重认证本就
        10~20s，多等一两秒无感。
        """
        with self._lock:
            child = self.state.collector
        self.stop()
        if child is None:
            threading.Timer(0.3, self.start).start()
            return

        self._go_fired = False

        def go():
            if self._go_fired:
                return
            self._go_fired = True
            with self._lock:
                running = self.state.collector is not None
            if not running:
                self.start()

        def wait_close():
            try:
                child.wait()
            except Exception:
                pass
            time.sleep(0.4)
            go()

        threading.Thread(target=wait_close, name="col-restart", daemon=True).start()
        threading.Timer(4.0, go).start()

    # -------------------------------------------------------------- 读取线程

    def _read_stdout(self, child) -> None:
        # ⚠️ 必须用 readline（按行）而不是 read(n)：管道上用 BufferedReader.read(n)
        # 会**阻塞到读满 n 字节或 EOF**。采集器是长驻进程、只零星吐行，用 read(n) 会
        # 让整条通道"看着没数据"（实测踩过：hello/link/state 全卡在管道里出不来）。
        try:
            while True:
                raw = child.stdout.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    self._push_log("采集器输出无法解析：" + text[:200])
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    self.on_line(obj)
                except Exception as exc:  # noqa: BLE001
                    self._push_log("处理采集器输出出错：%s" % exc)
        except Exception:
            pass

    def _read_stderr(self, child) -> None:
        try:
            while True:
                raw = child.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._push_log(line)
        except Exception:
            pass

    def _watch(self, child) -> None:
        try:
            code = child.wait()
        except Exception:
            code = None
        self._on_close(child, code)

    def _on_close(self, child, code) -> None:
        self._push_log("采集器退出，code=%s" % code)
        self.state.collecting = False
        self.reject_pending("采集器已退出，命令未送达")
        self.state.writable = []
        self.state.switches = []
        with self._lock:
            if self.state.collector is not child:
                return                      # 主动停止 / 已被替换
            self.state.collector = None
            self.state.link = {"state": "reconnecting", "attempt": self.state.restarts + 1,
                               "msg": "采集器已退出（code=%s），3 秒后重启" % code}
            self.state.restarts += 1
        threading.Timer(3.0, self._restart_if_needed).start()

    def _restart_if_needed(self) -> None:
        with self._lock:
            running = self.state.collector is not None
        if not running:
            self.start()

    # -------------------------------------------------------------- JSONL 分发

    def on_line(self, obj: dict) -> None:
        """处理采集器一行 JSONL（对齐 ``handleCollectorLine``）。"""
        kind = obj.get("t")

        if kind == "hello":
            self._push_log("采集器已启动 pid=%s" % obj.get("pid"))
            if isinstance(obj.get("writable"), list):
                self.state.writable = obj["writable"]
            if isinstance(obj.get("switches"), list):
                self.state.switches = obj["switches"]
            return

        if kind == "ack":
            # ⚠️ 这里只**投递**结果、不弹出在途项：弹出交给等待方（send_command）
            # 或超时分支。否则等待方醒来时条目已被本线程移除，会误判成"设备响应异常"。
            with self._lock:
                pending = self._pending.get(obj.get("id"))
            if pending is not None:
                pending["result"] = obj
                pending["event"].set()
            name = obj.get("name") or ("2." + str(obj.get("piid")))
            if obj.get("ok"):
                readback = obj.get("readback")
                extra = ("（回读 %s）" % readback) if (readback is not None and readback != obj.get("value")) else ""
                self._push_log("控制：%s 设为 %s%s" % (name, obj.get("value"), extra))
            else:
                self._push_log("控制失败：%s — %s" % (name, obj.get("error") or "未知原因"))
            return

        if kind == "link":
            prev = self.state.link if isinstance(self.state.link, dict) else {}
            rssi = obj.get("rssi") if obj.get("rssi") is not None else prev.get("rssi")
            link = {"state": obj.get("state"), "msg": obj.get("msg") or "",
                    "attempt": obj.get("attempt") or 0}
            if rssi is not None:
                # 对齐 server.js：无真实 rssi 时，JSON 里**根本没有** rssi 这个键
                # （Node 的 `rssi: undefined` 会被 JSON.stringify 丢掉；Python 需显式省略）。
                link["rssi"] = rssi
            self.state.link = link
            self._push_log("链路：" + str(obj.get("state"))
                           + (" — " + obj["msg"] if obj.get("msg") else ""))
            return

        if kind == "error":
            self._push_log("采集器报错：" + str(obj.get("msg")))
            return

        if kind == "state":
            self.state.latest = obj
            self.state.restarts = 0
            if not (isinstance(self.state.link, dict) and self.state.link.get("state") == "connected"):
                link = {"state": "connected", "msg": "已连接", "attempt": 0}
                if obj.get("rssi") is not None:      # 对齐 server.js：无真实 rssi 时不带这个键
                    link["rssi"] = obj.get("rssi")
                self.state.link = link
            elif obj.get("rssi") is not None:
                self.state.link["rssi"] = obj.get("rssi")
            self.state.push_history(obj)
            # A 口自动化：每来一帧最新状态就评估一次（内部自带防抖/最小间隔/重入保护）。
            auto_a = getattr(self.app, "auto_a", None)
            enabled = False
            fn = getattr(self.app, "auto_a_enabled", None)
            if callable(fn):
                try:
                    enabled = bool(fn())
                except Exception:
                    enabled = False
            if auto_a is not None and enabled:
                auto_a.request_evaluate()
            return

    # -------------------------------------------------------------- 命令通道

    def reject_pending(self, reason: str) -> None:
        """拒绝所有在途命令（采集器停/退出时）。"""
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
        for pending in items:
            pending["result"] = {"ok": False, "error": reason}
            pending["event"].set()

    def send_command(self, obj: dict, timeout_ms: int = 15000) -> dict:
        """把一条命令写进采集器 stdin 并等它的 ack。

        拒绝路径（对齐 ``server.js``）：未运行 → "采集器未运行，无法下发控制命令"；
        未连接 → "蓝牙尚未连接（当前：…）"；超时 → "设备响应超时"。
        """
        with self._lock:
            child = self.state.collector
            link = self.state.link if isinstance(self.state.link, dict) else {}

        if child is None or child.stdin is None or child.poll() is not None:
            return {"ok": False, "error": "采集器未运行，无法下发控制命令"}
        if link.get("state") != "connected":
            return {"ok": False, "error": "蓝牙尚未连接（当前：%s）" % link.get("state")}

        event = threading.Event()
        with self._lock:
            self._cmd_seq += 1
            cid = self._cmd_seq
            self._pending[cid] = {"event": event, "result": None}

        payload = dict(obj)
        payload["id"] = cid
        try:
            with self._write_lock:
                child.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                child.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._pending.pop(cid, None)
            return {"ok": False, "error": str(exc)}

        if event.wait(timeout_ms / 1000.0):
            with self._lock:
                pending = self._pending.pop(cid, None)
            result = pending["result"] if pending else None
            return result if isinstance(result, dict) else {"ok": False, "error": "设备响应异常"}
        with self._lock:
            self._pending.pop(cid, None)
        return {"ok": False, "error": "设备响应超时"}

    # -------------------------------------------------------------- 隐藏属性扫描

    def run_sweep(self, lo, hi) -> dict:
        """扫描 siid=2 的未知 piid 并枚举 GATT（BLE 同时只允许一个连接 → 先停采集器）。"""
        lo = lo if isinstance(lo, int) else 0x16
        hi = hi if isinstance(hi, int) else 0x40
        token = self.token_path()
        if not token.exists():
            return {"ok": False, "error": "尚未登录小米云，无法认证"}

        with self._lock:
            resume = self.state.collector is not None
        if resume:
            self._push_log("扫描前暂停采集器（蓝牙同一时间只允许一个连接）")
            self.stop()
            time.sleep(1.0)
        self._push_log("开始扫描 siid=2 隐藏属性并枚举 GATT，约 20-40 秒…")

        result = run_role("sweep", [
            "--address", self.effective_address(), "--token-file", str(token),
            "--sweep", "--scan-timeout", "30",
            "--sweep-lo", str(lo), "--sweep-hi", str(hi),
        ], 150000, on_spawn=self._set_sweep_proc)
        self._sweep_proc = None

        if resume:
            threading.Timer(0.5, self.start).start()

        try:
            (paths.data_dir() / "sweep.log").write_text(
                "--- stdout ---\n" + (result.get("stdout") or "")
                + "\n--- stderr ---\n" + (result.get("stderr") or "") + "\n",
                encoding="utf-8")
        except OSError:
            pass

        obj = last_json_of_type(result.get("stdout") or "", "sweep")
        if not obj:
            tail = " | ".join([s for s in (result.get("stdout") or "").strip().split("\n") if s][-3:])
            err = " ".join([x for x in [result.get("error") or "",
                                        (result.get("stderr") or "").strip()[-400:], tail] if x])
            self._push_log("扫描失败：" + err)
            return {"ok": False, "error": err or "没有取到扫描结果"}

        found = obj.get("found") or []
        self._push_log("扫描完成：0x%x..0x%x 共 %d 个 piid，设备可读 %d 个"
                       % (obj.get("lo", lo), obj.get("hi", hi), len(obj.get("rows") or []), len(found)))
        sweep = dict(obj)
        sweep["at"] = int(time.time() * 1000)
        self.state.sweep = sweep
        out = {"ok": True}
        out.update(obj)
        return out


__all__ = ["CollectorBridge", "run_role", "last_json_of_type"]
