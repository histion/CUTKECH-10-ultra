"""接口一致性回归（对齐 ``server.js`` 字段）。

四层校验：
  A) 进程内：直接调 ``App.handle_api``，断言 ``/api/state`` / ``/api/energy`` 等**字段集合与类型**；
  B) 真服务（源码）：起 ``python -m pyapp``（隔离 ``CUKTECH_HOME``、无窗无托盘），抓字段做结构断言，
     并验证 ``/api/ping → /api/config{autoA:true} → /api/quit`` 全链路与"退出后无 ``_MEI`` 残留"；
  C) 旧对照（可选）：若 ``dist\\cuktech 10 ultra.exe`` 存在，隔离启动旧 Node 版，与新 Python 版
     逐接口做**结构 schema** diff，差异应为空（前端一行不改的前提）；
  D) 打包产物：若 ``dist_py\\cuktech 10 ultra.exe`` 存在，冒烟其冷启动耗时 / 接口 / 干净退出 / 无残留。

离线运行：python build_py/qa_parity.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OLD_EXE = ROOT / "dist" / "cuktech 10 ultra.exe"
NEW_EXE = ROOT / "dist_py" / "cuktech 10 ultra.exe"
NEW_CMD = [sys.executable, "-m", "pyapp"]

PASS = 0
FAIL = 0
NOTES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


# ------------------------------------------------------------------ HTTP / schema

def fetch(port: int, path: str, method: str = "GET", body=None, timeout=8):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if method == "POST":
        data = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def tname(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _schema(v):
    if isinstance(v, dict):
        return {k: _schema(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_schema(v[0])] if v else []
    return tname(v)


def _t(s):
    return s if isinstance(s, str) else type(s).__name__


def diff_schema(a, b, path="$"):
    """比较两个 schema，返回差异字符串列表（键集合 & 标量类型）。"""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append("%s.%s 仅 B 有（type=%s）" % (path, k, _t(b[k])))
            elif k not in b:
                out.append("%s.%s 仅 A 有（type=%s）" % (path, k, _t(a[k])))
            else:
                out += diff_schema(a[k], b[k], "%s.%s" % (path, k))
    elif isinstance(a, list) and isinstance(b, list):
        if a and b:
            out += diff_schema(a[0], b[0], path + "[]")
    elif isinstance(a, str) and isinstance(b, str):
        if a != b:
            out.append("%s 类型不同：A=%s B=%s" % (path, a, b))
    else:
        out.append("%s 结构不同：A=%s B=%s" % (path, _t(a), _t(b)))
    return out


# ------------------------------------------------------------------ A) 进程内

STATE_KEYS = {"ok", "version", "app", "now", "link", "collecting", "config", "env",
              "latest", "historyCount", "writable", "switches", "sweep", "energy", "login"}
ENERGY_KEYS = {"ok", "today", "totalWh", "ports", "days", "current", "sessions", "since", "tracked"}


def part_a(tmp_home: Path) -> dict:
    os.environ["CUKTECH_HOME"] = str(tmp_home)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    os.environ["CUKTECH_NO_WINDOW"] = "1"
    os.environ["CUKTECH_NO_TRAY"] = "1"
    from pyapp.http_server import App

    app = App()
    schemas = {}

    code, payload, ct = app.handle_api("GET", "/api/ping", {}, None)
    ping = json.loads(payload)
    check("A /api/ping ok + v=1.3.0", code == 200 and ping == {"ok": True, "v": "1.3.0"}, str(ping))
    check("A Content-Type json", ct.startswith("application/json"), ct)

    code, payload, _ = app.handle_api("GET", "/api/state", {}, None)
    st = json.loads(payload)
    check("A /api/state 顶层字段集合正确", set(st.keys()) == STATE_KEYS,
          "多了 %s 少了 %s" % (set(st) - STATE_KEYS, STATE_KEYS - set(st)))
    check("A /api/state.app = name/version/vendor",
          set(st["app"]) == {"name", "version", "vendor"}, str(st["app"]))
    check("A /api/state.config 字段正确",
          set(st["config"]) == {"address", "interval", "tray", "trayMode", "autoA"}, str(st["config"]))
    check("A /api/state.env 字段正确",
          set(st["env"]) == {"python", "pythonFound", "tokenPresent", "appDir", "platform"}, str(st["env"]))
    check("A /api/state.energy 字段正确",
          set(st["energy"]) == {"todayWh", "totalWh", "tracking", "current"}, str(st["energy"]))
    check("A /api/state.login 字段正确",
          set(st["login"]) == {"running", "kind", "result"}, str(st["login"]))
    check("A /api/state.version=1.3.0 / app.version=1.0.0",
          st["version"] == "1.3.0" and st["app"]["version"] == "1.0.0")
    check("A /api/state.env.pythonFound=True（解释器内置）", st["env"]["pythonFound"] is True)
    schemas["/api/state"] = _schema(st)

    code, payload, _ = app.handle_api("GET", "/api/energy", {"days": ["7"]}, None)
    en = json.loads(payload)
    check("A /api/energy 顶层字段集合正确", set(en.keys()) == ENERGY_KEYS,
          "多了 %s 少了 %s" % (set(en) - ENERGY_KEYS, ENERGY_KEYS - set(en)))
    check("A /api/energy.days 长度=7", len(en["days"]) == 7)
    check("A /api/energy.ports 四口", set(en["ports"]) == {"c1", "c2", "c3", "a"})
    schemas["/api/energy"] = _schema(en)

    code, payload, _ = app.handle_api("GET", "/api/history", {"seconds": ["300"]}, None)
    hi = json.loads(payload)
    check("A /api/history 字段 {ok,rows}", set(hi.keys()) == {"ok", "rows"}, str(set(hi)))
    schemas["/api/history"] = _schema(hi)

    code, payload, _ = app.handle_api("GET", "/api/log", {}, None)
    lg = json.loads(payload)
    check("A /api/log 字段 {ok,lines}", set(lg.keys()) == {"ok", "lines"}, str(set(lg)))
    schemas["/api/log"] = _schema(lg)

    code, payload, ct = app.handle_api("GET", "/", {}, None)
    check("A GET / 返回 web/index.html", code == 200 and b"<" in payload and "html" in ct,
          "code=%s ct=%s" % (code, ct))
    code, payload, ct = app.handle_api("GET", "/favicon.ico", {}, None)
    check("A GET /favicon.ico 返回 ico", code == 200 and ct == "image/x-icon", "code=%s ct=%s" % (code, ct))
    code, payload, ct = app.handle_api("GET", "/api/login/qr.png", {}, None)
    check("A 无二维码时 /api/login/qr.png → 404 'no qr'", code == 404 and payload == b"no qr",
          "code=%s" % code)
    code, payload, _ = app.handle_api("POST", "/api/nope", {}, {})
    check("A 未知路径回退静态 404", code == 404, "code=%s" % code)
    return schemas


# ------------------------------------------------------------------ 真服务（源码 / exe 通用）

def wait_ready(home: Path, timeout=60):
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


def temp_mei_snapshot() -> set:
    base = Path(tempfile.gettempdir())
    try:
        return {p.name for p in base.glob("_MEI*")}
    except OSError:
        return set()


def _live_run(cmd, cwd, home, tag, timeout=90):
    """起一个真服务、抓接口结构、跑 config/quit、校验干净退出与无 _MEI 残留。"""
    env = dict(os.environ)
    env["CUKTECH_HOME"] = str(home)
    env.pop("CUKTECH_DATA_DIR", None)
    env["CUKTECH_NO_WINDOW"] = "1"
    env["CUKTECH_NO_TRAY"] = "1"
    before = temp_mei_snapshot()
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    schemas = {}
    elapsed = None
    try:
        port = wait_ready(home, timeout=timeout)
        elapsed = time.time() - t0
        check("%s 冷启动就绪（%.1fs）" % (tag, elapsed), port is not None)
        if port is None:
            return schemas, elapsed
        code, ping = fetch(port, "/api/ping")
        check("%s /api/ping ok" % tag, ping.get("ok") is True)
        code, st = fetch(port, "/api/state")
        check("%s /api/state 顶层字段集合正确" % tag, set(st.keys()) == STATE_KEYS,
              "多了 %s 少了 %s" % (set(st) - STATE_KEYS, STATE_KEYS - set(st)))
        schemas["/api/state"] = _schema(st)
        code, en = fetch(port, "/api/energy?days=7")
        check("%s /api/energy 顶层字段集合正确" % tag, set(en.keys()) == ENERGY_KEYS)
        schemas["/api/energy"] = _schema(en)
        code, hi = fetch(port, "/api/history?seconds=300")
        schemas["/api/history"] = _schema(hi)
        code, lg = fetch(port, "/api/log")
        schemas["/api/log"] = _schema(lg)
        code, cfg = fetch(port, "/api/config", "POST", {"autoA": True})
        check("%s POST /api/config{autoA:true} 生效" % tag,
              cfg.get("ok") is True and cfg.get("config", {}).get("autoA") is True, str(cfg))
        code, st2 = fetch(port, "/api/state")
        check("%s autoA 生效后 config.autoA=true" % tag, st2["config"]["autoA"] is True)
        code, q = fetch(port, "/api/quit", "POST", {})
        check("%s POST /api/quit ok" % tag, q.get("ok") is True)
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            rc = None
        check("%s 退出码 0" % tag, rc == 0, "rc=%s" % rc)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    time.sleep(0.5)
    leftover = temp_mei_snapshot() - before
    check("%s 退出后无 _MEI 残留" % tag, not leftover, str(leftover))
    return schemas, elapsed


def part_b(tmp_home: Path) -> dict:
    schemas, _ = _live_run(NEW_CMD, ROOT, tmp_home / "live_src", "B 源码", timeout=60)
    return schemas


def part_d(tmp_home: Path) -> None:
    if not NEW_EXE.exists():
        NOTES.append("未找到 dist_py 产物，跳过 D 段：" + str(NEW_EXE))
        return
    schemas, elapsed = _live_run([str(NEW_EXE)], NEW_EXE.parent, tmp_home / "live_exe", "D exe", timeout=120)
    size = NEW_EXE.stat().st_size
    print("    新 exe：%d 字节（%.2f MB），冷启动 ~%.1fs" % (size, size / (1024 * 1024), elapsed or -1))
    check("D 新 exe 体积在 16–22MB 量级", 16 * 1024 * 1024 <= size <= 22 * 1024 * 1024,
          "%.2f MB" % (size / (1024 * 1024)))
    check("D exe /api/state 结构正确", "/api/state" in schemas and set(schemas["/api/state"]) == STATE_KEYS)


# ------------------------------------------------------------------ C) 旧对照

def part_c(new_schemas: dict) -> None:
    if os.environ.get("CUKTECH_SKIP_OLD") == "1":
        NOTES.append("CUKTECH_SKIP_OLD=1，跳过旧版对照")
        return
    if not OLD_EXE.exists():
        NOTES.append("旧版 exe 不存在，跳过 C 段对照：" + str(OLD_EXE))
        return
    tmp = Path(tempfile.mkdtemp(prefix="cuk_old_"))
    env = dict(os.environ)
    env["CUKTECH_HOME"] = str(tmp)
    env.pop("CUKTECH_DATA_DIR", None)
    env["CUKTECH_NO_WINDOW"] = "1"
    env["CUKTECH_NO_TRAY"] = "1"
    proc = subprocess.Popen([str(OLD_EXE)], cwd=str(OLD_EXE.parent), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port = wait_ready(tmp, timeout=120)
        check("C 旧 Node 版就绪", port is not None)
        if port is None:
            NOTES.append("旧版未在 120s 内就绪，跳过 C 段 diff")
            return
        for path, key in (("/api/state", "/api/state"), ("/api/energy?days=7", "/api/energy")):
            code, obj = fetch(port, path)
            old = _schema(obj)
            new = new_schemas.get(key)
            if new is None:
                continue
            diffs = diff_schema(old, new, key)
            check("C %s 结构 diff 为空" % key, not diffs, "；".join(diffs[:12]))
        try:
            fetch(port, "/api/quit", "POST", {}, timeout=3)
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tmp_home = Path(tempfile.mkdtemp(prefix="cuk_parity_"))
    try:
        sa = part_a(tmp_home / "inproc")
        sb = part_b(tmp_home)
        for key in ("/api/state", "/api/energy"):
            if key in sa and key in sb:
                diffs = diff_schema(sa[key], sb[key], key)
                check("A↔B %s 结构一致" % key, not diffs, "；".join(diffs[:8]))
        part_d(tmp_home)
        part_c({**sa, **sb})
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)

    for n in NOTES:
        print("NOTE " + n)
    print("-" * 60)
    print("qa_parity 汇总：通过 %d，失败 %d" % (PASS, FAIL))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
