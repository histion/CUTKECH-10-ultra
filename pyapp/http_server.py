"""HTTP 路由 + 全部接口实现（对齐 ``server.js``，前端一行不改）。

设计要点：
  * **门面 ``App``**：持有 ``AppState`` / ``EnergyLedger`` / ``CollectorBridge`` /
    ``LoginProxy`` / ``AutoARunner`` / ``ConfigStore`` / ``TrayManager``，对外暴露
    ``handle_api``（纯函数式返回 ``(status, payload, content_type)``）——这样 ``qa_parity``
    可以直接在进程内断言接口，不必起真 HTTP。
  * **HTTP 层**：标准库 ``ThreadingHTTPServer + BaseHTTPRequestHandler``（本地单用户、
    并发极低；零第三方依赖 = 不增体积、不引打包坑）。
  * 接口 JSON 字段逐条对齐 design §6（含字段名大小写、``ok`` 语义、默认值、裁剪范围）。
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import APP_INFO, MAC_RE_TEXT, VERSION, browser_launcher, control, paths
from .auto_a import AutoARunner
from .collector_bridge import CollectorBridge
from .config_store import ConfigStore
from .energy_ledger import EnergyLedger
from .login_proxy import LoginProxy
from .shortcut import create_shortcut
from .state_store import AppState, day_key, log as file_log, map_ports, round3
from .tray import TrayManager

_MAC_RE = re.compile(MAC_RE_TEXT)

_JSON_CT = "application/json; charset=utf-8"
_TEXT_CT = "text/plain; charset=utf-8"

# 扩展名 → MIME（与 ``server.js`` 的 ``MIME`` 一致）。
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _json(obj, code: int = 200):
    return (code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), _JSON_CT)


def _q1(query, key, default=None):
    v = query.get(key)
    if isinstance(v, list) and v:
        return v[0]
    return default


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(value), 10)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _int0(value):
    """模拟 JS ``parseInt(x, 0)``（支持 ``0x`` 前缀）；解析不出返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        m = re.match(r"^[+-]?\d+", s)
        return int(m.group(0)) if m else None


class App:
    """服务门面：持有全部领域/集成组件，实现所有接口。"""

    def __init__(self) -> None:
        self.state = AppState()
        self.config = ConfigStore()
        self.energy = EnergyLedger(push_log=self.state.push_log)
        self.state.attach_energy(self.energy)
        self.bridge = CollectorBridge(self)
        self.login = LoginProxy(self)
        self.auto_a = AutoARunner(self.state, self.config, self.bridge, self.state.push_log)
        self.tray = TrayManager(self)
        self.port = None
        self.url = None
        self.last_activity = int(time.time() * 1000)
        self.shutdown_cb = None
        self._lock = threading.RLock()

    # -------------------------------------------------------------- 基础设施

    def log(self, msg: str) -> None:
        file_log(msg)

    def push_log(self, line: str) -> None:
        self.state.push_log(line)

    def on_activity(self) -> None:
        self.last_activity = int(time.time() * 1000)

    def token_path(self):
        return paths.data_file("cuktech.token")

    def token_address(self):
        """token 文件里记的设备地址（登录时写入）。"""
        try:
            data = json.loads(self.token_path().read_text(encoding="utf-8"))
            addr = str(data.get("address") or "").upper()
            return addr if _MAC_RE.match(addr) else None
        except Exception:
            return None

    def effective_address(self) -> str:
        """实际要用的设备地址：config 优先，其次 token，都没有就是空串。"""
        cfg = str(self.config.data.get("address") or "").strip().upper()
        if _MAC_RE.match(cfg):
            return cfg
        return self.token_address() or ""

    def set_address(self, mac: str) -> None:
        self.config.data["address"] = mac
        self.config.save()

    def auto_a_enabled(self) -> bool:
        return self.config.data.get("autoA") is True

    # -------------------------------------------------------------- 载荷

    def build_state_payload(self) -> dict:
        """``GET /api/state``（逐字段对齐 §6.2）。"""
        now = int(time.time() * 1000)
        cfg = self.config.data
        tb = self.energy.data["days"].get(day_key(now))
        cur = self.energy.data.get("current")
        mode = cfg.get("trayMode") if cfg.get("trayMode") in ("all", "panel") else "total"
        env_platform = ("win32 " + platform.version()) if sys.platform == "win32" \
            else (sys.platform + " " + platform.release())
        return {
            "ok": True,
            "version": VERSION,
            "app": APP_INFO,
            "now": now,
            "link": self.state.snapshot_link(),
            "collecting": self.state.collecting,
            "config": {
                "address": self.effective_address(),
                "interval": cfg.get("interval"),
                "tray": cfg.get("tray") is not False,
                "trayMode": mode,
                "autoA": cfg.get("autoA") is True,
                # 界面风格（classic / harmony）——前端首次拿到状态时同步一次
                "uiTheme": cfg.get("uiTheme") or "classic",
            },
            "env": {
                "python": str(paths.exe_path()),
                "pythonFound": True,
                "tokenPresent": self.token_path().exists(),
                "appDir": str(paths.root_dir()),
                "platform": env_platform,
            },
            "latest": self.state.latest,
            "historyCount": len(self.state.history),
            "writable": self.state.writable,
            "switches": self.state.switches,
            "sweep": self.state.sweep,
            "energy": {
                "todayWh": round3(tb.get("total")) if isinstance(tb, dict) else 0,
                "totalWh": round3(self.energy.data.get("totalWh", 0)),
                "tracking": isinstance(cur, dict),
                "current": {
                    "ms": (cur.get("lastActiveAt") or 0) - (cur.get("start") or 0),
                    "wh": round3(cur.get("wh", 0)),
                    "peak": round3(cur.get("peak", 0)),
                    "ports": map_ports(cur.get("ports")),
                } if isinstance(cur, dict) else None,
            },
            "login": self.login.current(),
        }

    def build_energy_payload(self, days) -> dict:
        return self.energy.snapshot(days)

    # -------------------------------------------------------------- 路由

    def handle_api(self, method: str, path: str, query: dict, body):
        """返回 ``(status:int, payload:bytes, content_type:str)``。"""
        self.on_activity()

        if method == "GET":
            if path == "/api/ping":
                return _json({"ok": True, "v": VERSION})
            if path == "/api/state":
                return _json(self.build_state_payload())
            if path == "/api/history":
                sec = _clamp_int(_q1(query, "seconds"), 300, 10, 3600)
                since = int(time.time() * 1000) - sec * 1000
                return _json({"ok": True, "rows": self.state.history_since(since)})
            if path == "/api/log":
                return _json({"ok": True, "lines": self.state.tail_logs(200)})
            if path == "/api/energy":
                days = _clamp_int(_q1(query, "days"), 7, 1, 90)
                return _json(self.build_energy_payload(days))
            if path == "/api/login/qr.png":
                try:
                    return (200, paths.data_file("qr.png").read_bytes(), "image/png")
                except OSError:
                    return (404, b"no qr", _TEXT_CT)
            return self._static(path)

        if method == "POST":
            if path == "/api/login/start":
                return _json(self.login.start())
            if path == "/api/login/poll":
                return _json(self.login.poll())
            if path == "/api/reconnect":
                self.push_log("手动重连…")
                self.bridge.restart_soon()
                return _json({"ok": True})
            if path == "/api/shortcut":
                return _json(create_shortcut(self.push_log))
            if path == "/api/control":
                return _json(control.handle_control(self.bridge, body))
            if path == "/api/sweep":
                lo = _int0(_q1(query, "lo"))
                hi = _int0(_q1(query, "hi"))
                return _json(self.bridge.run_sweep(lo, hi))
            if path == "/api/config":
                return self._config(body)
            if path == "/api/show":
                if self.url:
                    browser_launcher.launch_window(self.url, self.log)
                return _json({"ok": True})
            if path == "/api/quit":
                self.push_log("收到退出请求")
                if callable(self.shutdown_cb):
                    threading.Timer(0.2, self.shutdown_cb).start()
                return _json({"ok": True})

        return self._static(path)

    def _config(self, body) -> tuple:
        """``POST /api/config``：校验 + 生效 + 记日志 + 按需重启采集器（对齐 §6.2）。"""
        try:
            j = body if isinstance(body, dict) else {}
            cfg = self.config.data
            prev_interval = cfg.get("interval")
            prev_address = self.effective_address()

            interval = j.get("interval")
            if isinstance(interval, (int, float)) and not isinstance(interval, bool) \
                    and 0.5 <= interval <= 10:
                cfg["interval"] = interval
            if isinstance(j.get("address"), str) and _MAC_RE.match(j["address"]):
                cfg["address"] = j["address"].upper()
            if isinstance(j.get("tray"), bool):
                cfg["tray"] = j["tray"]
                if j["tray"]:
                    self.tray.start()
                else:
                    self.tray.stop()
            if j.get("trayMode") in ("total", "all", "panel"):
                mode_changed = cfg.get("trayMode") != j["trayMode"]
                cfg["trayMode"] = j["trayMode"]
                if mode_changed:
                    label = ("一行面板（四口+总）" if j["trayMode"] == "panel"
                             else ("图标轮流显示" if j["trayMode"] == "all" else "仅总功率"))
                    self.push_log("任务栏显示模式：" + label)

            if j.get("uiTheme") in ("classic", "harmony"):
                # 纯外观开关：只落盘，不重启采集器（重启会白白断一次蓝牙）
                if j["uiTheme"] != cfg.get("uiTheme"):
                    cfg["uiTheme"] = j["uiTheme"]
                    self.push_log("界面风格：" + ("鸿蒙 UI" if j["uiTheme"] == "harmony" else "经典"))

            auto_changed = False
            if isinstance(j.get("autoA"), bool) and j["autoA"] != cfg.get("autoA"):
                cfg["autoA"] = j["autoA"]
                auto_changed = True
                self.auto_a.reset_counters()          # 切换开关时清空运行态
                self.push_log("A口自动化：" + ("已开启" if cfg["autoA"] else "已关闭"))

            self.config.save()
            self.push_log("配置已保存：间隔 %ss" % cfg.get("interval"))
            if auto_changed and cfg.get("autoA"):
                self.auto_a.request_evaluate()

            # 只有采样间隔 / 有效地址**真的变了**才重启采集器（纯托盘开关别断线重连）。
            if cfg.get("interval") != prev_interval or self.effective_address() != prev_address:
                self.bridge.restart_soon()
            return _json({"ok": True, "config": self.config.snapshot()})
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "error": str(exc)}, 400)

    def _static(self, path: str) -> tuple:
        """静态文件：``/``→index.html、``/favicon.ico``→app.ico，其余回退 web，越界 403。"""
        if path in ("/", "/index.html"):
            rel = "index.html"
        elif path == "/favicon.ico":
            ico = paths.resource_path("app.ico")
            if not ico.exists():
                ico = paths.root_dir() / "app.ico"
            try:
                return (200, ico.read_bytes(), "image/x-icon")
            except OSError:
                return (204, b"", "")
        else:
            rel = path

        web = paths.meipass_dir() / "web"
        norm = os.path.normpath(rel.lstrip("/\\"))
        target = web / norm
        try:
            target.relative_to(web)
        except ValueError:
            return _json({"error": "forbidden"}, 403)
        try:
            if not target.is_file():
                raise OSError
            return (200, target.read_bytes(), MIME.get(target.suffix.lower(),
                                                      "application/octet-stream"))
        except OSError:
            return (404, b"404", _TEXT_CT)


def make_handler(app: App):
    """返回绑定了 ``app`` 的 ``BaseHTTPRequestHandler`` 子类。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "cuktech10ultra/" + VERSION

        def log_message(self, *_args):     # 静音默认访问日志
            pass

        def _read_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                n = 0
            if n <= 0:
                return None
            raw = self.rfile.read(min(n, 65536))
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return None

        def _send(self, code: int, payload: bytes, ctype: str) -> None:
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
            if code != 204:
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if code != 204 and payload:
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionError):
                    pass

        def _run(self, method: str) -> None:
            u = urlparse(self.path)
            query = parse_qs(u.query)
            try:
                body = self._read_body() if method == "POST" else None
                code, payload, ctype = app.handle_api(method, u.path, query, body)
            except Exception as exc:  # noqa: BLE001
                code, payload, ctype = _json({"ok": False, "error": str(exc)}, 500)
            self._send(code, payload, ctype)

        def do_GET(self) -> None:
            self._run("GET")

        def do_POST(self) -> None:
            self._run("POST")

    return Handler


def build_server(app: App, host: str = "127.0.0.1", port: int = 0):
    """创建 ``ThreadingHTTPServer`` 并绑定 ``app``；返回 server（调用方负责 serve_forever）。"""
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd


__all__ = ["App", "make_handler", "build_server", "MIME"]
