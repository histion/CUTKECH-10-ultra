"""内存运行态容器 + 历史/日志 + 通用小工具（等价重写 ``server.js`` 的 ``const state``）。

对齐点（逐字段）：``collector`` / ``collecting`` / ``restarts`` / ``lastSpawnAt`` /
``latest`` / ``link`` / ``history`` / ``logLines`` / ``writable`` / ``switches`` / ``sweep``。

线程模型：HTTP 请求线程、采集器读取线程、自动化线程、托盘/浏览器等都会读改这份状态，
统一用一把 ``RLock`` 保护；历史裁剪、日志环形缓冲都在锁内完成。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime

from . import paths

# 四口顺序（与 ``server.js`` 的 ``PORTS`` 一致）：C1 / C2 / C3 / USB-A。
PORTS = ["c1", "c2", "c3", "a"]

# 历史保留上限：约 1 小时（1.5s 采样）。
HISTORY_MAX = 3600

# UI 日志环形缓冲上限（``/api/log`` 取最近 200 行）。
LOG_MAX_LINES = 400

# 历史文件单文件上限（超出后只留末尾 2000 行），与 ``server.js`` 一致。
_HIST_FILE_MAX = 8 * 1024 * 1024

_LOG_LOCK = threading.Lock()


# ------------------------------------------------------------------ 数值 / 日期小工具

def js_round(value, digits: int = 0) -> float:
    """模拟 JS ``Math.round(v * 10^d) / 10^d``（四舍五入、半值向上）。

    ⚠️ 不能用内置 ``round``：Python 的 ``round`` 是"银行家舍入"（``round(0.5)==0``），
    与 JS 不一致，会让电量/功率末位出现系统性偏差。故显式用 ``floor(v*s+0.5)/s``。
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(n):
        return 0.0
    scale = 10 ** digits
    return math.floor(n * scale + 0.5) / scale


def round3(value) -> float:
    """四舍五入到 3 位小数（``server.js`` 的 ``round3``）。"""
    return js_round(value, 3)


def day_key(ts) -> str:
    """毫秒时间戳 → 本地日期 ``YYYY-MM-DD``（``server.js`` 的 ``dayKey``）。"""
    try:
        lt = time.localtime(float(ts) / 1000.0)
    except (TypeError, ValueError, OSError):
        lt = time.localtime()
    return time.strftime("%Y-%m-%d", lt)


def zero_ports() -> dict:
    """全零四口字典。"""
    return {"c1": 0, "c2": 0, "c3": 0, "a": 0}


def map_ports(p) -> dict:
    """把任意四口字典规范化成 ``{c1,c2,c3,a}``（缺失/非法按 0，值取 3 位小数）。"""
    out = zero_ports()
    src = p if isinstance(p, dict) else {}
    for pid in PORTS:
        out[pid] = round3(src.get(pid))
    return out


def _now_hms() -> str:
    """``zh-CN`` 24 小时制 ``HH:MM:SS``（``server.js`` 的 ``toLocaleTimeString`` 语义）。"""
    return time.strftime("%H:%M:%S", time.localtime())


# ------------------------------------------------------------------ 文件日志（data/app.log）

def log(msg: str) -> None:
    """写 ``data/app.log``（ISO 时间戳）。``CUKTECH_VERBOSE=1`` 时同时打屏。

    ⚠️ ``--windowed`` 下 ``sys.stdout`` 可能是 ``None``，故打屏包在 try 里；
    主进程日志一律以文件为准（design R4）。
    """
    line = "[%s] %s" % (datetime.now().isoformat(), msg)
    if os.environ.get("CUKTECH_VERBOSE") == "1":
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        d = paths.data_dir()
        d.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with open(d / "app.log", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ 运行态

class AppState:
    """线程安全的运行态容器（字段名/语义对齐 ``server.js`` 的 ``const state``）。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.collector = None                     # 采集器子进程（Popen）
        self.collecting = False
        self.restarts = 0
        self.last_spawn_at = 0
        self.latest = None                        # 最近一次 state 行（透传）
        self.link = {"state": "idle", "msg": "尚未启动采集", "attempt": 0}
        self.history: list = []                   # [{at, total, w:{c1,c2,c3,a}}]
        self.log_lines: list = []                 # ["[HH:MM:SS] 文本", ...]
        self.writable: list = []                  # 采集器 hello 声明的可写属性表
        self.switches: list = []                  # 协议开关清单（端口 × 协议 × bit）
        self.sweep = None                         # 最近一次隐藏属性/GATT 扫描结果
        # 由 App 注入的 EnergyLedger：history 落点顺带触发积分（见 push_history）。
        self.energy = None

    def attach_energy(self, ledger) -> None:
        """注入电量账本；每次 ``push_history`` 会同步触发 ``integrate`` + ``touch``。"""
        self.energy = ledger

    def push_history(self, sample: dict) -> dict:
        """把一帧采样落成历史行 ``{at,total,w}`` 并追加到 ``history.jsonl``。

        * ``total`` 用 2 位小数（``server.js`` ``Math.round(total*100)/100``）；
        * 内存历史裁剪到 ``HISTORY_MAX``；
        * 顺带触发电量积分（等价 ``pushHistory`` 里的 ``integrateEnergy(sampleFrom(row))``）。
        """
        total = 0.0
        w: dict = {}
        ports = sample.get("ports") if isinstance(sample, dict) else None
        for p in ports or []:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            w[pid] = p.get("w")
            try:
                total += float(p.get("w") or 0)
            except (TypeError, ValueError):
                pass
        row = {"at": sample.get("at") if isinstance(sample, dict) else None,
               "total": js_round(total, 2), "w": w}

        with self.lock:
            self.history.append(row)
            overflow = len(self.history) - HISTORY_MAX
            if overflow > 0:
                del self.history[0:overflow]

        # 文件追加（与内存历史一致，用于重启后回填）。失败只吞掉。
        try:
            hist = paths.data_file("history.jsonl")
            hist.parent.mkdir(parents=True, exist_ok=True)
            with open(hist, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            if hist.stat().st_size > _HIST_FILE_MAX:
                keep = hist.read_text(encoding="utf-8").split("\n")[-2000:]
                hist.write_text("\n".join(keep), encoding="utf-8")
        except OSError:
            pass

        if self.energy is not None:
            self.energy.integrate(self.energy.sample_from(row))
            self.energy.touch()
        return row

    def push_log(self, line: str) -> None:
        """追加一条 UI 日志（``[HH:MM:SS] 文本``），环形裁剪到 ``LOG_MAX_LINES``。"""
        entry = "[%s] %s" % (_now_hms(), line)
        with self.lock:
            self.log_lines.append(entry)
            overflow = len(self.log_lines) - LOG_MAX_LINES
            if overflow > 0:
                del self.log_lines[0:overflow]

    def snapshot_link(self) -> dict:
        """``link`` 的浅拷贝（供 ``/api/state`` 序列化，避免读到半更新状态）。"""
        with self.lock:
            return dict(self.link) if isinstance(self.link, dict) else self.link

    def history_since(self, since_ms: int) -> list:
        """``at >= since_ms`` 的历史行（``/api/history``）。"""
        with self.lock:
            return [r for r in self.history if isinstance(r.get("at"), (int, float))
                    and r["at"] >= since_ms]

    def tail_logs(self, n: int = 200) -> list:
        with self.lock:
            return list(self.log_lines[-n:])


__all__ = [
    "PORTS",
    "HISTORY_MAX",
    "LOG_MAX_LINES",
    "AppState",
    "day_key",
    "round3",
    "js_round",
    "zero_ports",
    "map_ports",
    "log",
]
