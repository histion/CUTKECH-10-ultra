"""A 口自动化：纯逻辑 + 运行态纪律（等价移植 ``autoA.js`` + ``server.js`` 的自动化接线）。

用户需求：开关打开时，C1/C2/C3 只要有一口带载，就自动打开 USB-A 口 + 小电流模式；
三口全空则自动关掉二者。

本模块分两层：
  * **纯逻辑**（``port_w`` / ``current_state_from_latest`` / ``compute_auto_a_action`` /
    ``consume_write_budget``）：零 IO、无副作用，可无充电头单测，逐字段对齐 JS；
  * **运行态纪律**（``AutoARunner``）：防抖 / 最小间隔 / 重入保护 / 重试上限 / 写序，
    对齐 ``server.js`` 的 ``evaluateAutoA``/``applyAutoA``/``resetAutoCounters``。

判定规则（用户原始需求 + 迟滞，见 design §7.1）：
  * 开条件 ``band="load"``：任一 C 口 ``w > LOAD_W`` → 目标 = USB-A 开 + 小电流开；
  * 关条件 ``band="idle"``：三 C 口**全部** ``w <= RELEASE_W`` → 目标 = 关；
  * 迟滞 ``band="hold"``：``RELEASE_W < max(C口) <= LOAD_W`` 且当前状态可读 → 保持当前；
  * 降级 ``band="idle-no-hysteresis"``：当前状态读不到 → 退化为按负载判定（目标=关），
    由重试上限兜底，避免无限重试。
"""

from __future__ import annotations

import math
import threading
import time

from . import control

# 开条件门限（W）：任一 C 口功率 **大于** 它就视为"有负载"。与 server.js 的
# ``SESSION_MIN_W``（空载噪声门限）同源 —— 唯一常量，避免两处各写一个 0.5。
LOAD_W = 0.5
# 关条件门限（W）：三 C 口**全部 <=** 它才判定"空闲 → 关"。与 LOAD_W 之间即迟滞带。
RELEASE_W = 0.2
# 需要关注的 C 口顺序。
C_PORTS = ["c1", "c2", "c3"]
# 小电流模式的属性号（piid）：2.15（0x0F）。
LOW_CURRENT_PIID = 0x0F

# 运行态纪律参数（design §7.2）。
AUTO_MIN_INTERVAL_MS = 3000
AUTO_DEBOUNCE_SAMPLES = 2
AUTO_MAX_WRITES = 3


# ------------------------------------------------------------------ 纯逻辑

def _num(value) -> float:
    """模拟 ``Number(value)`` 后判有限：非数/空 → 0.0。"""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) else 0.0


def port_w(ports, pid: str) -> float:
    """从 ``latest.ports`` 安全取某口功率（W）。缺失/非数字按 0。"""
    if not isinstance(ports, list):
        return 0.0
    for p in ports:
        if isinstance(p, dict) and p.get("id") == pid:
            return _num(p.get("w"))
    return 0.0


def current_state_from_latest(latest) -> dict:
    """从 ``latest.settings`` 提取当前 A 口 / 小电流实际状态（供迟滞判定）。

    A 口开关取 ``settings.ports_enabled.a``；小电流取 ``settings.usb_a_always_on``；
    读不到一律 ``None``。
    """
    s = latest.get("settings") if isinstance(latest, dict) else None
    enabled = s.get("ports_enabled") if isinstance(s, dict) else None
    a = enabled.get("a") if (isinstance(enabled, dict) and isinstance(enabled.get("a"), bool)) else None
    low = s.get("usb_a_always_on") if (isinstance(s, dict) and isinstance(s.get("usb_a_always_on"), bool)) else None
    return {"a": a, "lowCurrent": low}


def compute_auto_a_action(latest, current=None,
                          load_w: float = LOAD_W, release_w: float = RELEASE_W) -> dict:
    """计算此刻的目标动作与当前差异。纯函数，无副作用。

    ``current`` 缺省时从 ``latest.settings`` 派生；显式传入 ``{"a":…, "lowCurrent":…}``
    可覆盖（JS 里等价于 ``opts.current``）。``load_w``/``release_w`` 覆盖门限
    （JS 里的 ``opts.loadW``/``opts.releaseW``，其中旧别名 ``threshold`` 归并进 ``load_w``）。
    """
    try:
        load_w = float(load_w)
        if not math.isfinite(load_w):
            load_w = LOAD_W
    except (TypeError, ValueError):
        load_w = LOAD_W
    try:
        release_w = float(release_w)
        if not math.isfinite(release_w):
            release_w = RELEASE_W
    except (TypeError, ValueError):
        release_w = RELEASE_W

    ports = latest.get("ports") if isinstance(latest, dict) else None
    c_ports = {pid: port_w(ports, pid) for pid in C_PORTS}
    c_loaded = any(c_ports[pid] > load_w for pid in C_PORTS)             # 开条件
    c_all_released = all(c_ports[pid] <= release_w for pid in C_PORTS)   # 关条件
    a_loaded = port_w(ports, "a") > load_w

    cur = current if isinstance(current, dict) else current_state_from_latest(latest)
    cur_a = cur.get("a") if isinstance(cur.get("a"), bool) else None
    cur_low = cur.get("lowCurrent") if isinstance(cur.get("lowCurrent"), bool) else None
    current_known = cur_a is not None and cur_low is not None

    if c_loaded:
        target_a, target_low, band = True, True, "load"
    elif c_all_released:
        target_a, target_low, band = False, False, "idle"
    elif current_known:
        target_a, target_low, band = cur_a, cur_low, "hold"
    else:
        target_a, target_low, band = False, False, "idle-no-hysteresis"

    # 读不到当前值就保守地认为"需要写"（设备侧对同名值会返回 noop，代价极小）。
    need_a = True if cur_a is None else (cur_a != target_a)
    need_low = True if cur_low is None else (cur_low != target_low)
    changed = need_a or need_low

    if band == "load":
        reason = "C 口有负载 → 目标：USB-A 开 + 小电流开"
    elif band == "idle":
        reason = ("C 口空闲（A 口有负载）→ 目标：USB-A 关 + 小电流关" if a_loaded
                  else "C 口空闲 → 目标：USB-A 关 + 小电流关")
    elif band == "hold":
        reason = "C 口功率处于迟滞区间（%s~%sW）→ 保持当前状态" % (release_w, load_w)
    else:
        reason = "读不到当前状态 → 退化为按负载判定"

    return {
        "active": True,
        "band": band,
        "cLoaded": c_loaded,
        "cAllReleased": c_all_released,
        "aLoaded": a_loaded,
        "cPorts": c_ports,
        "targetA": target_a,
        "targetLowCurrent": target_low,
        "curA": cur_a,
        "curLowCurrent": cur_low,
        "needA": need_a,
        "needLowCurrent": need_low,
        "changed": changed,
        "reason": reason,
    }


def consume_write_budget(budget, key: str, max_writes: int) -> dict:
    """同一目标的重试预算（纯函数）。语义逐字对齐 ``autoA.consumeWriteBudget``：

      * 目标 ``key`` 变化（``on``↔``off``）→ 预算重置，允许重新尝试；
      * 同一 key 第 ``1..max`` 次 → ``allow=True``；
      * 同一 key 第 ``max+1`` 次 → ``allow=False, pause=True``（调用方记一条日志）；
      * 之后同 key → ``allow=False, pause=False``（已暂停，静默等待目标变化）。
    """
    b = budget if isinstance(budget, dict) else {}
    last_key = b.get("lastKey") if b.get("lastKey") in ("on", "off") else None
    try:
        attempts = int(b.get("attempts"))
    except (TypeError, ValueError):
        attempts = 0
    paused_key = b.get("pausedKey") if b.get("pausedKey") in ("on", "off") else None
    try:
        cap = int(max_writes)
    except (TypeError, ValueError):
        cap = AUTO_MAX_WRITES

    if last_key != key:                     # 目标变化 → 重置预算
        last_key, attempts, paused_key = None, 0, None

    if paused_key == key:
        return {"allow": False, "pause": False,
                "next": {"lastKey": last_key, "attempts": attempts, "pausedKey": paused_key}}

    next_attempts = attempts + 1
    if next_attempts > cap:
        return {"allow": False, "pause": True,
                "next": {"lastKey": key, "attempts": attempts, "pausedKey": key}}
    return {"allow": True, "pause": False,
            "next": {"lastKey": key, "attempts": next_attempts, "pausedKey": None}}


# ------------------------------------------------------------------ 运行态纪律

class AutoARunner:
    """A 口自动化的运行态纪律状态机。

    依赖注入：``state``（AppState）、``config``（ConfigStore，读 ``autoA`` 开关）、
    ``bridge``（写设备）、``push_log``（UI 日志）、``set_port``/``set_prop``（默认走
    ``control``，测试可注入假实现）。

    ``evaluate`` 是**同步**方法（内部调 ``operation`` 阻塞等 ack）；由于写命令要等采集器
    读取线程投递 ack，``evaluate`` 必须在**非采集器读取线程**里跑 —— 故提供
    ``request_evaluate`` 用独立工作线程调度。
    """

    def __init__(self, state, config, bridge, push_log,
                 set_port=None, set_prop=None, log=None) -> None:
        self.state = state
        self.config = config
        self.bridge = bridge
        self.push_log = push_log if callable(push_log) else (lambda _m: None)
        self._set_port = set_port or control.set_port
        self._set_prop = set_prop or control.set_prop
        self._log = log if callable(log) else (lambda _m: None)

        self.busy = False
        self.last_action_at = 0.0
        self.pending_key: str | None = None
        self.pending_count = 0
        self.budget = {"lastKey": None, "attempts": 0, "pausedKey": None}
        self._worker: threading.Thread | None = None

    # -------------------------------------------------------------- 开关

    def _enabled(self) -> bool:
        try:
            return self.config.data.get("autoA") is True
        except AttributeError:
            try:
                return self.config.get("autoA") is True
            except Exception:
                return False

    def reset_counters(self) -> None:
        """清空防抖与重试预算（目标收敛 / 开关切换时调用）。"""
        self.pending_key = None
        self.pending_count = 0
        self.budget = {"lastKey": None, "attempts": 0, "pausedKey": None}

    # -------------------------------------------------------------- 调度

    def request_evaluate(self) -> None:
        """从采集器读取线程触发一次评估：起一个独立工作线程，避免与 ack 投递死锁。"""
        if self._worker is not None and self._worker.is_alive():
            return
        worker = threading.Thread(target=self._safe_evaluate, name="autoA", daemon=True)
        self._worker = worker
        worker.start()

    def _safe_evaluate(self) -> None:
        try:
            self.evaluate()
        except Exception as exc:  # noqa: BLE001 -- 自动化失败绝不影响采集主流程
            try:
                self.push_log("A口自动化：判定出错 — %s" % exc)
            except Exception:
                pass

    # -------------------------------------------------------------- 评估

    def evaluate(self) -> None:
        """在"autoA 已开 + 已连接 + 有 latest"时评估一次并按纪律执行。"""
        if not self._enabled():
            return
        link = self.state.link if isinstance(self.state.link, dict) else {}
        if link.get("state") != "connected":
            return
        if not self.state.latest:
            return
        if self.busy:
            return

        try:
            latest = self.state.latest
            result = compute_auto_a_action(latest, current_state_from_latest(latest))
        except Exception as exc:  # noqa: BLE001
            self.push_log("A口自动化：判定出错 — %s" % exc)
            return

        if not result["changed"]:          # 目标已满足 → 收敛
            self.reset_counters()
            return

        key = "on" if result["targetA"] else "off"
        if self.pending_key == key:
            self.pending_count += 1
        else:
            self.pending_key = key
            self.pending_count = 1

        if self.pending_count < AUTO_DEBOUNCE_SAMPLES:
            return                          # 防抖：再等一帧确认

        now = int(time.time() * 1000)
        if now - self.last_action_at < AUTO_MIN_INTERVAL_MS:
            return                          # 最小间隔

        plan = consume_write_budget(self.budget, key, AUTO_MAX_WRITES)
        self.budget = plan["next"]
        if plan["pause"]:
            self.push_log("A口自动化：连续 %d 次写入后状态仍未变化，暂停重试" % AUTO_MAX_WRITES)
            return
        if not plan["allow"]:
            return                          # 已暂停：静默等待目标变化

        self._apply(result)

    # -------------------------------------------------------------- 写设备

    def _apply(self, result: dict) -> None:
        """按目标写设备。开：先开 A 口再开小电流；关：先关小电流再关 A 口。"""
        self.busy = True
        self.last_action_at = int(time.time() * 1000)

        if result["targetA"]:
            head = "A口自动化：C 口出现负载 → 开启 USB-A + 小电流"
        elif result["aLoaded"]:
            head = "A口自动化：C 口空闲，按自动化关闭 USB-A（此时 A 口有负载）"
        else:
            head = "A口自动化：C 口空闲 → 关闭 USB-A + 小电流"

        try:
            results = []
            if result["targetA"]:
                results.append(("USB-A", self._set_port(self.bridge, "a", "on")))
                results.append(("小电流", self._set_prop(self.bridge, LOW_CURRENT_PIID, 1)))
            else:
                results.append(("小电流", self._set_prop(self.bridge, LOW_CURRENT_PIID, 0)))
                results.append(("USB-A", self._set_port(self.bridge, "a", "off")))
            bad = [(n, r) for n, r in results if not r or r.get("ok") is False]
            if bad:
                self.push_log(head + " 失败：" + "；".join(
                    "%s（%s）" % (n, (r or {}).get("error") or "未知原因") for n, r in bad))
            else:
                self.push_log(head)
        except Exception as exc:  # noqa: BLE001
            self.push_log(head + " 失败：" + str(exc))
        finally:
            self.busy = False
            self.pending_key = None
            self.pending_count = 0


__all__ = [
    "LOAD_W",
    "RELEASE_W",
    "C_PORTS",
    "LOW_CURRENT_PIID",
    "AUTO_MIN_INTERVAL_MS",
    "AUTO_DEBOUNCE_SAMPLES",
    "AUTO_MAX_WRITES",
    "port_w",
    "current_state_from_latest",
    "compute_auto_a_action",
    "consume_write_budget",
    "AutoARunner",
]
