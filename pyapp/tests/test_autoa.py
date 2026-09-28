"""T02 自测（二）：A 口自动化纯逻辑 —— autoA.js 用例逐字段对照。

把 ``build/qa-auto-a.js`` 的全部用例翻译成 Python 断言（含 LOAD_W=0.5 / RELEASE_W=0.2 /
迟滞 / 边界 0.2、0.5 / 降级 / 重试预算 / 恒定状态时序），逐字段比对 ``compute_auto_a_action``
与 ``consume_write_budget`` 的输出。

离线运行：python pyapp/tests/test_autoa.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


def latest(w, enabled_a, low_cur):
    """造一份 latest：w={c1,c2,c3,a} 功率；enabled_a/low_cur 为当前开关（None → 读不到）。"""
    ports = [{"id": k, "w": v} for k, v in w.items()]
    settings = {
        "ports_enabled": None if enabled_a is None else {"c1": True, "c2": True, "c3": True, "a": enabled_a},
        "usb_a_always_on": None if low_cur is None else low_cur,
    }
    return {"ports": ports, "settings": settings}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cuk_aa_"))
    os.environ["CUKTECH_HOME"] = str(tmp)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    try:
        from pyapp import auto_a
        from pyapp.auto_a import LOAD_W, RELEASE_W, LOW_CURRENT_PIID, C_PORTS

        print("LOAD_W = %s   RELEASE_W = %s   LOW_CURRENT_PIID = 0x%X"
              % (LOAD_W, RELEASE_W, LOW_CURRENT_PIID))
        print("=" * 80)

        # 用例集（逐条对齐 build/qa-auto-a.js 的 cases 数组）
        cases = [
            ("① C1 有负载(30W)，A 关/小电流关 → 目标为「开」",
             latest({"c1": 30, "c2": 0, "c3": 0, "a": 0}, False, False), None,
             {"band": "load", "active": True, "cLoaded": True, "targetA": True,
              "targetLowCurrent": True, "changed": True, "needA": True, "needLowCurrent": True}),
            ("② 三 C 口全空(0W)，A 关/小电流关 → 目标为「关」且无需动作",
             latest({"c1": 0, "c2": 0, "c3": 0, "a": 0}, False, False), None,
             {"band": "idle", "active": True, "cAllReleased": True, "targetA": False,
              "targetLowCurrent": False, "changed": False, "needA": False, "needLowCurrent": False}),
            ("③ C 口有负载且已是目标（A 开/小电流开）→ 不需动作",
             latest({"c1": 12, "c2": 0, "c3": 0, "a": 0}, True, True), None,
             {"cLoaded": True, "targetA": True, "changed": False, "needA": False, "needLowCurrent": False}),
            ("④ 边界 w=0.49（迟滞区间，当前关）→ 保持关、changed=false",
             latest({"c1": 0.49, "c2": 0, "c3": 0, "a": 0}, False, False), None,
             {"band": "hold", "cLoaded": False, "cAllReleased": False, "targetA": False, "changed": False}),
            ("⑤ 边界 w=0.5（> LOAD_W 才计负载，当前关）→ 保持关",
             latest({"c1": 0.5, "c2": 0, "c3": 0, "a": 0}, False, False), None,
             {"band": "hold", "cLoaded": False, "targetA": False, "changed": False}),
            ("⑥ 边界 w=0.51（> LOAD_W 计负载）→ 开",
             latest({"c1": 0.51, "c2": 0, "c3": 0, "a": 0}, False, False), None,
             {"band": "load", "cLoaded": True, "targetA": True, "changed": True}),
            ("⑦ C2/C3 负载（任一口有负载即算）",
             latest({"c1": 0, "c2": 5, "c3": 9, "a": 0}, False, False), None,
             {"cLoaded": True, "targetA": True, "targetLowCurrent": True, "changed": True}),
            ("⑧ A 口有负载(40W)但 C 口全空 → 目标仍为「关」",
             latest({"c1": 0, "c2": 0, "c3": 0, "a": 40}, True, True), None,
             {"active": True, "cLoaded": False, "aLoaded": True, "targetA": False,
              "changed": True, "needA": True}),
            ("⑨ 缺 C 口数据（ports 只给 a）→ 视为全空，不崩",
             {"ports": [{"id": "a", "w": 0}],
              "settings": {"ports_enabled": {"a": False}, "usb_a_always_on": False}}, None,
             {"active": True, "cLoaded": False, "targetA": False, "changed": False}),
            ("⑩ latest 为空 → 不崩，无负载", None, None,
             {"active": True, "cLoaded": False, "targetA": False}),
            ("⑪ 非法功率值（缺 w / 字符串 / 负值）→ 按 0 处理，不崩",
             {"ports": [{"id": "c1"}, {"id": "c2", "w": "abc"}, {"id": "c3", "w": -3}],
              "settings": {"ports_enabled": {"a": False}, "usb_a_always_on": False}}, None,
             {"active": True, "cLoaded": False, "targetA": False, "changed": False}),
            ("⑫ 自定义负载阈值 10W：C1=9W 不算负载",
             latest({"c1": 9, "c2": 0, "c3": 0, "a": 0}, False, False), {"loadW": 10},
             {"band": "hold", "cLoaded": False, "targetA": False}),
            ("⑬ 迟滞：0.3W（中间区间）且当前「开」→ 保持开、changed=false",
             latest({"c1": 0.3, "c2": 0, "c3": 0, "a": 0}, True, True),
             {"current": {"a": True, "lowCurrent": True}},
             {"band": "hold", "cLoaded": False, "cAllReleased": False, "targetA": True,
              "targetLowCurrent": True, "changed": False}),
            ("⑭ 迟滞：0.3W（中间区间）且当前「关」→ 保持关、changed=false",
             latest({"c1": 0.3, "c2": 0, "c3": 0, "a": 0}, False, False),
             {"current": {"a": False, "lowCurrent": False}},
             {"band": "hold", "targetA": False, "changed": False}),
            ("⑮ 0.6W → 开（> LOAD_W）",
             latest({"c1": 0.6, "c2": 0, "c3": 0, "a": 0}, False, False),
             {"current": {"a": False, "lowCurrent": False}},
             {"band": "load", "cLoaded": True, "targetA": True, "changed": True}),
            ("⑯ 0.1W → 关（<= RELEASE_W）",
             latest({"c1": 0.1, "c2": 0, "c3": 0, "a": 0}, True, True),
             {"current": {"a": True, "lowCurrent": True}},
             {"band": "idle", "cAllReleased": True, "targetA": False, "changed": True}),
            ("⑰ 0.2W 边界 → 关（<= RELEASE_W）",
             latest({"c1": 0.2, "c2": 0, "c3": 0, "a": 0}, True, True),
             {"current": {"a": True, "lowCurrent": True}},
             {"band": "idle", "cAllReleased": True, "targetA": False, "changed": True}),
            ("⑱ 0.5W 边界（不计负载）+ 当前关 → 保持关、changed=false",
             latest({"c1": 0.5, "c2": 0, "c3": 0, "a": 0}, False, False),
             {"current": {"a": False, "lowCurrent": False}},
             {"band": "hold", "cLoaded": False, "targetA": False, "changed": False}),
            ("⑲ 三 C 口全 0 且当前关 → 关、changed=false",
             latest({"c1": 0, "c2": 0, "c3": 0, "a": 0}, False, False),
             {"current": {"a": False, "lowCurrent": False}},
             {"band": "idle", "cAllReleased": True, "targetA": False, "changed": False}),
            ("⑳ 读不到当前状态 + 中间区间(0.3W) → 退化为按负载（关）",
             latest({"c1": 0.3, "c2": 0, "c3": 0, "a": 0}, None, None),
             {"current": {"a": None, "lowCurrent": None}},
             {"band": "idle-no-hysteresis", "cLoaded": False, "targetA": False}),
        ]

        print("==== A) compute_auto_a_action 目标判定（含迟滞）====")
        for name, lat, opts, expect in cases:
            opts = opts or {}
            load_w = opts.get("loadW", LOAD_W)
            current = opts["current"] if "current" in opts else None
            try:
                r = auto_a.compute_auto_a_action(lat, current=current, load_w=load_w)
                mism = ["%s 期望 %r 实际 %r" % (k, v, r.get(k)) for k, v in expect.items()
                        if r.get(k) != v]
                check(name, not mism, "；".join(mism))
            except Exception as exc:  # noqa: BLE001
                check(name, False, "抛出异常：%r" % exc)

        # ==== B) consume_write_budget 重试预算 ====
        print("-" * 80)
        print("==== B) consume_write_budget 重试预算 ====")
        b = {"lastKey": None, "attempts": 0, "pausedKey": None}
        seen = []
        for _ in range(6):
            p = auto_a.consume_write_budget(b, "on", 3)
            b = p["next"]
            seen.append({"allow": p["allow"], "pause": p["pause"]})
        expect_seq = [{"allow": True, "pause": False}, {"allow": True, "pause": False},
                      {"allow": True, "pause": False}, {"allow": False, "pause": True},
                      {"allow": False, "pause": False}, {"allow": False, "pause": False}]
        check("同一目标 6 次调用（cap=3）→ 前3允许/第4暂停/后2静默",
              seen == expect_seq, str(seen))

        p_off = auto_a.consume_write_budget(b, "off", 3)
        check("暂停后目标变化(on→off) → 预算重置：allow=True, attempts=1",
              p_off["allow"] is True and p_off["pause"] is False and p_off["next"]["attempts"] == 1,
              str(p_off))

        b2 = {"lastKey": None, "attempts": 0, "pausedKey": None}
        pause_count = 0
        for _ in range(12):
            p = auto_a.consume_write_budget(b2, "on", 3)
            b2 = p["next"]
            if p["pause"]:
                pause_count += 1
        check("状态始终不变场景：12 次调用只触发 1 次 pause",
              pause_count == 1, "got %d" % pause_count)

        # ==== C) 常量 + 恒定状态时序 ====
        print("-" * 80)
        print("==== C) 运行态常量 + 恒定状态时序 ====")
        check("auto_a.AUTO_MAX_WRITES == 3", auto_a.AUTO_MAX_WRITES == 3)
        check("AutoARunner.evaluate 存在", hasattr(auto_a.AutoARunner, "evaluate"))
        pause_text = "A口自动化：连续 %d 次写入后状态仍未变化，暂停重试" % auto_a.AUTO_MAX_WRITES

        budget = {"lastKey": None, "attempts": 0, "pausedKey": None}
        pending_key = None
        pending_count = 0
        writes = 0
        logs = []
        r = {"changed": True, "targetA": True}   # 目标恒定、状态读不到 → 永远需要写
        for _frame in range(20):
            if not r["changed"]:
                pending_key = None
                pending_count = 0
                budget = {"lastKey": None, "attempts": 0, "pausedKey": None}
                continue
            key = "on" if r["targetA"] else "off"
            if pending_key == key:
                pending_count += 1
            else:
                pending_key = key
                pending_count = 1
            if pending_count < auto_a.AUTO_DEBOUNCE_SAMPLES:
                continue
            plan = auto_a.consume_write_budget(budget, key, auto_a.AUTO_MAX_WRITES)
            budget = plan["next"]
            if plan["pause"]:
                logs.append(pause_text)
                continue
            if not plan["allow"]:
                continue
            writes += 1
            pending_key = None
            pending_count = 0
        check("状态恒定场景时序：实际写入 3 次、暂停日志 1 条",
              writes == 3 and len(logs) == 1, "writes=%d logs=%d" % (writes, len(logs)))

        # C_PORTS 顺序
        check("C_PORTS == ['c1','c2','c3']", C_PORTS == ["c1", "c2", "c3"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 80)
    print("T02(autoA) 汇总：通过 %d，失败 %d" % (PASS, FAIL))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
