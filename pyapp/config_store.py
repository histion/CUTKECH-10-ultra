"""``config.json`` 的读写与校验（等价重写 ``server.js`` 的 ``loadConfig``/``saveConfig``）。

为什么单独成模块：配置既被 HTTP 层读（``/api/state`` 回显、``/api/config`` 写入），
又被设备链路读（采样间隔、设备地址），还被登录代理写（自动发现 MAC）——集中一处，
避免"三处各自解析同一份 JSON"。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from . import paths

# 默认配置。语义对齐 ``server.js`` 的 ``loadConfig`` 默认对象：
#   address 不再硬编码（便携包要发给别人，对方充电头 MAC 必然不同）；
#   interval 采样间隔（秒）；tray 托盘总开关；trayMode 托盘显示模式；autoA A 口自动化。
DEFAULTS: dict = {
    "address": "",
    "interval": 1.5,
    "tray": True,
    "trayMode": "total",
    "autoA": False,
}

# 托盘显示模式的合法取值（与 ``server.js`` 一致）。
TRAY_MODES = ("total", "all", "panel")


class ConfigStore:
    """``config.json`` 的持久化与规范化校验。

    线程安全：HTTP 写、登录代理写、设备链路读可能并发，用一把可重入锁串行化。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        # 默认落 ``CUKTECH_DATA_DIR/config.json``；测试可注入隔离路径。
        self.path = Path(path) if path is not None else (paths.data_dir() / "config.json")
        self._lock = threading.RLock()
        self.data: dict = self.load()

    def load(self) -> dict:
        """读盘并规范化。任何异常（缺失/损坏）都回退默认值，绝不抛错。"""
        raw: dict = {}
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                raw = parsed
        except Exception:
            raw = {}
        return self.validate(raw)

    def save(self, cfg: dict | None = None) -> None:
        """写盘（缩进 2，与 ``server.js`` 的 ``JSON.stringify(c, null, 2)`` 一致）。

        传入 ``cfg`` 时先规范化再写，保证落盘内容始终合法。写失败只吞掉——
        配置保存失败不该拖垮主流程（与旧实现一致）。
        """
        with self._lock:
            if cfg is not None:
                self.data = self.validate(cfg)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(self.data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

    def get(self, key: str, default=None):
        """读一个字段（浅拷贝语义：数值/字符串/bool 直接返回）。"""
        with self._lock:
            return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        """改一个字段（不落盘，由调用方决定何时 ``save``）。"""
        with self._lock:
            self.data[key] = value

    @staticmethod
    def validate(cfg: dict | None) -> dict:
        """把任意输入规范成合法配置。

        规则（等价于 ``server.js`` ``loadConfig`` + ``/api/config`` 的约束）：
          * ``interval``：数值且落在 ``[0.5, 10]``，否则回落默认 1.5；
          * ``trayMode``：仅 ``total|all|panel``，否则 ``total``；
          * ``autoA`` / ``tray``：收敛为布尔；
          * ``address``：字符串；统一去空白并大写（与 ``/api/config`` 写入语义一致）。
        """
        out = dict(DEFAULTS)
        if isinstance(cfg, dict):
            out.update(cfg)

        try:
            interval = float(out.get("interval"))
        except (TypeError, ValueError):
            interval = DEFAULTS["interval"]
        if not (0.5 <= interval <= 10):
            interval = DEFAULTS["interval"]
        out["interval"] = interval

        if out.get("trayMode") not in TRAY_MODES:
            out["trayMode"] = "total"

        out["autoA"] = out.get("autoA") is True
        out["tray"] = out.get("tray") is not False

        addr = out.get("address")
        out["address"] = addr.strip().upper() if isinstance(addr, str) else ""

        return out

    def snapshot(self) -> dict:
        """返回一份用于 ``/api/config`` / ``/api/state`` 回显的浅拷贝。"""
        with self._lock:
            return dict(self.data)


__all__ = ["ConfigStore", "DEFAULTS", "TRAY_MODES"]
