"""入口：角色分发 + 服务生命周期（单实例探测 / 空闲自杀 / shutdown）。

角色模型与现状 1:1（design §4.4）：
  * ``server``（默认）：HTTP 服务 + 托盘 + 窗口；
  * ``collector`` / ``sweep``：``runpy`` 复用现有 ``collector.py``；
  * ``login-qr`` / ``login-poll``：``runpy`` 复用现有 ``login.py``。
子进程由 ``paths.child_argv('--role', …)`` 生成，PyInstaller 引导器靠 ``_MEIPASS2``
**跳过二次解压**（实测 ~0.21s）。

⚠️ 退出纪律（risk R2）：onefile 引导器在**主进程退出时**删 ``_MEIPASS``；若有子进程仍
占用该目录会删不掉并残留 ``_MEI``。故 ``shutdown()`` **必须先 kill 全部子进程**
（collector / sweep / login / tray）再退出。
"""

from __future__ import annotations

import json
import os
import runpy
import signal
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

# 兼容两种入口：开发期 ``python -m pyapp``（包已可导入）与打包后 __main__ 被当脚本执行。
try:
    import pyapp  # noqa: F401
except ImportError:  # pragma: no cover - 仅打包边界兜底
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyapp import browser_launcher, paths  # noqa: E402
from pyapp.http_server import App, build_server  # noqa: E402
from pyapp.state_store import log as _log  # noqa: E402

# 无请求 2 分钟后自杀，避免留下幽灵进程（每 15s 检查一次）。
IDLE_EXIT_MS = 120000
_IDLE_CHECK_MS = 15000

_APP = None
_STOPPING = False
_LOCK = threading.RLock()


def probe(port: int) -> bool:
    """探测某端口上是否已有本服务在跑（``/api/ping`` 返回 ``ok:true``）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % int(port), timeout=1.2) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def _run_script(rel: str, argv: list) -> int:
    """以 ``__main__`` 身份跑一个复用脚本（``collector.py`` / ``login.py``）。"""
    script = paths.resource_path(rel)
    try:
        os.chdir(str(paths.meipass_dir()))     # 让脚本内 ``Path(__file__).parent`` 定位稳定
    except OSError:
        pass
    sys.argv = [str(script), *argv]
    runpy.run_path(str(script), run_name="__main__")
    return 0


def shutdown() -> None:
    """干净退出：先落盘账本 → 杀全部子进程 → 删 lock/ready → 300ms 兜底 exit(0)。"""
    global _STOPPING
    with _LOCK:
        if _STOPPING:
            return
        _STOPPING = True
    app = _APP

    # 退出前把电量账本落盘（脏标记 + 15s 节流，内存里可能还有没写盘的增量）。
    if app is not None:
        try:
            app.energy.dirty = True
            app.energy.save(force=True)
        except Exception:
            pass
        # 先杀全部子进程（顺序无关紧要，但必须在引导器删 _MEIPASS 之前）。
        for stop in (lambda: app.tray.stop(),
                     lambda: app.bridge.stop(),
                     lambda: app.bridge.kill_sweep(),
                     lambda: app.login.stop()):
            try:
                stop()
            except Exception:
                pass

    _log("收到退出请求")

    lock = paths.data_file("running.lock")
    ready = paths.data_file("ready.json")

    def _finish():
        for p in (lock, ready):
            try:
                p.unlink()
            except OSError:
                pass
        os._exit(0)

    for p in (lock, ready):                    # 先尽力同步清一次
        try:
            p.unlink()
        except OSError:
            pass
    timer = threading.Timer(0.3, _finish)      # 300ms 兜底：删不掉也退
    timer.daemon = True
    timer.start()


def run_server() -> int:
    """默认角色：起 HTTP 服务（含单实例探测 / 窗口 / 托盘 / 采集器 / 空闲自杀）。"""
    global _APP

    paths.resolve_root()          # 先校验稳定根（非法 HOME → 回落 %TEMP%，否则上抛给兜底）
    paths.apply_env_contract()
    paths.ensure_runtime_assets()
    paths.maybe_import_data()                                  # 必须先迁移、后清理
    threading.Thread(target=paths.cleanup_legacy_app_tree, daemon=True).start()
    threading.Thread(target=browser_launcher.cleanup_legacy_profile,
                     args=(_log,), daemon=True).start()

    app = App()
    _APP = app
    app.shutdown_cb = shutdown

    app.energy.load()
    filled = app.energy.backfill()
    app.log("能量账本已载入：累计 %s Wh，回填 %d 个采样"
            % (round(app.energy.data.get("totalWh", 0), 3), filled))

    # 已经在跑就直接开窗口，不要起第二个服务。
    lock = paths.data_file("running.lock")
    try:
        lk = json.loads(lock.read_text(encoding="utf-8"))
        if isinstance(lk, dict) and lk.get("port") and probe(int(lk["port"])):
            url = "http://127.0.0.1:%d/" % int(lk["port"])
            browser_launcher.launch_window(url, app.log)
            return 0
    except Exception:
        pass

    httpd = build_server(app)
    port = httpd.server_address[1]
    app.port = port
    app.url = "http://127.0.0.1:%d/" % port
    try:
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"port": port, "pid": os.getpid(),
                                    "at": int(time.time() * 1000)}), encoding="utf-8")
        paths.data_file("ready.json").write_text(
            json.dumps({"port": port, "pid": os.getpid(), "url": app.url}), encoding="utf-8")
    except OSError:
        pass
    app.log("listening " + app.url)

    if os.environ.get("CUKTECH_NO_WINDOW") != "1":
        browser_launcher.launch_window(app.url, app.log)
    app.tray.start()
    app.bridge.start()

    def _idle_watch():
        while True:
            time.sleep(_IDLE_CHECK_MS / 1000.0)
            if int(time.time() * 1000) - app.last_activity > IDLE_EXIT_MS:
                app.log("空闲超时，自动退出")
                shutdown()
                return

    threading.Thread(target=_idle_watch, name="idle-watch", daemon=True).start()

    try:
        signal.signal(signal.SIGINT, lambda *_a: shutdown())
        signal.signal(signal.SIGTERM, lambda *_a: shutdown())
    except (ValueError, OSError):
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown()
    return 0


def main(argv=None) -> int:
    """角色分发。``--role <role>`` 之后的所有参数原样转给被复用的脚本。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    role = "server"
    rest = argv
    if "--role" in argv:
        idx = argv.index("--role")
        role = argv[idx + 1] if idx + 1 < len(argv) else "server"
        rest = argv[:idx] + argv[idx + 2:]

    if role in ("collector", "sweep"):
        return _run_script("collector.py", rest)
    if role in ("login-qr", "login-poll"):
        return _run_script("login.py", rest)
    return run_server()


def _write_launch_error(exc: BaseException) -> None:
    """把启动期未捕获异常写成 ``%TEMP%\\cuktech-launch-error.log``（对齐旧 Node 版基线）。

    ⚠️ 关键：``--windowed`` 打包下，任何"逃逸出 Python"的异常都会被 PyInstaller
    bootloader 渲染成**模态弹窗**（标题 ``Unhandled exception in script``）并**一直等点击**，
    无人值守时表现为"进程卡死、stdout/stderr 为空"。旧 Node 版对非法 HOME 是
    「快速退出 + 写 %TEMP%\\cuktech-launch-error.log」，这里必须回到同样的行为。

    写日志本身也全程兜底：``%TEMP%`` 不可写就静默（绝不再抛，避免二次弹框）。
    """
    try:
        temp = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
        path = Path(temp) / "cuktech-launch-error.log"
        lines = [
            "[cuktech 10 ultra] 启动失败 —— %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "argv=%r\n" % (sys.argv,),
            "frozen=%r  exe=%r\n" % (bool(getattr(sys, "frozen", False)), sys.executable),
            "CUKTECH_HOME=%r\n" % os.environ.get("CUKTECH_HOME"),
            "CUKTECH_DATA_DIR=%r\n" % os.environ.get("CUKTECH_DATA_DIR"),
            "cwd=%r\n" % os.getcwd(),
            "-" * 64 + "\n",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
        path.write_text("".join(lines), encoding="utf-8", errors="replace")
    except BaseException:                          # noqa: BLE001 - 日志兜底绝不再抛
        pass


def run() -> int:
    """真正的程序入口：**任何**未捕获异常都写日志 + ``os._exit(1)``，绝不抛给 bootloader。

    覆盖全部启动路径（含 ``--role`` 角色分发）。这样非法 HOME / 非法参数 / 资源缺失
    等一律"快速退出 + 落日志"，不产生任何模态窗口。
    """
    try:
        return main()
    except SystemExit:
        raise                                     # 正常的显式退出（sys.exit）照常
    except BaseException as exc:                  # noqa: BLE001 - 启动期一律兜底
        _write_launch_error(exc)
        os._exit(1)


if __name__ == "__main__":
    sys.exit(run())
