"""QA 回归自测：``/api/state.link`` 键集合**严格对齐**旧 Node 版（含未连接 / 已连接两态）。

背景：修复前 Python 版在未连接时 `link` 多出一个 `rssi: null` 键，而旧 Node 版此时
**根本没有该键**（Node 里 `rssi: undefined` 会被 ``JSON.stringify`` 丢掉；Python 的
``None`` 却会序列化成 ``null``）。前端 `!= null` 判空虽等价，但要求字段集合严格一致。

两段：
  A) **真机对照**：同一隔离环境启动旧 Node exe 与新 Python exe（均无 token）→ 抓
     ``/api/state.link`` 键集合 → 断言两侧完全一致（未连接态）。
  B) **已连接态**：用假采集器驱动新 Python 侧 ``CollectorBridge``，覆盖
     「无 rssi 的 link」「有 rssi 的 link」「rssi=null 的 link」三种帧，断言键集合
     与 ``server.js`` 构造规则一致（已连接 = {state,msg,attempt,rssi}）。

离线运行：python pyapp/tests/test_link_keys.py
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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OLD_EXE = ROOT / "dist" / "cuktech 10 ultra.exe"
NEW_EXE = ROOT / "dist_py" / "cuktech 10 ultra.exe"
NEW_CMD = [sys.executable, "-m", "pyapp"]

PASS = 0
FAIL = 0
SKIP = 0
MAC = "AA:BB:CC:DD:EE:FF"

FAKE_COLLECTOR = r'''
import sys, json, time
def emit(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n"); sys.stdout.flush()
emit({"t": "hello", "v": "1.3", "pid": 7, "writable": [], "switches": []})
emit({"t": "link", "state": "connecting", "msg": "连接中", "attempt": 1})          # 无 rssi
time.sleep(0.5)
emit({"t": "link", "state": "reconnecting", "msg": "重连中", "attempt": 2, "rssi": None})  # 显式 null
time.sleep(0.5)
emit({"t": "link", "state": "connected", "msg": "已连接", "rssi": -55})            # 有 rssi
emit({"t": "state", "at": int(time.time() * 1000), "ok": True, "rssi": -55,
      "ports": [{"id": "c1", "w": 5}], "settings": {"port_ctl": 15}})
for line in sys.stdin:
    if line.strip() in ('{"cmd": "quit"}', '{"cmd":"quit"}'):
        break
'''


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


def skip(name, detail=""):
    global SKIP
    SKIP += 1
    print("SKIP " + name + (("  → " + detail) if detail else ""))


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


def get(port, path, method="GET", body=None, timeout=8):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    h = {"Content-Type": "application/json"} if method == "POST" else {}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def link_keys_of(exe_cmd, cwd, tag):
    """起一个真服务（新/旧皆可），抓 link 键集合后退出。"""
    home = Path(tempfile.mkdtemp(prefix="cuk_link_%s_" % tag))
    env = dict(os.environ)
    env["CUKTECH_HOME"] = str(home)
    env.pop("CUKTECH_DATA_DIR", None)
    env["CUKTECH_NO_WINDOW"] = "1"
    env["CUKTECH_NO_TRAY"] = "1"
    proc = subprocess.Popen(exe_cmd, cwd=str(cwd), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    keys = None
    try:
        port = wait_ready(home, timeout=120)
        if port:
            st = get(port, "/api/state")
            keys = set((st.get("link") or {}).keys())
            try:
                get(port, "/api/quit", "POST", {}, timeout=3)
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)
    return keys


def part_a() -> dict:
    """真机对照：旧/新 exe 未连接态 link 键集合。"""
    out = {}
    if not OLD_EXE.exists():
        skip("旧 Node exe 对照", str(OLD_EXE))
    # 新侧优先用**打包产物**（若已构建），否则退回源码服务。
    if NEW_EXE.exists():
        new_cmd, new_cwd, new_tag = [str(NEW_EXE)], NEW_EXE.parent, "新 exe"
    else:
        new_cmd, new_cwd, new_tag = NEW_CMD, ROOT, "新源码"
    try:
        out["new"] = link_keys_of(new_cmd, new_cwd, "new")
    except Exception as exc:
        out["new"] = None
        check("新 Python 版可起服务抓 link", False, repr(exc))
    if OLD_EXE.exists():
        out["old"] = link_keys_of([str(OLD_EXE)], OLD_EXE.parent, "old")
    print("    %s link 键集合：%s" % (new_tag, sorted(out.get("new") or [])))
    print("    旧 Node 版  link 键集合：%s" % sorted(out.get("old") or []))
    if out.get("new") is not None and out.get("old") is not None:
        check("未连接态：旧/新 link 键集合完全一致", out["new"] == out["old"],
              "new=%s old=%s" % (sorted(out["new"]), sorted(out["old"])))
    check("未连接态：新 Python 版 **不含** rssi 键",
          out.get("new") is not None and "rssi" not in out["new"], str(out.get("new")))
    return out


def part_b() -> None:
    """已连接态：假采集器驱动 Bridge，断言三种帧的键集合与 server.js 规则一致。"""
    tmp = Path(tempfile.mkdtemp(prefix="cuk_link_b_"))
    os.environ["CUKTECH_HOME"] = str(tmp)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    try:
        from pyapp import paths
        from pyapp.collector_bridge import CollectorBridge
        from pyapp.state_store import AppState

        paths.data_dir().mkdir(parents=True, exist_ok=True)
        paths.data_file("cuktech.token").write_text('{"address": "%s", "token_hex": "00"}' % MAC,
                                                    encoding="utf-8")
        fake = tmp / "fake_collector.py"
        fake.write_text(FAKE_COLLECTOR, encoding="utf-8")
        paths.child_argv = lambda role, extra=None: [sys.executable, str(fake), *(extra or [])]

        class FakeConfig:
            data = {"interval": 1.5, "autoA": False, "address": MAC}

        class FakeApp:
            def __init__(self):
                self.state = AppState()
                self.config = FakeConfig()
                self.auto_a = None

            def push_log(self, line):
                pass

            def log(self, line):
                pass

            def effective_address(self):
                return MAC

            def auto_a_enabled(self):
                return False

        app = FakeApp()
        bridge = CollectorBridge(app)
        app.bridge = bridge

        def wait(pred, timeout=6.0):
            end = time.time() + timeout
            while time.time() < end:
                if pred():
                    return True
                time.sleep(0.03)
            return pred()

        bridge.start()
        try:
            ok = wait(lambda: (app.state.link or {}).get("state") == "connecting")
            keys = set((app.state.link or {}).keys())
            print("    [无 rssi 的 link]      keys=%s" % sorted(keys))
            check("已连接前：link 键集合 = {state,msg,attempt}（无 rssi）",
                  ok and keys == {"state", "msg", "attempt"}, str(keys))

            ok = wait(lambda: (app.state.link or {}).get("state") == "reconnecting")
            keys = set((app.state.link or {}).keys())
            print("    [rssi=null 的 link]    keys=%s" % sorted(keys))
            check("rssi 显式 null：link **不含** rssi 键（对齐 Node undefined 丢键）",
                  ok and "rssi" not in keys, str(keys))

            ok = wait(lambda: (app.state.link or {}).get("state") == "connected")
            keys = set((app.state.link or {}).keys())
            print("    [有 rssi 的 link]      keys=%s" % sorted(keys))
            check("已连接（有 rssi）：link 键集合 = {state,msg,attempt,rssi}（= server.js 规则）",
                  ok and keys == {"state", "msg", "attempt", "rssi"}, str(keys))
        finally:
            bridge.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("== A) 真机对照：旧/新 exe 未连接态 link 键集合 ==")
    part_a()
    print("== B) 已连接态：假采集器驱动 Bridge（新 Python 侧）==")
    part_b()
    print("-" * 60)
    print("T05(link 键集合) 汇总：通过 %d，失败 %d，跳过 %d" % (PASS, FAIL, SKIP))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
