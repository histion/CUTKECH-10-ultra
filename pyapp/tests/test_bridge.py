"""T03 自测：采集器子进程桥接 / 控制命令校验 / 登录代理。

用一个**假采集器脚本**（输出 hello/link/state + 对 set 回 ack 的 JSONL）模拟真实采集器
进程，验证 ``CollectorBridge`` 的解析、ack 匹配、超时、重启逻辑，以及
``control`` 的越界/非整数/清单外拒绝与 ``defaults`` 失败即中止，还有 ``LoginProxy``
的 qr/poll 受理与结果落值。**不依赖真机 BLE**。

离线运行：python pyapp/tests/test_bridge.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0

MAC = "AA:BB:CC:DD:EE:FF"
MAC2 = "11:22:33:44:55:66"

FAKE_COLLECTOR = r'''
import sys, json, time
def emit(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n"); sys.stdout.flush()
WRITABLE = [
    {"piid": 16, "name": "端口开关", "type": "u8", "min": 0, "max": 15, "advanced": False},
    {"piid": 5, "name": "场景模式", "type": "u8", "min": 1, "max": 4, "advanced": False},
]
SWITCHES = [{"port": "a", "sw": "ufcs", "label": "UFCS", "bit": 24}]
emit({"t": "hello", "v": "1.3", "pid": 4242, "writable": WRITABLE, "switches": SWITCHES})
emit({"t": "link", "state": "connected", "msg": "已连接", "rssi": -55})
emit({"t": "state", "at": int(time.time() * 1000), "ok": True, "rssi": -55,
      "ports": [{"id": "c1", "w": 5}], "settings": {"port_ctl": 15, "protocol_ctl_extend": 0}})
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except Exception:
        emit({"t": "ack", "ok": False, "error": "bad json"}); continue
    kind = cmd.get("cmd")
    cid = cmd.get("id")
    if kind in ("quit", "exit"):
        break
    if kind == "hang":
        continue                      # 故意不回，测超时
    emit({"t": "ack", "id": cid, "ok": True, "cmd": kind,
          "piid": cmd.get("piid"), "value": cmd.get("value"),
          "name": "测试属性", "readback": cmd.get("value")})
'''

FAKE_QR = r'''
import json, sys
print(json.dumps({"ok": True, "png": "data/qr.png", "timeout": 280, "stateFile": "x"}))
'''

FAKE_POLL = r'''
import json, sys, time
time.sleep(0.2)
print(json.dumps({"ok": True, "region": "cn", "mac": "MAC_PLACEHOLDER",
                  "tokenLen": 24, "candidates": []}))
'''


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


def wait_until(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class FakeConfig:
    def __init__(self):
        self.data = {"interval": 1.5, "autoA": False, "address": MAC, "tray": True,
                     "trayMode": "total"}

    def save(self, cfg=None):
        if cfg is not None:
            self.data = cfg


class FakeApp:
    def __init__(self):
        from pyapp.state_store import AppState
        self.state = AppState()
        self.config = FakeConfig()
        self.logs = []
        self.auto_a = None
        self.bridge = None
        self.login = None

    def push_log(self, line):
        self.logs.append(str(line))

    def log(self, line):
        self.logs.append(str(line))

    def effective_address(self):
        return self.config.data.get("address") or ""

    def set_address(self, mac):
        self.config.data["address"] = mac

    def auto_a_enabled(self):
        return self.config.data.get("autoA") is True

    def lines(self):
        return "\n".join(self.logs)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cuk_bridge_"))
    os.environ["CUKTECH_HOME"] = str(tmp)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    try:
        from pyapp import paths, control
        from pyapp.collector_bridge import CollectorBridge
        from pyapp.login_proxy import LoginProxy

        # 数据目录 + token
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        paths.data_file("cuktech.token").write_text('{"address": "%s", "token_hex": "00"}' % MAC,
                                                    encoding="utf-8")

        # 三个假脚本
        fake_collector = tmp / "fake_collector.py"
        fake_collector.write_text(FAKE_COLLECTOR, encoding="utf-8")
        fake_qr = tmp / "fake_qr.py"
        fake_qr.write_text(FAKE_QR, encoding="utf-8")
        fake_poll = tmp / "fake_poll.py"
        fake_poll.write_text(FAKE_POLL.replace("MAC_PLACEHOLDER", MAC2), encoding="utf-8")

        # 把 child_argv 指向假脚本（collector/login-qr/login-poll 各司其职）
        role_script = {"collector": fake_collector, "login-qr": fake_qr,
                       "login-poll": fake_poll, "sweep": fake_collector}

        def fake_child_argv(role, extra=None):
            script = role_script.get(role, fake_collector)
            return [sys.executable, str(script), *(extra or [])]

        paths.child_argv = fake_child_argv   # monkeypatch

        app = FakeApp()
        bridge = CollectorBridge(app)
        app.bridge = bridge
        login = LoginProxy(app)
        app.login = login
        procs = []

        # ---------- 无设备时拒绝 ----------
        r = bridge.send_command({"cmd": "set", "piid": 16, "value": 1})
        check("无设备 send_command 拒绝（采集器未运行…）",
              r.get("ok") is False and "采集器未运行" in (r.get("error") or ""), str(r))

        # ---------- 启动 → hello/link/state 解析 ----------
        started = bridge.start()
        check("bridge.start 成功", started.get("ok") is True, str(started))
        procs.append(app.state.collector)
        ok = wait_until(lambda: (app.state.link or {}).get("state") == "connected"
                        and app.state.writable and app.state.latest is not None)
        check("解析 hello/link/state：link=connected 且 writable/latest 就绪", ok,
              "link=%s" % app.state.link)
        check("有 state 帧即写入历史", len(app.state.history) >= 1)

        # ---------- ack 匹配 ----------
        ack = bridge.send_command({"cmd": "set", "piid": 16, "value": 15})
        check("send_command 收到匹配 ack", ack.get("ok") is True and ack.get("readback") == 15, str(ack))

        # ---------- 超时 ----------
        t0 = time.time()
        to = bridge.send_command({"cmd": "hang"}, timeout_ms=700)
        check("未回 ack 走超时（设备响应超时）",
              to.get("ok") is False and "超时" in (to.get("error") or ""), str(to))
        check("超时耗时接近 700ms", (time.time() - t0) < 3.0)

        # ---------- setProp 校验 ----------
        app.state.writable = [
            {"piid": 16, "name": "端口开关", "min": 0, "max": 15, "advanced": False},
            {"piid": 5, "name": "场景模式", "min": 1, "max": 4, "advanced": False},
        ]
        bad = control.set_prop(bridge, 16, 99)
        check("setProp 越界被拒", bad.get("ok") is False and "之间" in (bad.get("error") or ""), str(bad))
        bad = control.set_prop(bridge, 16, 1.5)
        check("setProp 非整数被拒", bad.get("ok") is False and "整数" in (bad.get("error") or ""), str(bad))
        bad = control.set_prop(bridge, 99, 1)
        check("setProp 不在清单被拒", bad.get("ok") is False and "可写清单" in (bad.get("error") or ""), str(bad))
        bad = control.set_prop(bridge, 16, 1, advanced=True)
        check("setProp advanced 非高级区被拒", bad.get("ok") is False and "高级区" in (bad.get("error") or ""), str(bad))

        # ---------- setPort：位掩码 / noop ----------
        app.state.latest = {"settings": {"port_ctl": 15}}
        rp = control.set_port(bridge, "c1", "off")           # 15 & ~1 = 14
        check("setPort c1 off → value=14", rp.get("ok") is True and rp.get("value") == 14, str(rp))
        rp = control.set_port(bridge, "a", "on")             # bit3 已置 1 → noop
        check("setPort a on 已在位 → noop=True",
              rp.get("ok") is True and rp.get("noop") is True, str(rp))
        rp = control.set_port(bridge, "zz", "on")
        check("setPort 未知端口被拒", rp.get("ok") is False and "未知端口" in (rp.get("error") or ""), str(rp))

        # ---------- defaults：某步失败即中止并返回 done ----------
        app.state.writable = [{"piid": 16, "name": "端口开关", "min": 0, "max": 15, "advanced": False}]
        dr = control.handle_control(bridge, {"action": "defaults"})
        done = dr.get("done") or []
        check("defaults 首步成功、次步（piid5 不在清单）失败即中止并返回 done（含失败步）",
              dr.get("ok") is False and len(done) == 2 and done[0]["ok"] is True
              and done[1]["ok"] is False and "场景模式 = AI" in (dr.get("error") or ""), str(dr))

        # ---------- 未知动作 ----------
        ur = control.handle_control(bridge, {"action": "nope"})
        check("未知动作被拒", ur.get("ok") is False and "未知动作" in (ur.get("error") or ""), str(ur))

        # ---------- restart_soon：等旧进程退出再起新进程 ----------
        old = app.state.collector
        bridge.restart_soon()
        ok = wait_until(lambda: app.state.collector is not None and app.state.collector is not old
                        and (app.state.link or {}).get("state") == "connected", timeout=10)
        procs.append(app.state.collector)
        check("restart_soon 起新进程且重新连上（非旧进程）", ok,
              "collector=%s" % app.state.collector)

        # ---------- stop ----------
        bridge.stop()
        check("stop 后 collector 置空、collecting=False",
              app.state.collector is None and app.state.collecting is False)

        # ---------- 登录：start(qr) ----------
        sr = login.start()
        check("login.start 成功返回 timeout=280",
              sr.get("ok") is True and sr.get("timeout") == 280, str(sr))

        # ---------- 登录：poll 受理 → result 落值 + 地址自动发现 + 重启采集器 ----------
        pr = login.poll()
        check("login.poll 受理（ok=true）", pr.get("ok") is True, str(pr))
        ok = wait_until(lambda: login.current()["running"] is False
                        and login.current()["result"] is not None, timeout=10)
        result = login.current()["result"]
        check("poll 结果落值 {ok,region,tokenLen}",
              ok and result == {"ok": True, "region": "cn", "tokenLen": 24}, str(result))
        check("poll 成功后自动发现并记住设备地址", app.config.data.get("address") == MAC2,
              str(app.config.data))
        ok = wait_until(lambda: app.state.collector is not None, timeout=10)
        procs.append(app.state.collector)
        check("poll 成功后重启采集器（restart_soon）", ok and "登录成功" in app.lines())

        # ---------- 登录冲突 ----------
        login.running = True
        check("已有任务时 login.start 冲突",
              login.start().get("ok") is False and login.poll().get("ok") is False)

        # 清理
        login.stop()
        bridge.stop()
        for p in procs:
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    print("T03 汇总：通过 %d，失败 %d" % (PASS, FAIL))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
